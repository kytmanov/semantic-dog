"""Stage 16 tests — packaged web UI shell."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from semanticdog.config import Config
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

    async def test_dashboard_renders_for_configured_runtime(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/dashboard")

        assert r.status_code == 200
        assert "Dashboard" in r.text
        assert "SemanticDog" in r.text

    async def test_setup_page_renders(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/setup")

        assert r.status_code == 200
        assert "Scan Roots" in r.text

    async def test_static_assets_are_served(self, tmp_path):
        cfg = Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db"))
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/static/app.css")

        assert r.status_code == 200
        assert "shell-header" in r.text
