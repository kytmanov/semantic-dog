"""Background scan orchestration for the HTTP server."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from semanticdog.notify import Notifier, ScanSummary
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
        self._last_notification_errors: list[str] = []

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

    def last_notification_errors(self) -> list[str]:
        with self._lock:
            return list(self._last_notification_errors)

    def start(self, scope: str | None = None) -> ScanStartResult:
        return self._launch(scope=scope, resume_scan_id=None)

    def resume(self, scan_id: str) -> ScanStartResult:
        return self._launch(scope=None, resume_scan_id=scan_id)

    def shutdown(self) -> None:
        with self._lock:
            future = self._active_future
            if future is not None and not future.done():
                return
            executor = self._executor
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sdog-scan")
        executor.shutdown(wait=False)

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
                stats = scanner.scan(resume_scan_id=resume_scan_id, progress_callback=self._on_progress)
            else:
                paths = [scope] if scope else None
                stats = scanner.scan(paths=paths, progress_callback=self._on_progress)
            self._send_notifications(stats)
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            raise

    def _send_notifications(self, stats) -> None:
        if stats is None or not stats.scan_id:
            return
        scan = self._db.get_scan(stats.scan_id)
        if scan is None:
            return

        duration_s = max(time.monotonic() - getattr(stats, "start_time", time.monotonic()), 0.0)
        started_at = scan.get("started_at")
        finished_at = scan.get("finished_at")
        if started_at and finished_at:
            try:
                duration_s = max(
                    (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds(),
                    0.0,
                )
            except ValueError:
                pass

        corrupt = [row for row in self._db.get_new_corrupt() if row.get("scan_id") == stats.scan_id][:50]
        unreadable = [row for row in self._db.get_new_unreadable() if row.get("scan_id") == stats.scan_id][:50]
        if not corrupt and not unreadable:
            with self._lock:
                self._last_notification_errors = []
            return

        summary = ScanSummary(
            scan_id=stats.scan_id,
            scope=scan.get("scope") or ",".join(self._cfg.paths),
            duration_s=duration_s,
            total_checked=stats.total,
            corrupt=corrupt,
            unreadable=unreadable,
        )
        errors = Notifier(self._cfg).notify(summary)
        if not errors:
            self._db.mark_notified([row["path"] for row in corrupt + unreadable])
        with self._lock:
            self._last_notification_errors = errors

    def _on_progress(self, snapshot: ScanProgressSnapshot) -> None:
        with self._lock:
            self._current_snapshot = snapshot
            self._last_snapshot = snapshot
            if snapshot.state == "failed":
                self._last_error = snapshot.last_error
