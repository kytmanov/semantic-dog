"""Stage 13 tests — background scan manager."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from semanticdog.config import Config
from semanticdog.db import Database
from semanticdog.scanner import ScanProgressSnapshot
from semanticdog.services.scan_manager import ScanManager
from tests.fixtures.generators import make_minimal_jpeg


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=[str(tmp_path)],
        db_path=str(tmp_path / "state.db"),
        workers=1,
        raw_workers=1,
        validation_timeout_s=30,
        force_recheck_days=0,
    )


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state.db")


class TestScanManager:
    def test_start_returns_immediately_and_updates_snapshots(self, cfg, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        manager = ScanManager(cfg, db)

        result = manager.start()

        assert result.accepted is True
        deadline = time.time() + 5
        while manager.last_snapshot() is None and time.time() < deadline:
            time.sleep(0.01)

        assert manager.last_snapshot() is not None
        manager._active_future.result(timeout=5)

    def test_duplicate_start_rejected_while_running(self, cfg, db):
        manager = ScanManager(cfg, db)
        manager._active_future = type("FutureStub", (), {"done": lambda self: False})()
        manager._current_snapshot = ScanProgressSnapshot(
            state="running",
            scan_id="scan-1",
            scope=None,
            discovered_total=0,
            processed=0,
            skipped=0,
            ok=0,
            corrupt=0,
            unreadable=0,
            unsupported=0,
            error=0,
            files_per_sec=0.0,
            eta_s=None,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at=None,
        )

        result = manager.start()

        assert result.accepted is False
        assert result.error == "scan already running"
        assert result.scan_id == "scan-1"

    def test_progress_updates_are_recorded(self, cfg, db):
        manager = ScanManager(cfg, db)
        snapshot = ScanProgressSnapshot(
            state="running",
            scan_id="scan-2",
            scope="/photos",
            discovered_total=10,
            processed=3,
            skipped=1,
            ok=2,
            corrupt=1,
            unreadable=0,
            unsupported=0,
            error=0,
            files_per_sec=2.0,
            eta_s=3.5,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at=None,
        )

        manager._on_progress(snapshot)

        assert manager.current_snapshot() == snapshot
        assert manager.last_snapshot() == snapshot

    def test_failure_snapshot_sets_last_error(self, cfg, db):
        manager = ScanManager(cfg, db)
        snapshot = ScanProgressSnapshot(
            state="failed",
            scan_id="scan-3",
            scope=None,
            discovered_total=1,
            processed=0,
            skipped=0,
            ok=0,
            corrupt=0,
            unreadable=0,
            unsupported=0,
            error=1,
            files_per_sec=0.0,
            eta_s=None,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            last_error="boom",
        )

        manager._on_progress(snapshot)

        assert manager.last_error() == "boom"

    def test_completed_scan_sends_notifications(self, cfg, db, tmp_path):
        manager = ScanManager(cfg, db)

        with patch("semanticdog.services.scan_manager.Notifier.notify", return_value=[]) as notify:
            scan_id = db.create_scan(scope=str(tmp_path))
            db.record(str(tmp_path / "img.jpg"), 1.0, 100, "corrupt", scan_id=scan_id)
            manager._send_notifications(type("Stats", (), {"scan_id": scan_id, "start_time": time.monotonic(), "total": 1})())

        assert notify.call_count == 1

    def test_notifications_only_use_new_rows_for_current_scan(self, cfg, db):
        manager = ScanManager(cfg, db)
        old_scan = db.create_scan(scope="/old")
        db.record("/old.jpg", 1.0, 100, "corrupt", scan_id=old_scan)
        current_scan = db.create_scan(scope="/current")
        db.record("/current.jpg", 1.0, 100, "corrupt", scan_id=current_scan)

        with patch("semanticdog.services.scan_manager.Notifier.notify", return_value=[]) as notify:
            manager._send_notifications(type("Stats", (), {"scan_id": current_scan, "start_time": time.monotonic(), "total": 1})())

        summary = notify.call_args.args[0]
        assert [row["path"] for row in summary.corrupt] == ["/current.jpg"]

    def test_successful_notifications_mark_rows_notified(self, cfg, db):
        manager = ScanManager(cfg, db)
        scan_id = db.create_scan(scope="/photos")
        db.record("/current.jpg", 1.0, 100, "corrupt", scan_id=scan_id)

        with patch("semanticdog.services.scan_manager.Notifier.notify", return_value=[]):
            manager._send_notifications(type("Stats", (), {"scan_id": scan_id, "start_time": time.monotonic(), "total": 1})())

        assert db.get_new_corrupt() == []

    def test_notification_errors_are_recorded(self, cfg, db):
        manager = ScanManager(cfg, db)
        scan_id = db.create_scan(scope="/photos")
        db.record("/current.jpg", 1.0, 100, "corrupt", scan_id=scan_id)
        with patch("semanticdog.services.scan_manager.Notifier.notify", return_value=["SMTP: boom"]):
            manager._send_notifications(type("Stats", (), {"scan_id": scan_id, "start_time": time.monotonic(), "total": 1})())

        assert manager.last_notification_errors() == ["SMTP: boom"]

    def test_shutdown_replaces_idle_executor(self, cfg, db):
        manager = ScanManager(cfg, db)
        original = manager._executor

        manager.shutdown()

        assert manager._executor is not original

    def test_shutdown_ignores_active_scan(self, cfg, db):
        manager = ScanManager(cfg, db)
        original = manager._executor
        manager._active_future = type("FutureStub", (), {"done": lambda self: False})()

        manager.shutdown()

        assert manager._executor is original
