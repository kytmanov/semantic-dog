"""Stage 16 tests — packaged web UI shell."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from semanticdog.config import Config
from semanticdog.config_store import ConfigStore
from semanticdog.db import Database
from semanticdog.runtime import AppRuntime
from semanticdog.server import create_app


class TestWebUi:
    async def test_root_renders_setup_when_unconfigured(self):
        app = create_app(AppRuntime())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/")

        assert r.status_code == 200
        assert "Setup" in r.text

    async def test_config_page_renders_with_no_config_store(self):
        app = create_app(AppRuntime(cfg=Config(), db=None, config_store=None))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/config")

        assert r.status_code == 200
        assert "Configuration" in r.text

    async def test_dashboard_renders_for_configured_runtime(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/dashboard")

        assert r.status_code == 200
        assert "Dashboard" in r.text
        assert "SemanticDog" in r.text
        assert "Run Scan" in r.text
        assert "count-ok" in r.text

    async def test_setup_page_renders(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/setup")

        assert r.status_code == 200
        assert "Scan Roots" in r.text
        assert "Save Configuration" in r.text

    async def test_config_page_renders_settings_form(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/config")

        assert r.status_code == 200
        assert "Configuration" in r.text
        assert "Save Configuration" in r.text

    async def test_config_page_shows_env_override_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SDOG_HTTP_PORT", "9876")
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"), http_port=9876)
        # ConfigStore required: default_config_view returns "default" for all sources
        # and never detects env vars; ConfigStore.field_sources() checks os.environ.
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path), config_store=ConfigStore()))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/config")

        assert r.status_code == 200
        assert "tag-env" in r.text

    async def test_issues_page_renders_issue_table(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        db = Database(cfg.db_path)
        db.record(str(tmp_path / "bad.jpg"), 1.0, 100, "corrupt", error="Unexpected EOF")
        app = create_app(AppRuntime(cfg=cfg, db=db))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/issues")

        assert r.status_code == 200
        assert "Issues" in r.text
        assert "Unexpected EOF" in r.text

    async def test_history_page_renders_scan_table(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        db = Database(cfg.db_path)
        scan_id = db.create_scan(scope=str(tmp_path))
        db.finish_scan(scan_id, total=2, corrupt=1, unreadable=0, files_per_sec=1.0)
        app = create_app(AppRuntime(cfg=cfg, db=db))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/history")

        assert r.status_code == 200
        assert "Scan History" in r.text
        assert scan_id in r.text

    async def test_dashboard_shows_configuration_needed_banner_for_degraded_runtime(self):
        app = create_app(AppRuntime(config_error="bad config"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/dashboard")

        assert r.status_code == 200
        assert "Configuration needed" in r.text

    async def test_dashboard_shows_healthy_banner_after_scan(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        db = Database(cfg.db_path)
        scan_id = db.create_scan(scope=str(tmp_path))
        db.record(str(tmp_path / "img.jpg"), 1.0, 100, "ok", scan_id=scan_id)
        db.finish_scan(scan_id, total=1, corrupt=0, unreadable=0, files_per_sec=1.0)
        app = create_app(AppRuntime(cfg=cfg, db=db))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/dashboard")

        assert r.status_code == 200
        assert "Healthy" in r.text

    async def test_static_assets_are_served(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/static/app.css")

        assert r.status_code == 200
        assert "progress-fill" in r.text
