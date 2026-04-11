"""Stage 12 tests — HTTP server (FastAPI)."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

import semanticdog.server as server_module
from semanticdog.server import app, build_app
from semanticdog.config import Config
from semanticdog.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_server_state():
    """Reset module-level state between tests."""
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0
    # Reset lock in case previous test left it locked
    if server_module._scan_lock.locked():
        try:
            server_module._scan_lock.release()
        except RuntimeError:
            pass
    yield
    server_module._cfg = None
    server_module._db = None
    server_module._last_trigger_time = 0.0


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

    async def test_status_includes_file_count(self, configured_app, db):
        db.record("/img.jpg", 1.0, 100, "ok")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/status")
        assert r.json()["files_indexed"] == 1


# ---------------------------------------------------------------------------
# /trigger
# ---------------------------------------------------------------------------

class TestTriggerEndpoint:
    async def test_trigger_returns_409_when_locked(self, configured_app):
        async with server_module._scan_lock:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/trigger")
        assert r.status_code == 409
        assert r.json()["error"] == "scan already running"

    async def test_trigger_returns_503_when_unconfigured(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 503

    async def test_trigger_runs_scan(self, configured_app, tmp_path, cfg):
        from tests.fixtures.generators import make_minimal_jpeg
        make_minimal_jpeg(tmp_path / "img.jpg")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/trigger")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "complete"
        assert "scan_id" in data

    async def test_trigger_cooldown_returns_429(self, configured_app):
        server_module._last_trigger_time = time.monotonic()  # simulate recent trigger
        # Patch cooldown to large value
        configured_app  # ensure _cfg is set
        server_module._cfg = Config(paths=["/x"], workers=1, raw_workers=1)
        # Set trigger_cooldown_s attribute manually
        server_module._cfg.__dict__["trigger_cooldown_s"] = 9999

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

    def test_build_app_returns_fastapi(self, cfg, db):
        from fastapi import FastAPI
        result = build_app(cfg, db)
        assert isinstance(result, FastAPI)

    def test_mcp_not_mounted_when_disabled(self, cfg, db):
        cfg_no_mcp = Config(paths=["/x"], mcp_enabled=False)
        result = build_app(cfg_no_mcp, db)
        route_paths = [r.path for r in result.routes if hasattr(r, "path")]
        assert "/mcp/sse" not in route_paths


# ---------------------------------------------------------------------------
# Scan lock is asyncio.Lock
# ---------------------------------------------------------------------------

class TestScanLock:
    def test_scan_lock_is_asyncio_lock(self):
        assert isinstance(server_module._scan_lock, asyncio.Lock)

    async def test_lock_prevents_concurrent_trigger(self, configured_app):
        """Two concurrent /trigger calls — second gets 409."""
        results = []

        async def _call():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/trigger")
                results.append(r.status_code)

        # Hold lock, then fire two requests
        async with server_module._scan_lock:
            await asyncio.gather(_call(), _call())

        assert 409 in results
