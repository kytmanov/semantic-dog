from __future__ import annotations

from datetime import datetime

from semanticdog.config import Config
from semanticdog.db import Database
from semanticdog.services.scan_manager import ScanManager
from semanticdog.services.scheduler import SchedulerService


def test_scheduler_exposes_next_run_state(tmp_path):
    cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="*/5 * * * *")
    db = Database(cfg.db_path)
    scheduler = SchedulerService(cfg, ScanManager(cfg, db))

    state = scheduler.state()

    assert state.enabled is True
    assert state.cron == "*/5 * * * *"
    assert state.next_run_at is not None


def test_scheduler_can_be_disabled(tmp_path):
    cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="")
    db = Database(cfg.db_path)
    scheduler = SchedulerService(cfg, ScanManager(cfg, db))

    state = scheduler.state()

    assert state.enabled is False
    assert state.next_run_at is None


def test_scheduler_triggers_scan_manager(tmp_path):
    cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="* * * * *")
    db = Database(cfg.db_path)
    manager = ScanManager(cfg, db)
    calls: list[tuple[str, str]] = []

    def fake_start(scope=None, *, origin="manual"):
        calls.append((scope or "all", origin))
        return type("StartResult", (), {"accepted": True})()

    manager.start = fake_start
    scheduler = SchedulerService(cfg, manager)
    scheduler.debug_force_run()

    assert calls == [("all", "scheduled")]
    assert scheduler.state().last_trigger_result == "started"
    assert scheduler.state().last_run_at is not None


def test_scheduler_reports_completed_scheduled_run(tmp_path):
    cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="* * * * *")
    db = Database(cfg.db_path)
    manager = ScanManager(cfg, db)
    scheduler = SchedulerService(cfg, manager)

    manager._last_run_summaries["scheduled"] = {
        "state": "completed",
        "scan_id": "scan-1",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:04+00:00",
        "processed": 12,
        "issues": 0,
        "last_error": None,
    }

    assert scheduler.as_dict()["last_trigger_result"] == "completed"


def test_scheduler_updates_next_run_when_config_changes(tmp_path):
    cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="0 2 * * *")
    db = Database(cfg.db_path)
    manager = ScanManager(cfg, db)
    scheduler = SchedulerService(cfg, manager)
    first = scheduler.state().next_run_at

    new_cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="*/10 * * * *")
    scheduler.update_config(new_cfg, manager)

    assert scheduler.state().next_run_at != first


def test_scheduler_records_invalid_cron(tmp_path):
    cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), schedule="not-a-cron")
    db = Database(cfg.db_path)
    scheduler = SchedulerService(cfg, ScanManager(cfg, db))

    state = scheduler.state()

    assert state.next_run_at is None
    assert state.last_error is not None
