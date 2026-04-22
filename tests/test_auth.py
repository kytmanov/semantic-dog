"""Stage 15 tests — HTTP basic auth."""

from __future__ import annotations

import base64

from httpx import ASGITransport, AsyncClient

from semanticdog.config import Config
from semanticdog.db import Database
from semanticdog.server import create_app
from semanticdog.runtime import AppRuntime


def _auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class TestHttpBasicAuth:
    async def test_health_remains_public(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")

        assert r.status_code == 200

    async def test_ready_remains_public(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"paths:\n  - {tmp_path}\n")
        app = create_app(
            AppRuntime(
                cfg=cfg,
                db=Database(cfg.db_path),
                config_path=str(config_path),
            )
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/ready")

        assert r.status_code == 200

    async def test_favicon_remains_public(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path)))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/favicon.ico")

        assert r.status_code == 204

    async def test_protected_route_returns_401_without_credentials(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        app = create_app(AppRuntime(cfg=cfg, db=Database(cfg.db_path), scan_manager=None))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")

        assert r.status_code == 401

    async def test_protected_route_accepts_valid_credentials(self, tmp_path):
        cfg = Config(
            paths=[str(tmp_path)],
            db_path=str(tmp_path / "state.db"),
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        runtime = AppRuntime(cfg=cfg, db=Database(cfg.db_path))
        app = create_app(runtime)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status", headers=_auth_header("admin", "secret"))

        assert r.status_code == 200
