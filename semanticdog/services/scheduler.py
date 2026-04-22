"""Lightweight in-process cron scheduler for HTTP-triggered scans."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from croniter import croniter

if TYPE_CHECKING:
    from semanticdog.config import Config
    from semanticdog.services.scan_manager import ScanManager


@dataclass
class SchedulerState:
    enabled: bool
    cron: str | None
    next_run_at: str | None
    last_run_at: str | None
    last_trigger_result: str | None
    last_error: str | None


class SchedulerService:
    """Run scans on a cron schedule inside the HTTP process."""

    def __init__(self, cfg: "Config", scan_manager: "ScanManager") -> None:
        self._cfg = cfg
        self._scan_manager = scan_manager
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_run_at: datetime | None = None
        self._last_run_at: datetime | None = None
        self._last_trigger_result: str | None = None
        self._last_error: str | None = None
        self._compute_next_run_locked(datetime.now().astimezone())

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="sdog-scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def state(self) -> SchedulerState:
        with self._lock:
            return SchedulerState(
                enabled=bool(self._cfg.schedule),
                cron=self._cfg.schedule or None,
                next_run_at=self._next_run_at.isoformat() if self._next_run_at else None,
                last_run_at=self._last_run_at.isoformat() if self._last_run_at else None,
                last_trigger_result=self._last_trigger_result,
                last_error=self._last_error,
            )

    def update_config(self, cfg: "Config", scan_manager: "ScanManager") -> None:
        with self._lock:
            self._cfg = cfg
            self._scan_manager = scan_manager
            self._last_error = None
            self._compute_next_run_locked(datetime.now().astimezone())

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now().astimezone()
            with self._lock:
                next_run = self._next_run_at
                enabled = bool(self._cfg.schedule)

            if not enabled or next_run is None:
                self._stop_event.wait(5.0)
                continue

            wait_s = (next_run - now).total_seconds()
            if wait_s > 0:
                self._stop_event.wait(min(wait_s, 5.0))
                continue

            self._trigger_due_run(now)

    def _trigger_due_run(self, now: datetime) -> None:
        result_text = "started"
        error_text = None
        try:
            result = self._scan_manager.start(origin="scheduled")
            if not result.accepted:
                result_text = "skipped: scan already running"
        except Exception as e:
            result_text = "failed"
            error_text = str(e)

        with self._lock:
            self._last_run_at = now
            self._last_trigger_result = result_text
            self._last_error = error_text
            self._compute_next_run_locked(now)

    def _compute_next_run_locked(self, base: datetime) -> None:
        if not self._cfg.schedule:
            self._next_run_at = None
            return
        try:
            itr = croniter(self._cfg.schedule, base)
            self._next_run_at = itr.get_next(datetime)
            self._last_error = None
        except Exception as e:
            self._next_run_at = None
            self._last_error = str(e)

    def debug_force_run(self) -> None:
        """Testing hook to execute a scheduled trigger immediately."""
        self._trigger_due_run(datetime.now().astimezone())

    def debug_set_next_run(self, value: datetime | None) -> None:
        """Testing hook to override next run."""
        with self._lock:
            self._next_run_at = value

    def as_dict(self) -> dict[str, Any]:
        state = self.state()
        completed = self._scan_manager.last_run_summary("scheduled")
        last_result = state.last_trigger_result
        if completed is not None:
            if completed.get("state") == "completed":
                issues = int(completed.get("issues") or 0)
                last_result = "completed" if issues == 0 else f"completed with {issues} issue{'s' if issues != 1 else ''}"
            elif completed.get("state") == "failed":
                last_result = completed.get("last_error") or "failed"
            elif completed.get("state") == "interrupted":
                last_result = "interrupted"
        return {
            "enabled": state.enabled,
            "cron": state.cron,
            "next_run_at": state.next_run_at,
            "last_run_at": state.last_run_at,
            "last_trigger_result": last_result,
            "last_error": state.last_error,
        }
