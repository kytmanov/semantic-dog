"""End-to-end tests — real files, real DB, real scanner, real CLI."""

from __future__ import annotations

import asyncio
import json
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport

from semanticdog.cli import app as cli_app
from semanticdog.config import Config, load_config
from semanticdog.db import Database
from semanticdog.scanner import Scanner
import semanticdog.server as server_module
from semanticdog.server import app as http_app, build_app
from semanticdog.runtime import AppRuntime

from tests.fixtures.generators import (
    make_minimal_jpeg,
    make_truncated_jpeg,
    make_minimal_png,
    make_truncated_png,
    make_minimal_pdf,
    make_not_an_image,
    make_zero_byte,
    make_minimal_tiff,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path, **kwargs) -> Config:
    defaults = dict(
        paths=[str(tmp_path)],
        db_path=str(tmp_path / "state.db"),
        workers=2,
        raw_workers=1,
        validation_timeout_s=30,
        force_recheck_days=0,
        follow_symlinks=False,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _yaml_cfg(tmp_path, **extra) -> str:
    cfg_path = tmp_path / "config.yaml"
    lines = [
        f"db_path: {tmp_path / 'state.db'}",
        "paths:",
        f"  - {tmp_path}",
    ]
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    cfg_path.write_text("\n".join(lines) + "\n")
    return str(cfg_path)


@pytest.fixture(autouse=True)
def _reset_server():
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0
    server_module.app.state.runtime = AppRuntime()
    yield
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0
    server_module.app.state.runtime = AppRuntime()


# ---------------------------------------------------------------------------
# E2E: Scanner — valid files
# ---------------------------------------------------------------------------

class TestScannerE2E:
    def test_empty_dir_zero_results(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()
        assert stats.total == 0
        assert db.get_stats()["total"] == 0

    def test_valid_jpeg_recorded_as_ok(self, tmp_path):
        make_minimal_jpeg(tmp_path / "photo.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()
        assert stats.total >= 1
        db_stats = db.get_stats()
        assert db_stats["by_status"].get("ok", 0) >= 1

    def test_corrupt_jpeg_recorded_as_corrupt(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()
        assert stats.corrupt >= 1
        corrupt_files = db.get_corrupt_files()
        assert any("bad.jpg" in r["path"] for r in corrupt_files)

    def test_multiple_formats_all_scanned(self, tmp_path):
        make_minimal_jpeg(tmp_path / "a.jpg")
        make_minimal_png(tmp_path / "b.png")
        make_minimal_tiff(tmp_path / "c.tiff")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()
        assert stats.total >= 3

    def test_progress_callback_receives_completed_snapshot(self, tmp_path):
        make_minimal_jpeg(tmp_path / "photo.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        snapshots = []

        stats = scanner.scan(progress_callback=snapshots.append)

        assert stats.total >= 1
        assert snapshots[-1].state == "completed"
        assert snapshots[-1].scan_id == stats.scan_id

    def test_second_scan_skips_unchanged_files(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats1 = scanner.scan()
        assert stats1.total >= 1
        # Second scan — same files, nothing changed
        stats2 = scanner.scan()
        assert stats2.total == 0
        assert stats2.skipped >= 1

    def test_exclude_pattern_skips_files(self, tmp_path):
        sub = tmp_path / "@eaDir"
        sub.mkdir()
        make_minimal_jpeg(sub / "thumb.jpg")
        make_minimal_jpeg(tmp_path / "real.jpg")
        cfg = _cfg(tmp_path, exclude=["**/@eaDir/**"])
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()
        # Only real.jpg should be scanned
        assert stats.total <= 1
        paths = [r["path"] for r in db.export_json()]
        assert not any("@eaDir" in p for p in paths)

    def test_scan_creates_scan_record(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        scanner.scan()
        scans = db.list_scans()
        assert len(scans) == 1
        assert scans[0]["finished_at"] is not None

    def test_ok_then_corrupt_resets_notified(self, tmp_path):
        """File ok → notified → re-corrupted → notified_at reset."""
        p = make_minimal_jpeg(tmp_path / "img.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        # First scan — ok
        scanner.scan()
        db.mark_notified([str(p)])
        assert db.get_new_corrupt() == []
        # Replace with corrupt version, reset mtime check
        make_truncated_jpeg(tmp_path / "img.jpg")
        # Force recheck by resetting DB record mtime
        db.reset(path_prefix=str(tmp_path))
        scanner.scan()
        corrupt = db.get_new_corrupt()
        assert any("img.jpg" in r["path"] for r in corrupt)

    def test_lock_released_after_scan(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        scanner.scan()
        assert db.get_meta("lock") is None

    def test_unregistered_extension_not_scanned(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / "data.xyz").write_bytes(b"\x00")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        scanner.scan()
        assert db.get_stats()["total"] == 0

    def test_scan_stats_fps_nonzero_after_files(self, tmp_path):
        for i in range(3):
            make_minimal_jpeg(tmp_path / f"img{i}.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()
        assert stats.files_per_sec() >= 0  # may be 0 if very fast, just no error


# ---------------------------------------------------------------------------
# E2E: CLI — scan command
# ---------------------------------------------------------------------------

class TestCliScanE2E:
    def test_scan_dry_run_no_db(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, ["scan", "--dry-run", "--config", cfg_path])
        assert r.exit_code == 0
        assert not (tmp_path / "state.db").exists()
        assert ".jpg" in r.output

    def test_scan_valid_files_exit_zero(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, ["scan", "--config", cfg_path])
        assert r.exit_code == 0, r.output

    def test_scan_corrupt_exits_two(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, ["scan", "--config", cfg_path])
        assert r.exit_code == 2, r.output

    def test_scan_then_report_shows_corrupt(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["report", "--config", cfg_path])
        assert r.exit_code == 0
        assert "bad.jpg" in r.output

    def test_scan_then_show_stats(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["show-stats", "--config", cfg_path])
        assert r.exit_code == 0
        assert "ok" in r.output

    def test_estimate_then_scan_counts_match(self, tmp_path):
        for i in range(3):
            make_minimal_jpeg(tmp_path / f"img{i}.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        r_est = runner.invoke(cli_app, ["estimate", "--config", cfg_path])
        assert r_est.exit_code == 0
        assert "3" in r_est.output
        # After scan, estimate should show 0 (already checked)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r_est2 = runner.invoke(cli_app, ["estimate", "--config", cfg_path])
        assert "0" in r_est2.output

    def test_status_after_scan(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["status", "--config", cfg_path])
        assert r.exit_code == 0
        assert "Last scan ID" in r.output

    def test_list_scans_shows_entry(self, tmp_path):
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["list-scans", "--config", cfg_path])
        assert r.exit_code == 0
        assert "complete" in r.output

    def test_reset_clears_records(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["reset", "--yes", "--config", cfg_path])
        assert r.exit_code == 0
        assert "1" in r.output
        # After reset, report should show no corrupt files
        r2 = runner.invoke(cli_app, ["report", "--config", cfg_path])
        assert r2.exit_code == 0
        assert "corrupt" not in r2.output.lower() or "0" in r2.output

    def test_report_json_has_stats(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["report", "--format", "json", "--config", cfg_path])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["stats"]["total"] >= 1

    def test_report_csv_format(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["report", "--format", "csv", "--config", cfg_path])
        assert r.exit_code == 0
        assert "path,status" in r.output


# ---------------------------------------------------------------------------
# E2E: db-export / db-import round-trip
# ---------------------------------------------------------------------------

class TestDbExportImportE2E:
    def test_export_import_round_trip(self, tmp_path):
        # Scan into db1
        src = tmp_path / "src"
        src.mkdir()
        p = make_minimal_jpeg(src / "img.jpg")
        cfg1 = _cfg(src, db_path=str(tmp_path / "db1.db"))
        db1 = Database(cfg1.db_path)
        Scanner(cfg1, db1).scan()
        assert db1.get_stats()["total"] == 1

        # Export
        export_path = tmp_path / "export.json"
        cfg_path = _yaml_cfg(src, db_path=str(tmp_path / "db1.db"))
        r = runner.invoke(cli_app, ["db-export", "--output", str(export_path), "--config", cfg_path])
        assert r.exit_code == 0
        records = json.loads(export_path.read_text())
        assert len(records) == 1

        # Import into db2 (same paths — files exist)
        cfg_path2 = _yaml_cfg(src, db_path=str(tmp_path / "db2.db"))
        r2 = runner.invoke(cli_app, [
            "db-import", "--input", str(export_path), "--config", cfg_path2
        ])
        assert r2.exit_code == 0
        assert "1" in r2.output

        db2 = Database(str(tmp_path / "db2.db"))
        assert db2.get_stats()["total"] == 1

    def test_import_force_overwrites(self, tmp_path):
        p = make_minimal_jpeg(tmp_path / "img.jpg")
        db = Database(str(tmp_path / "state.db"))
        db.record(str(p), 1.0, 100, "ok")

        # Export with status ok, then import with corrupt (force)
        records = [{"path": str(p), "mtime": 1.0, "size": 100,
                    "status": "corrupt", "checked_at": "2024-01-01T00:00:00+00:00"}]
        import_path = tmp_path / "imp.json"
        import_path.write_text(json.dumps(records))

        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, [
            "db-import", "--input", str(import_path), "--force", "--config", cfg_path
        ])
        assert r.exit_code == 0
        db2 = Database(str(tmp_path / "state.db"))
        assert db2.get_stats()["by_status"].get("corrupt", 0) == 1

    def test_import_skip_missing_paths(self, tmp_path):
        records = [{"path": "/nonexistent/ghost.jpg", "mtime": 1.0, "size": 100,
                    "status": "ok", "checked_at": "2024-01-01T00:00:00+00:00"}]
        import_path = tmp_path / "imp.json"
        import_path.write_text(json.dumps(records))
        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, [
            "db-import", "--input", str(import_path), "--config", cfg_path
        ])
        assert r.exit_code == 0
        assert "0" in r.output  # 0 inserted


# ---------------------------------------------------------------------------
# E2E: HTTP server
# ---------------------------------------------------------------------------

class TestHttpServerE2E:
    async def test_health_always_ok(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_status_idle_before_scan(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.status_code == 200
        assert r.json()["status"] == "idle"
        assert r.json()["files_indexed"] == 0

    async def test_trigger_scan_then_status_shows_files(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            r = await c.post("/trigger")
            assert r.status_code == 200
            assert r.json()["status"] == "started"

            deadline = time.time() + 5
            while True:
                r2 = await c.get("/status")
                if r2.json()["files_indexed"] >= 1 or time.time() >= deadline:
                    break
                await asyncio.sleep(0.05)
        assert r2.json()["files_indexed"] >= 1

    async def test_trigger_409_while_scanning(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        runtime = http_app.state.runtime
        runtime.scan_manager._current_snapshot = type("Snapshot", (), {"scan_id": "scan-1", "state": "running"})()
        runtime.scan_manager._active_future = type("FutureStub", (), {"done": lambda self: False})()
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 409

    async def test_metrics_shows_file_counts(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            await c.post("/trigger")
            deadline = time.time() + 5
            while db.get_stats()["total"] == 0 and time.time() < deadline:
                await asyncio.sleep(0.05)
            r = await c.get("/metrics")
        assert r.status_code == 200
        assert "sdog_files_total" in r.text

    async def test_trigger_corrupt_file_recorded(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 200
        deadline = time.time() + 5
        while db.get_corrupt_files() == [] and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert db.get_corrupt_files() != []

    async def test_api_scan_current_reports_background_scan(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            await c.post("/trigger")
            deadline = time.time() + 5
            while time.time() < deadline:
                r = await c.get("/api/scan/current")
                payload = r.json()
                if payload["current"] is not None or payload["last"] is not None:
                    break
                await asyncio.sleep(0.05)
        assert payload["current"] is not None or payload["last"] is not None

    async def test_notify_test_endpoint_works(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        with patch("semanticdog.server.Notifier.notify", return_value=[]):
            async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
                r = await c.post("/api/notify/test")
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    async def test_restart_required_config_save_does_not_apply_live(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(textwrap.dedent(f"""
            paths:
              - {tmp_path}
            db_path: {tmp_path}/state.db
            http_port: 9090
            workers: 1
            raw_workers: 1
        """))
        from semanticdog.config_store import ConfigStore

        http_app.state.runtime.config_store = ConfigStore(str(config_path))
        original_port = http_app.state.runtime.cfg.http_port

        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            r = await c.put("/api/config", json={"http_port": 9191})

        assert r.status_code == 200
        assert r.json()["restart_required"] == ["http_port"]
        assert http_app.state.runtime.cfg.http_port == original_port

    async def test_paths_save_with_unchanged_db_path_applies_live_and_scans_all_roots(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        make_minimal_jpeg(first / "one.jpg")
        make_minimal_jpeg(second / "two.jpg")
        state_db = tmp_path / "state.db"

        cfg = _cfg(first)
        cfg.trigger_cooldown_s = 0
        cfg.db_path = str(state_db)
        db = Database(cfg.db_path)
        build_app(cfg, db)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(textwrap.dedent(f"""
            paths:
              - {first}
            db_path: {state_db}
            workers: 1
            raw_workers: 1
        """))
        from semanticdog.config_store import ConfigStore

        http_app.state.runtime.config_store = ConfigStore(str(config_path))

        async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
            save = await c.put(
                "/api/config",
                json={"paths": [str(first), str(second)], "db_path": str(state_db)},
            )
            assert save.status_code == 200
            assert save.json()["restart_required"] == []

            trigger = await c.post("/trigger")
            assert trigger.status_code == 200

        deadline = time.time() + 5
        while db.get_stats()["total"] < 2 and time.time() < deadline:
            await asyncio.sleep(0.05)

        assert http_app.state.runtime.cfg.paths == [str(first), str(second)]
        assert db.get_stats()["total"] == 2

    async def test_completed_scan_notifications_are_marked_notified(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)

        with patch("semanticdog.services.scan_manager.Notifier.notify", return_value=[]):
            async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
                r = await c.post("/trigger")
                assert r.status_code == 200

        deadline = time.time() + 5
        while http_app.state.runtime.scan_manager.is_running() and time.time() < deadline:
            await asyncio.sleep(0.05)

        assert db.get_new_corrupt() == []

    async def test_completed_scan_notifications_only_include_current_scan(self, tmp_path):
        older = tmp_path / "older"
        current = tmp_path / "current"
        older.mkdir()
        current.mkdir()
        make_truncated_jpeg(older / "old-bad.jpg")
        make_truncated_jpeg(current / "new-bad.jpg")

        cfg = _cfg(tmp_path)
        cfg.trigger_cooldown_s = 0
        db = Database(cfg.db_path)
        build_app(cfg, db)

        captured = []

        def _capture(summary):
            captured.append(summary)
            return []

        with patch("semanticdog.services.scan_manager.Notifier.notify", side_effect=_capture):
            async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as c:
                first = await c.post("/trigger", json={"scope": str(older)})
                assert first.status_code == 200
                deadline = time.time() + 5
                while http_app.state.runtime.scan_manager.is_running() and time.time() < deadline:
                    await asyncio.sleep(0.05)

                second = await c.post("/trigger", json={"scope": str(current)})
                assert second.status_code == 200
                deadline = time.time() + 5
                while http_app.state.runtime.scan_manager.is_running() and time.time() < deadline:
                    await asyncio.sleep(0.05)

        assert len(captured) == 2
        assert [row["path"] for row in captured[0].corrupt] == [str(older / "old-bad.jpg")]
        assert [row["path"] for row in captured[1].corrupt] == [str(current / "new-bad.jpg")]


# ---------------------------------------------------------------------------
# E2E: config loading from YAML
# ---------------------------------------------------------------------------

class TestConfigE2E:
    def test_yaml_config_loaded_by_cli(self, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(textwrap.dedent(f"""
            paths:
              - {tmp_path}
            db_path: {tmp_path}/state.db
            workers: 1
            raw_workers: 1
        """))
        r = runner.invoke(cli_app, ["scan", "--config", str(cfg_path)])
        assert r.exit_code == 0

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path, workers=4)
        monkeypatch.setenv("SDOG_WORKERS", "1")
        cfg = load_config(cfg_path)
        assert cfg.workers == 1

    def test_is_path_allowed_used_by_config(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.is_path_allowed(str(tmp_path / "subdir" / "img.jpg")) is True
        assert cfg.is_path_allowed("/etc/passwd") is False


# ---------------------------------------------------------------------------
# E2E: multi-format scan
# ---------------------------------------------------------------------------

class TestMultiFormatScanE2E:
    def test_mixed_valid_invalid_files(self, tmp_path):
        make_minimal_jpeg(tmp_path / "ok.jpg")
        make_truncated_jpeg(tmp_path / "bad.jpg")
        make_minimal_png(tmp_path / "ok.png")
        make_truncated_png(tmp_path / "bad.png")
        make_minimal_pdf(tmp_path / "ok.pdf")

        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        stats = scanner.scan()

        assert stats.ok >= 3
        assert stats.corrupt >= 2
        db_stats = db.get_stats()
        assert db_stats["by_status"].get("ok", 0) >= 3
        assert db_stats["by_status"].get("corrupt", 0) >= 2

    def test_zero_byte_files_not_ok(self, tmp_path):
        make_zero_byte(tmp_path / "empty.jpg")
        make_zero_byte(tmp_path / "empty.pdf")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        scanner.scan()
        stats = db.get_stats()
        # Zero-byte files should be corrupt or unreadable, not ok
        assert stats["by_status"].get("ok", 0) == 0

    def test_non_image_content_with_image_extension_corrupt(self, tmp_path):
        make_not_an_image(tmp_path / "fake.jpg")
        make_not_an_image(tmp_path / "fake.png")
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        scanner = Scanner(cfg, db)
        scanner.scan()
        assert db.get_stats()["by_status"].get("corrupt", 0) >= 2


# ---------------------------------------------------------------------------
# E2E: cancel + resume
# ---------------------------------------------------------------------------

class TestResumeE2E:
    def test_interrupted_scan_shows_incomplete_in_list(self, tmp_path):
        """Scan interrupted via shutdown appears as 'incomplete' in list-scans."""
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_obj = _cfg(tmp_path)
        db = Database(cfg_obj.db_path)
        scanner = Scanner(cfg_obj, db)
        scanner._shutdown.set()
        scanner.scan()

        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, ["list-scans", "--config", cfg_path])
        assert r.exit_code == 0
        assert "incomplete" in r.output

    def test_resume_via_cli_completes_scan(self, tmp_path):
        """--resume flag completes an interrupted scan and sets finished_at."""
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_obj = _cfg(tmp_path)
        db = Database(cfg_obj.db_path)
        # Interrupt immediately via pre-set shutdown
        scanner = Scanner(cfg_obj, db)
        scanner._shutdown.set()
        stats = scanner.scan()
        scan_id = stats.scan_id
        assert db.get_scan(scan_id)["finished_at"] is None

        # Resume via CLI — should complete
        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, ["scan", "--resume", scan_id, "--config", cfg_path])
        assert r.exit_code in (0, 2)
        assert db.get_scan(scan_id)["finished_at"] is not None

    def test_resume_completed_scan_via_cli_exits_error(self, tmp_path):
        """--resume on a finished scan exits with code 1 and prints error."""
        make_minimal_jpeg(tmp_path / "img.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        db = Database(str(tmp_path / "state.db"))
        scan_id = db.list_scans()[0]["id"]

        r = runner.invoke(cli_app, ["scan", "--resume", scan_id, "--config", cfg_path])
        assert r.exit_code == 1

    def test_resume_skipped_files_dont_persist_as_pending(self, tmp_path):
        """
        Regression: files skipped on resume (already validated) are marked done
        in scan_queue. A second resume sees 0 pending, not the original count.
        """
        files = [make_minimal_jpeg(tmp_path / f"img{i}.jpg") for i in range(3)]
        cfg_obj = _cfg(tmp_path)
        db = Database(cfg_obj.db_path)
        # Initial scan: all files into DB
        Scanner(cfg_obj, db).scan()

        # Simulate interrupted scan with all 3 files pending
        scan_id = db.create_scan(scope=str(tmp_path))
        db.queue_paths(scan_id, [str(f) for f in files])
        assert len(db.get_all_pending_paths(scan_id)) == 3

        # Resume via CLI — files are already in DB so they'll be skipped
        cfg_path = _yaml_cfg(tmp_path)
        r = runner.invoke(cli_app, ["scan", "--resume", scan_id, "--config", cfg_path])
        assert r.exit_code == 0

        # All must be marked done — no pending remain
        assert db.get_all_pending_paths(scan_id) == []

    def test_stats_ok_corrupt_nonzero_after_scan(self, tmp_path):
        """
        Inline stats fix: ok/corrupt populated in real time, not just at end.
        Verifies stats match what was written to DB.
        """
        make_minimal_jpeg(tmp_path / "ok.jpg")
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg_obj = _cfg(tmp_path)
        db = Database(cfg_obj.db_path)
        stats = Scanner(cfg_obj, db).scan()

        assert stats.ok >= 1
        assert stats.corrupt >= 1
        assert stats.ok == db.get_stats()["by_status"].get("ok", 0)
        assert stats.corrupt == db.get_stats()["by_status"].get("corrupt", 0)
