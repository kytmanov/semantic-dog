"""Scanner engine — walks paths, dispatches validation, records results."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from wcmatch import fnmatch as wfn

from .validators import get_validator, all_extensions
from .validators.base import ValidationResult

if TYPE_CHECKING:
    from .config import Config
    from .db import Database


# ---------------------------------------------------------------------------
# Worker function (module-level — must be picklable for pebble)
# ---------------------------------------------------------------------------

def _validate_file(path: str, decode_depth: str = "structure") -> ValidationResult:
    """Top-level picklable task dispatched to pebble worker processes."""
    # Safety: cap Pillow decompression bomb threshold inside worker
    try:
        from PIL import Image, ImageFile
        Image.MAX_IMAGE_PIXELS = 250_000_000
        ImageFile.LOAD_TRUNCATED_IMAGES = False
    except ImportError:
        pass

    ext = Path(path).suffix.lower()
    validator_cls = get_validator(ext)
    if validator_cls is None:
        return ValidationResult(status="unsupported", error=f"No validator for {ext!r}")

    validator = validator_cls()
    import inspect
    sig = inspect.signature(validator.validate)
    if "decode_depth" in sig.parameters:
        return validator.validate(path, decode_depth=decode_depth)
    return validator.validate(path)


# ---------------------------------------------------------------------------
# Exclusion matching
# ---------------------------------------------------------------------------

def is_excluded(path: str, patterns: list[str]) -> bool:
    """Return True if path matches any exclusion glob pattern (supports **)."""
    if not patterns:
        return False
    return any(
        wfn.fnmatch(path, pat)
        for pat in patterns
    )


# ---------------------------------------------------------------------------
# ScanStats — progress tracking
# ---------------------------------------------------------------------------

@dataclass
class ScanStats:
    total: int = 0
    ok: int = 0
    corrupt: int = 0
    unreadable: int = 0
    unsupported: int = 0
    error: int = 0
    skipped: int = 0
    toctou_discards: int = 0
    start_time: float = field(default_factory=time.monotonic)
    scan_id: str = ""

    def files_per_sec(self) -> float:
        elapsed = time.monotonic() - self.start_time
        if elapsed < 0.001:
            return 0.0
        return self.total / elapsed

    def record(self, status: str) -> None:
        self.total += 1
        if status == "ok":
            self.ok += 1
        elif status == "corrupt":
            self.corrupt += 1
        elif status == "unreadable":
            self.unreadable += 1
        elif status == "unsupported":
            self.unsupported += 1
        else:
            self.error += 1


# ---------------------------------------------------------------------------
# File walker
# ---------------------------------------------------------------------------

def walk_paths(
    paths: list[str],
    follow_symlinks: bool = False,
    exclude: list[str] | None = None,
) -> list[tuple[str, float, int]]:
    """
    Walk scan roots, return list of (path, mtime, size) for registered-extension files.
    Unstat-able files (broken symlinks, permission errors) get mtime=0, size=0.
    """
    exclude = exclude or []
    registered_exts = {e.lower() for e in all_extensions()}
    results: list[tuple[str, float, int]] = []

    for root in paths:
        for dirpath, _dirs, filenames in os.walk(root, followlinks=follow_symlinks):
            for name in filenames:
                full = os.path.join(dirpath, name)
                ext = Path(name).suffix.lower()
                if ext not in registered_exts:
                    continue
                if is_excluded(full, exclude):
                    continue
                try:
                    st = os.stat(full)
                    results.append((full, st.st_mtime, st.st_size))
                except OSError:
                    results.append((full, 0.0, 0))

    return results


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

_PROGRESS_INTERVAL_S = 5.0  # print progress at most this often


def _progress_line(done: int, total: int, stats: ScanStats) -> str:
    pct = done / total * 100 if total else 0.0
    fps = stats.files_per_sec()
    remaining = total - done
    if fps > 0 and remaining > 0:
        eta_min = remaining / fps / 60
        eta_str = f"  ETA: ~{eta_min:.1f} min"
    else:
        eta_str = ""
    return (
        f"  [{done}/{total}]  {pct:.1f}%"
        f"  ok:{stats.ok}  corrupt:{stats.corrupt}  unreadable:{stats.unreadable}"
        f"  {fps:.1f} f/s{eta_str}"
    )


def _print_progress(line: str, is_tty: bool) -> None:
    if is_tty:
        sys.stderr.write(f"\r{line}")
        sys.stderr.flush()
    else:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    """
    Orchestrates scans: walk → filter → pebble pool → TOCTOU → DB record.

    Two pebble.ProcessPool instances (spawn context for C library safety):
      - low/medium: SDOG_WORKERS workers, all non-RAW formats
      - high: SDOG_RAW_WORKERS workers, RAW formats
    """

    def __init__(self, config: "Config", db: "Database") -> None:
        self.config = config
        self.db = db
        self._shutdown = threading.Event()
        self._boot_uuid = str(uuid.uuid4())

    def _install_sigterm(self) -> None:
        import threading
        if threading.current_thread() is not threading.main_thread():
            return  # signal.signal only works in main thread
        def _handler(signum: int, frame: object) -> None:
            self._shutdown.set()
        signal.signal(signal.SIGTERM, _handler)

    def estimate(self, paths: list[str] | None = None) -> dict[str, int]:
        """Count files that need checking per extension. No DB writes."""
        scan_paths = paths or self.config.paths
        file_list = walk_paths(scan_paths, self.config.follow_symlinks, self.config.exclude)
        counts: dict[str, int] = {}
        for fpath, mtime, size in file_list:
            if self.db.needs_check(fpath, mtime, size, self.config.force_recheck_days):
                ext = Path(fpath).suffix.lower()
                counts[ext] = counts.get(ext, 0) + 1
        return counts

    def scan(
        self,
        paths: list[str] | None = None,
        resume_scan_id: str | None = None,
    ) -> ScanStats:
        """Run a full scan or resume an interrupted one. Returns ScanStats."""
        from pebble import ProcessPool  # noqa: F401 — ensures pebble available
        from .exceptions import ScanError

        self._install_sigterm()
        self.db.acquire_lock(self._boot_uuid)

        mp_ctx = multiprocessing.get_context("spawn")
        is_tty = sys.stderr.isatty()
        interrupted = False

        if resume_scan_id:
            # ---- Resume path ----
            existing = self.db.get_scan(resume_scan_id)
            if existing is None:
                self.db.release_lock()
                raise ScanError(f"Scan ID not found: {resume_scan_id!r}")
            if existing["finished_at"] is not None:
                self.db.release_lock()
                raise ScanError(
                    f"Scan {resume_scan_id!r} already completed "
                    f"(finished {existing['finished_at']}). Cannot resume."
                )

            scan_id = resume_scan_id
            stats = ScanStats(
                total=existing["total"] or 0,
                corrupt=existing["corrupt"] or 0,
                unreadable=existing["unreadable"] or 0,
                scan_id=scan_id,
            )

            pending_paths = self.db.get_all_pending_paths(scan_id)
            sys.stderr.write(
                f"Resuming scan {scan_id}\n"
                f"Pending files: {len(pending_paths)}\n"
            )
            sys.stderr.flush()

            # Re-stat pending paths
            file_list: list[tuple[str, float, int]] = []
            for p in pending_paths:
                try:
                    st = os.stat(p)
                    file_list.append((p, st.st_mtime, st.st_size))
                except OSError:
                    file_list.append((p, 0.0, 0))

        else:
            # ---- New scan path ----
            scan_paths = paths or self.config.paths
            scan_id = self.db.create_scan(scope=",".join(scan_paths))
            stats = ScanStats(scan_id=scan_id)

            file_list = walk_paths(scan_paths, self.config.follow_symlinks, self.config.exclude)

            sys.stderr.write(
                f"Discovered {len(file_list)} files.\n"
                f"Scan ID: {scan_id}  "
                f"(resume with: sdog scan --resume {scan_id})\n"
            )
            sys.stderr.flush()

            # Populate scan_queue for resume support
            all_paths = [p for p, _, _ in file_list]
            self.db.queue_paths(scan_id, all_paths)
            self.db.cleanup_stale_queues(max_age_days=7)

        from .validators.raw import RawValidator
        high_exts = {e.lower() for e in RawValidator.extensions}

        high_files = [(p, m, s) for p, m, s in file_list if Path(p).suffix.lower() in high_exts]
        low_files  = [(p, m, s) for p, m, s in file_list if Path(p).suffix.lower() not in high_exts]
        total_files = len(file_list)

        try:
            self._run_pool(low_files, scan_id, stats, self.config.workers, mp_ctx, total_files, is_tty)
            if not self._shutdown.is_set():
                self._run_pool(high_files, scan_id, stats, self.config.raw_workers, mp_ctx, total_files, is_tty)
        except KeyboardInterrupt:
            interrupted = True
            self._shutdown.set()
        finally:
            if not interrupted and not self._shutdown.is_set():
                self.db.finish_scan(
                    scan_id,
                    total=stats.total,
                    corrupt=stats.corrupt,
                    unreadable=stats.unreadable,
                    files_per_sec=stats.files_per_sec(),
                )
                self.db.cleanup_scan_queue(scan_id)
            else:
                sys.stderr.write(
                    f"\nInterrupted. Resume with: sdog scan --resume {scan_id}\n"
                )
                sys.stderr.flush()
            self.db.release_lock()

        if interrupted:
            raise KeyboardInterrupt

        if not self._shutdown.is_set():
            if self.db.should_vacuum():
                self.db.incremental_vacuum()

        return stats

    def _run_pool(
        self,
        file_list: list[tuple[str, float, int]],
        scan_id: str,
        stats: ScanStats,
        workers: int,
        context: "multiprocessing.context.BaseContext",
        total_files: int,
        is_tty: bool,
    ) -> None:
        import queue as _queue
        from pebble import ProcessPool

        timeout = self.config.validation_timeout_s
        decode_depth = self.config.raw_decode_depth
        semaphore = threading.Semaphore(workers * 2)

        last_progress_t = time.monotonic()
        done_batch: list[str] = []
        _processed = 0

        # result_q receives (future, fpath, pre_mtime, pre_size) when a worker finishes.
        # Done callbacks run in a pebble thread — we drain in the main thread.
        result_q: _queue.SimpleQueue = _queue.SimpleQueue()
        _n_submitted = 0
        _n_processed = 0  # tracks results consumed (inline drain + blocking drain)

        # Print initial line immediately
        if total_files > 0:
            _print_progress(_progress_line(0, total_files, stats), is_tty)

        def _maybe_progress() -> None:
            nonlocal last_progress_t
            now = time.monotonic()
            if now - last_progress_t >= _PROGRESS_INTERVAL_S:
                line = _progress_line(_processed, total_files, stats)
                _print_progress(line, is_tty)
                last_progress_t = now

        def _process_result(future, fpath: str, pre_mtime: float, pre_size: int) -> None:
            """Handle one completed future. Called from main thread only."""
            nonlocal _processed
            try:
                result = future.result()
            except FuturesTimeoutError:
                result = ValidationResult(
                    status="error",
                    error=f"validation timed out after {timeout}s",
                    suggested_action="File may be severely corrupt",
                )
            except Exception as e:
                result = ValidationResult(status="error", error=str(e))

            # TOCTOU: discard if file changed during validation
            try:
                post_stat = os.stat(fpath)
                if post_stat.st_mtime != pre_mtime or post_stat.st_size != pre_size:
                    stats.toctou_discards += 1
                    _maybe_progress()
                    return
            except OSError:
                stats.toctou_discards += 1
                _maybe_progress()
                return

            self.db.record(
                fpath, pre_mtime, pre_size,
                result.status, scan_id=scan_id,
                error=result.error,
                suggested_action=result.suggested_action,
            )
            stats.record(result.status)
            _processed += 1

            done_batch.append(fpath)
            if len(done_batch) >= 100:
                self.db.mark_queue_done(scan_id, done_batch)
                done_batch.clear()

            _maybe_progress()

        def _drain_nonblocking() -> None:
            """Drain all currently available results without blocking."""
            nonlocal _n_processed
            while True:
                try:
                    f, fp, pm, ps = result_q.get_nowait()
                    _process_result(f, fp, pm, ps)
                    _n_processed += 1
                except _queue.Empty:
                    break

        with ProcessPool(max_workers=workers, context=context, max_tasks=100) as pool:

            for fpath, pre_mtime, pre_size in file_list:
                if self._shutdown.is_set():
                    break

                if not self.db.needs_check(fpath, pre_mtime, pre_size, self.config.force_recheck_days):
                    stats.skipped += 1
                    _processed += 1
                    done_batch.append(fpath)
                    if len(done_batch) >= 100:
                        self.db.mark_queue_done(scan_id, done_batch)
                        done_batch.clear()
                    _maybe_progress()
                    _drain_nonblocking()
                    continue

                # Unstat-able → record immediately
                if pre_mtime == 0.0 and pre_size == 0:
                    self.db.record(fpath, 0.0, 0, "unreadable", scan_id=scan_id,
                                   error="Cannot stat file")
                    stats.record("unreadable")
                    _processed += 1
                    done_batch.append(fpath)
                    if len(done_batch) >= 100:
                        self.db.mark_queue_done(scan_id, done_batch)
                        done_batch.clear()
                    _maybe_progress()
                    _drain_nonblocking()
                    continue

                semaphore.acquire()
                future = pool.schedule(
                    _validate_file,
                    args=(fpath, decode_depth),
                    timeout=timeout,
                )

                _fp, _pm, _ps = fpath, pre_mtime, pre_size

                def _on_done(f, fp=_fp, pm=_pm, ps=_ps) -> None:
                    semaphore.release()
                    result_q.put((f, fp, pm, ps))

                future.add_done_callback(_on_done)
                _n_submitted += 1
                _drain_nonblocking()

            # Drain remaining results blocking until all submitted futures are accounted for
            while _n_processed < _n_submitted:
                f, fp, pm, ps = result_q.get()
                _process_result(f, fp, pm, ps)
                _n_processed += 1

            if self._shutdown.is_set():
                pool.stop()
                pool.join()

        # Flush remaining done_batch
        if done_batch:
            self.db.mark_queue_done(scan_id, done_batch)

        # Final progress line
        if total_files > 0:
            line = _progress_line(_processed, total_files, stats)
            if is_tty:
                sys.stderr.write(f"\r{line}\n")
            else:
                sys.stderr.write(line + "\n")
            sys.stderr.flush()
