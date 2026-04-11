"""Scanner engine — walks paths, dispatches validation, records results."""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
import time
import uuid
from concurrent.futures import as_completed, TimeoutError as FuturesTimeoutError
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

    def scan(self, paths: list[str] | None = None) -> ScanStats:
        """Run a full scan. Acquires instance lock, returns ScanStats."""
        from pebble import ProcessPool  # noqa: F401 — ensures pebble available

        self._install_sigterm()
        self.db.acquire_lock(self._boot_uuid)
        scan_paths = paths or self.config.paths
        scan_id = self.db.create_scan(scope=",".join(scan_paths))
        stats = ScanStats()

        mp_ctx = multiprocessing.get_context("spawn")

        try:
            file_list = walk_paths(scan_paths, self.config.follow_symlinks, self.config.exclude)

            from .validators.raw import RawValidator
            high_exts = {e.lower() for e in RawValidator.extensions}

            high_files = [(p, m, s) for p, m, s in file_list if Path(p).suffix.lower() in high_exts]
            low_files  = [(p, m, s) for p, m, s in file_list if Path(p).suffix.lower() not in high_exts]

            self._run_pool(low_files, scan_id, stats, self.config.workers, mp_ctx)
            if not self._shutdown.is_set():
                self._run_pool(high_files, scan_id, stats, self.config.raw_workers, mp_ctx)
        finally:
            self.db.finish_scan(
                scan_id,
                total=stats.total,
                corrupt=stats.corrupt,
                unreadable=stats.unreadable,
                files_per_sec=stats.files_per_sec(),
            )
            self.db.release_lock()

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
    ) -> None:
        from pebble import ProcessPool

        timeout = self.config.validation_timeout_s
        decode_depth = self.config.raw_decode_depth
        semaphore = threading.Semaphore(workers * 2)

        with ProcessPool(max_workers=workers, context=context, max_tasks=100) as pool:
            futures: dict = {}

            for fpath, pre_mtime, pre_size in file_list:
                if self._shutdown.is_set():
                    break

                if not self.db.needs_check(fpath, pre_mtime, pre_size, self.config.force_recheck_days):
                    stats.skipped += 1
                    continue

                # Unstat-able → record immediately
                if pre_mtime == 0.0 and pre_size == 0:
                    self.db.record(fpath, 0.0, 0, "unreadable", scan_id=scan_id,
                                   error="Cannot stat file")
                    stats.record("unreadable")
                    continue

                semaphore.acquire()
                future = pool.schedule(
                    _validate_file,
                    args=(fpath, decode_depth),
                    timeout=timeout,
                )
                future.add_done_callback(lambda _f: semaphore.release())
                futures[future] = (fpath, pre_mtime, pre_size)

            for future in as_completed(futures):
                fpath, pre_mtime, pre_size = futures[future]
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
                        continue
                except OSError:
                    stats.toctou_discards += 1
                    continue

                self.db.record(
                    fpath, pre_mtime, pre_size,
                    result.status, scan_id=scan_id,
                    error=result.error,
                    suggested_action=result.suggested_action,
                )
                stats.record(result.status)

            if self._shutdown.is_set():
                pool.stop()
                pool.join()
