"""Stage 12 tests — HTTP server (FastAPI)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from semanticdog import __version__
import semanticdog.server as server_module
from semanticdog.server import app, build_app, create_app
from semanticdog.config import Config
from semanticdog.config_store import ConfigStore
from semanticdog.db import Database
from semanticdog.runtime import AppRuntime
from semanticdog.scanner import ScanProgressSnapshot
from semanticdog.services.scan_manager import ScanManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_server_state():
    """Reset module-level state between tests."""
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0
    server_module.app.state.runtime = AppRuntime()
    yield
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0
    server_module.app.state.runtime = AppRuntime()


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state.db")


@pytest.fixture
def cfg(tmp_path):
    return Config(paths=[str(tmp_path)], workers=1, raw_workers=1)


@pytest.fixture
def configured_app(cfg, db):
    return build_app(cfg, db)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    async def test_health_returns_ok(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_favicon_returns_empty_success(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/favicon.ico")
        assert r.status_code == 204

    async def test_health_stays_public_with_auth_enabled(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            workers=1,
            raw_workers=1,
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        auth_app = build_app(cfg, Database(cfg.db_path))
        async with AsyncClient(transport=ASGITransport(app=auth_app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200

    async def test_ready_returns_503_for_unconfigured_runtime(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/ready")
        assert r.status_code == 503
        assert r.json()["ready"] is False

    async def test_ready_returns_200_for_usable_runtime(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), workers=1, raw_workers=1)
        ready_app = build_app(cfg, Database(cfg.db_path))
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"paths:\n  - {tmp_path}\n")
        ready_app.state.runtime.config_path = str(config_path)

        async with AsyncClient(transport=ASGITransport(app=ready_app), base_url="http://test") as c:
            r = await c.get("/ready")
        assert r.status_code == 200
        assert r.json()["ready"] is True

    async def test_ready_stays_public_with_auth_enabled(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            workers=1,
            raw_workers=1,
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        auth_app = build_app(cfg, Database(cfg.db_path))
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"paths:\n  - {tmp_path}\n")
        auth_app.state.runtime.config_path = str(config_path)

        async with AsyncClient(transport=ASGITransport(app=auth_app), base_url="http://test") as c:
            r = await c.get("/ready")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

class TestMetricsEndpoint:
    async def test_metrics_returns_text(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    async def test_metrics_with_db_shows_stats(self, configured_app, db):
        db.record("/img.jpg", 1.0, 100, "ok")
        db.record("/bad.jpg", 1.0, 100, "corrupt")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/metrics")
        assert r.status_code == 200
        assert "sdog_files_total" in r.text
        assert 'status="ok"' in r.text

    async def test_metrics_ends_with_end(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/metrics")
        assert "END" in r.text


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    async def test_status_unconfigured(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.status_code == 200
        assert r.json()["status"] == "unconfigured"

    async def test_status_idle(self, configured_app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.status_code == 200
        assert r.json()["status"] == "idle"

    async def test_status_reports_current_scan(self, configured_app, tmp_path):
        runtime = app.state.runtime
        snapshot = ScanProgressSnapshot(
            state="running",
            scan_id="scan-1",
            scope=str(tmp_path),
            discovered_total=10,
            processed=2,
            skipped=0,
            ok=2,
            corrupt=0,
            unreadable=0,
            unsupported=0,
            error=0,
            files_per_sec=1.5,
            eta_s=5.0,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at=None,
        )
        runtime.scan_manager._current_snapshot = snapshot

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")

        assert r.status_code == 200
        assert r.json()["status"] == "scanning"
        assert r.json()["current_scan"]["scan_id"] == "scan-1"

    async def test_status_degraded(self):
        degraded_app = create_app(AppRuntime(config_error="bad config"))
        async with AsyncClient(transport=ASGITransport(app=degraded_app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"
        assert r.json()["config_error"] == "bad config"

    async def test_status_includes_file_count(self, configured_app, db):
        db.record("/img.jpg", 1.0, 100, "ok")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.json()["files_indexed"] == 1

    async def test_status_includes_scheduler_state(self, configured_app):
        async with AsyncClient(transport=ASGITransport(app=configured_app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.status_code == 200
        assert "scheduler" in r.json()
        assert r.json()["scheduler"]["enabled"] is True

    async def test_status_includes_file_type_breakdown(self, configured_app, db):
        db.record("/img.jpg", 1.0, 100, "ok")
        db.record("/clip.mp4", 1.0, 100, "ok")
        db.record("/other.jpg", 1.0, 100, "ok")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")

        assert r.status_code == 200
        assert r.json()["file_types"][0]["label"] == "JPG"
        assert r.json()["file_types"][0]["count"] == 2
        assert r.json()["file_types"][1]["label"] == "MP4"

    async def test_status_includes_overview_breakdown(self, configured_app, db):
        db.record("/good-a.jpg", 1.0, 100, "ok")
        db.record("/good-b.jpg", 1.0, 100, "ok")
        db.record("/bad.jpg", 1.0, 100, "corrupt")
        db.record("/locked.jpg", 1.0, 100, "unreadable")
        db.record("/clip.mp4", 1.0, 100, "ok")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")

        assert r.status_code == 200
        assert r.json()["overview_breakdown"] == [
            {
                "key": ".jpg:ok",
                "label": "Healthy JPG",
                "ext": ".jpg",
                "status": "ok",
                "count": 2,
                "tone": "healthy",
            },
            {
                "key": ".jpg:corrupt",
                "label": "Corrupt JPG",
                "ext": ".jpg",
                "status": "corrupt",
                "count": 1,
                "tone": "corrupt",
            },
            {
                "key": ".jpg:unreadable",
                "label": "Unreadable JPG",
                "ext": ".jpg",
                "status": "unreadable",
                "count": 1,
                "tone": "unreadable",
            },
            {
                "key": ".mp4:ok",
                "label": "Healthy MP4",
                "ext": ".mp4",
                "status": "ok",
                "count": 1,
                "tone": "healthy",
            },
        ]


class TestApiEndpoints:
    async def test_api_app_returns_runtime_state(self, configured_app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/app")
        assert r.status_code == 200
        assert r.json()["ready"] is True
        assert "readiness" in r.json()
        assert r.json()["version"] == __version__

    async def test_openapi_uses_package_version(self, configured_app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/openapi.json")
        assert r.status_code == 200
        assert r.json()["info"]["version"] == __version__

    async def test_api_config_returns_sources_and_effective_values(self, configured_app, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\nworkers: 3\n")
        app.state.runtime.config_store = __import__("semanticdog.config_store", fromlist=["ConfigStore"]).ConfigStore(str(config_path))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/config")

        assert r.status_code == 200
        assert r.json()["raw"]["workers"] == 3
        assert r.json()["sources"]["workers"] == "yaml"

    async def test_api_config_validate_accepts_schedule_field(self, configured_app, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\n")
        app.state.runtime.config_store = ConfigStore(str(config_path))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/config/validate", json={"schedule": "* * * * *"})

        assert r.status_code == 200
        assert r.json()["valid"] is True

    async def test_api_config_save_persists_editable_fields(self, configured_app, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\nfuture_key: 42\n")
        app.state.runtime.config_store = __import__("semanticdog.config_store", fromlist=["ConfigStore"]).ConfigStore(str(config_path))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put("/api/config", json={"workers": 7})

        assert r.status_code == 200
        assert r.json()["status"] == "saved"
        assert "future_key: 42" in config_path.read_text()

    async def test_api_config_save_does_not_apply_restart_required_fields_live(self, configured_app, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"paths:\n  - {tmp_path}\nhttp_port: 8181\n")
        app.state.runtime.config_store = __import__("semanticdog.config_store", fromlist=["ConfigStore"]).ConfigStore(str(config_path))
        original_port = app.state.runtime.cfg.http_port

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put("/api/config", json={"http_port": 9191})

        assert r.status_code == 200
        assert r.json()["restart_required"] == ["http_port"]
        assert app.state.runtime.cfg.http_port == original_port

    async def test_api_config_save_applies_paths_live_when_restart_field_is_unchanged(self, configured_app, tmp_path):
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        state_db = tmp_path / "state.db"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"paths:\n  - {first}\ndb_path: {state_db}\nworkers: 1\nraw_workers: 1\n"
        )
        app.state.runtime.config_store = __import__("semanticdog.config_store", fromlist=["ConfigStore"]).ConfigStore(str(config_path))
        app.state.runtime.cfg.db_path = str(state_db)
        app.state.runtime.cfg.paths = [str(first)]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put(
                "/api/config",
                json={"paths": [str(first), str(second)], "db_path": str(state_db)},
            )

        assert r.status_code == 200
        assert r.json()["restart_required"] == []
        assert app.state.runtime.cfg.paths == [str(first), str(second)]
        assert app.state.runtime.cfg.db_path == str(state_db)

    async def test_api_config_save_blocked_while_scan_running(self, configured_app):
        runtime = app.state.runtime
        runtime.scan_manager._active_future = MagicMock(done=MagicMock(return_value=False))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put("/api/config", json={"workers": 7})

        assert r.status_code == 409

    async def test_api_setup_returns_diagnostics(self, configured_app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/setup")
        assert r.status_code == 200
        assert "scan_roots" in r.json()
        assert "dependencies" in r.json()

    async def test_api_scan_current_returns_snapshot(self, configured_app, tmp_path):
        runtime = app.state.runtime
        runtime.scan_manager._current_snapshot = ScanProgressSnapshot(
            state="running",
            scan_id="scan-42",
            scope=str(tmp_path),
            discovered_total=4,
            processed=1,
            skipped=0,
            ok=1,
            corrupt=0,
            unreadable=0,
            unsupported=0,
            error=0,
            files_per_sec=1.0,
            eta_s=3.0,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at=None,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/scan/current")
        assert r.status_code == 200
        assert r.json()["current"]["scan_id"] == "scan-42"

    async def test_api_notify_test_returns_result(self, configured_app):
        with patch("semanticdog.server.Notifier.notify", return_value=[]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/api/notify/test")
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    async def test_api_scans_returns_history(self, configured_app, db):
        scan_id = db.create_scan(scope="/photos")
        db.finish_scan(scan_id, total=1, corrupt=0, unreadable=0, files_per_sec=1.0)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/scans")
        assert r.status_code == 200
        assert r.json()["scans"][0]["id"] == scan_id

    async def test_api_scans_by_id_returns_scan(self, configured_app, db):
        scan_id = db.create_scan(scope="/photos")
        db.finish_scan(scan_id, total=1, corrupt=0, unreadable=0, files_per_sec=1.0)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/scans/{scan_id}")
        assert r.status_code == 200
        assert r.json()["id"] == scan_id

    async def test_api_scans_by_id_returns_404(self, configured_app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/scans/missing")
        assert r.status_code == 404

    async def test_api_issues_returns_corrupt_and_unreadable(self, configured_app, db):
        db.record("/good.jpg", 1.0, 100, "ok")
        db.record("/bad.jpg", 1.0, 100, "corrupt")
        db.record("/blocked.jpg", 1.0, 100, "unreadable")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/issues")
        assert r.status_code == 200
        assert {issue["path"] for issue in r.json()["issues"]} == {"/bad.jpg", "/blocked.jpg"}

    async def test_api_issues_filters_by_status(self, configured_app, db):
        db.record("/bad.jpg", 1.0, 100, "corrupt")
        db.record("/blocked.jpg", 1.0, 100, "unreadable")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/issues", params={"status": "corrupt"})
        assert r.status_code == 200
        assert [issue["path"] for issue in r.json()["issues"]] == ["/bad.jpg"]


# ---------------------------------------------------------------------------
# /trigger
# ---------------------------------------------------------------------------

class TestTriggerEndpoint:
    async def test_trigger_returns_409_when_locked(self, configured_app):
        snapshot = ScanProgressSnapshot(
            state="running",
            scan_id="scan-1",
            scope=None,
            discovered_total=1,
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
        runtime = app.state.runtime
        runtime.scan_manager._current_snapshot = snapshot
        runtime.scan_manager._active_future = MagicMock(done=MagicMock(return_value=False))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 409
        assert r.json()["error"] == "scan already running"

    async def test_trigger_returns_503_when_unconfigured(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 503

    async def test_trigger_returns_503_with_runtime_errors(self):
        degraded_app = create_app(AppRuntime(config_error="bad config"))
        async with AsyncClient(transport=ASGITransport(app=degraded_app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 503
        assert r.json()["config_error"] == "bad config"

    async def test_trigger_runs_scan(self, configured_app, tmp_path, cfg):
        result = {"accepted": True, "scan_id": None}

        def _fake_start(scope=None):
            app.state.runtime.scan_manager._current_snapshot = ScanProgressSnapshot(
                state="starting",
                scan_id="scan-123",
                scope=scope,
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
            return type("Result", (), result)()

        app.state.runtime.scan_manager.start = _fake_start

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "started"
        assert data["scan_id"] == "scan-123"

    async def test_config_save_replaces_scan_manager_without_leaking_executor(self, configured_app, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"paths:\n  - {tmp_path}\ndb_path: {tmp_path / 'state.db'}\nworkers: 1\nraw_workers: 1\n")
        app.state.runtime.config_store = __import__("semanticdog.config_store", fromlist=["ConfigStore"]).ConfigStore(str(config_path))
        previous_manager = app.state.runtime.scan_manager

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.put("/api/config", json={"workers": 2})

        assert r.status_code == 200
        assert app.state.runtime.scan_manager is not previous_manager

    async def test_trigger_cooldown_returns_429(self, configured_app):
        server_module._last_trigger_time = time.monotonic()  # simulate recent trigger
        app.state.runtime.cfg.trigger_cooldown_s = 9999

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 429
        assert r.json()["error"] == "cooldown active"
        assert "retry_after_s" in r.json()


# ---------------------------------------------------------------------------
# build_app wiring
# ---------------------------------------------------------------------------

class TestBuildApp:
    def test_build_app_sets_state(self, cfg, db):
        build_app(cfg, db)
        assert server_module._cfg is cfg
        assert server_module._db is db
        assert app.state.runtime.cfg is cfg
        assert app.state.runtime.db is db
        assert app.state.runtime.scan_manager is not None

    def test_build_app_returns_fastapi(self, cfg, db):
        from fastapi import FastAPI
        result = build_app(cfg, db)
        assert isinstance(result, FastAPI)

    def test_mcp_not_mounted_when_disabled(self, cfg, db):
        cfg_no_mcp = Config(paths=["/x"], mcp_enabled=False)
        result = build_app(cfg_no_mcp, db)
        route_paths = [r.path for r in result.routes if hasattr(r, "path")]
        assert "/mcp/sse" not in route_paths


class TestScanManagerWiring:
    def test_build_app_initializes_scan_manager(self, cfg, db):
        build_app(cfg, db)
        assert isinstance(app.state.runtime.scan_manager, ScanManager)
