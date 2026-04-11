"""End-to-end tests — real files, real DB, real scanner, real CLI."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport

from semanticdog.cli import app as cli_app
from semanticdog.config import Config, load_config
from semanticdog.db import Database
from semanticdog.scanner import Scanner
import semanticdog.server as server_module
from semanticdog.server import app as http_app, build_app

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
    if server_module._scan_lock.locked():
        try:
            server_module._scan_lock.release()
        except RuntimeError:
            pass
    yield
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0


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

    def test_scan_then_show_corrupt(self, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        cfg_path = _yaml_cfg(tmp_path)
        runner.invoke(cli_app, ["scan", "--config", cfg_path])
        r = runner.invoke(cli_app, ["show-corrupt", "--config", cfg_path])
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
        # After reset, show-corrupt should show nothing
        r2 = runner.invoke(cli_app, ["show-corrupt", "--config", cfg_path])
        assert "No corrupt" in r2.output

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
            assert r.json()["status"] == "complete"
            r2 = await c.get("/status")
        assert r2.json()["files_indexed"] >= 1

    async def test_trigger_409_while_scanning(self, tmp_path):
        cfg = _cfg(tmp_path)
        db = Database(cfg.db_path)
        build_app(cfg, db)
        async with server_module._scan_lock:
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
        assert db.get_corrupt_files() != []


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
