"""Background scan orchestration for the HTTP server."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from semanticdog.scanner import ScanProgressSnapshot, Scanner


@dataclass
class ScanStartResult:
    accepted: bool
    scan_id: str | None = None
    state: str | None = None
    error: str | None = None


class ScanManager:
    """Run at most one scan at a time and expose live progress snapshots."""

    def __init__(self, cfg, db) -> None:
        self._cfg = cfg
        self._db = db
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sdog-scan")
        self._active_future: Future | None = None
        self._current_snapshot: ScanProgressSnapshot | None = None
        self._last_snapshot: ScanProgressSnapshot | None = None
        self._last_error: str | None = None

    def is_running(self) -> bool:
        with self._lock:
            return self._active_future is not None and not self._active_future.done()

    def current_snapshot(self) -> ScanProgressSnapshot | None:
        with self._lock:
            return self._current_snapshot

    def last_snapshot(self) -> ScanProgressSnapshot | None:
        with self._lock:
            return self._last_snapshot

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def start(self, scope: str | None = None) -> ScanStartResult:
        return self._launch(scope=scope, resume_scan_id=None)

    def resume(self, scan_id: str) -> ScanStartResult:
        return self._launch(scope=None, resume_scan_id=scan_id)

    def _launch(self, scope: str | None, resume_scan_id: str | None) -> ScanStartResult:
        with self._lock:
            if self._active_future is not None and not self._active_future.done():
                active_scan_id = self._current_snapshot.scan_id if self._current_snapshot else None
                return ScanStartResult(
                    accepted=False,
                    scan_id=active_scan_id,
                    state=self._current_snapshot.state if self._current_snapshot else "running",
                    error="scan already running",
                )

            self._current_snapshot = None
            self._last_error = None
            future = self._executor.submit(self._run_scan, scope, resume_scan_id)
            self._active_future = future

        return ScanStartResult(accepted=True)

    def _run_scan(self, scope: str | None, resume_scan_id: str | None) -> None:
        try:
            scanner = Scanner(self._cfg, self._db)
            if resume_scan_id:
                scanner.scan(resume_scan_id=resume_scan_id, progress_callback=self._on_progress)
            else:
                paths = [scope] if scope else None
                scanner.scan(paths=paths, progress_callback=self._on_progress)
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            raise

    def _on_progress(self, snapshot: ScanProgressSnapshot) -> None:
        with self._lock:
            self._current_snapshot = snapshot
            self._last_snapshot = snapshot
            if snapshot.state == "failed":
                self._last_error = snapshot.last_error
