"""HTTP server — FastAPI: /metrics, /health, /trigger, /status, /mcp."""

from __future__ import annotations

import asyncio
import time
from typing import Any, TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from .config import Config
    from .db import Database


# ---------------------------------------------------------------------------
# Module-level app — routes defined here, wired to real state by build_app()
# ---------------------------------------------------------------------------

app = FastAPI(title="SemanticDog", version="0.1.0")

# Shared state (set by build_app)
_cfg: "Config | None" = None
_db: "Database | None" = None
_scan_lock = asyncio.Lock()
_last_trigger_time: float = 0.0


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /metrics  (Prometheus text format)
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics() -> Response:
    lines: list[str] = []

    if _db is not None:
        try:
            stats = _db.get_stats()
            by_status = stats.get("by_status", {})
            for status, count in by_status.items():
                lines.append(f'sdog_files_total{{status="{status}"}} {count}')
            lines.append(f'sdog_files_indexed_total {stats.get("total", 0)}')

            fps = _db.get_last_files_per_sec()
            if fps is not None:
                lines.append(f"sdog_scan_rate_files_per_second {fps:.3f}")

            scans = _db.list_scans(limit=1)
            if scans and scans[0].get("finished_at"):
                lines.append(f'sdog_last_scan_timestamp{{scan_id="{scans[0]["id"]}"}} 1')
        except Exception:
            pass

        try:
            import os
            db_size = os.path.getsize(str(_db.db_path))
            lines.append(f"sdog_db_size_bytes {db_size}")
        except Exception:
            pass

    lines.append("# END")
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

@app.get("/status")
async def status() -> dict[str, Any]:
    if _db is None:
        return {"status": "unconfigured"}
    try:
        stats = _db.get_stats()
        scans = _db.list_scans(limit=1)
        last_scan = scans[0] if scans else None
        return {
            "status": "scanning" if _scan_lock.locked() else "idle",
            "files_indexed": stats.get("total", 0),
            "by_status": stats.get("by_status", {}),
            "last_scan": last_scan,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# /trigger
# ---------------------------------------------------------------------------

@app.post("/trigger")
async def trigger(request: Request) -> JSONResponse:
    global _last_trigger_time

    # 409 if scan already running
    if _scan_lock.locked():
        scans = _db.list_scans(limit=1) if _db else []
        running_id = scans[0]["id"] if scans else None
        return JSONResponse(
            status_code=409,
            content={"error": "scan already running", "scan_id": running_id},
        )

    # 429 cooldown
    if _cfg is not None:
        cooldown = getattr(_cfg, "trigger_cooldown_s", 60)
        elapsed = time.monotonic() - _last_trigger_time
        if _last_trigger_time > 0 and elapsed < cooldown:
            retry_after = int(cooldown - elapsed) + 1
            return JSONResponse(
                status_code=429,
                content={"error": "cooldown active", "retry_after_s": retry_after},
            )

    if _cfg is None or _db is None:
        return JSONResponse(status_code=503, content={"error": "server not configured"})

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    scope = body.get("scope") or None
    if scope is not None and not _cfg.is_path_allowed(scope):
        return JSONResponse(
            status_code=400,
            content={"error": f"scope {scope!r} is not under a configured scan root"},
        )

    result: dict = {}

    async with _scan_lock:
        _last_trigger_time = time.monotonic()
        loop = asyncio.get_running_loop()
        from .scanner import Scanner

        def _run_scan() -> str:
            scanner = Scanner(_cfg, _db)
            paths = [scope] if scope else None
            stats = scanner.scan(paths)
            scans = _db.list_scans(limit=1)
            return scans[0]["id"] if scans else "unknown"

        try:
            scan_id = await loop.run_in_executor(None, _run_scan)
            result = {"status": "complete", "scan_id": scan_id}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# build_app — factory with real state + optional MCP
# ---------------------------------------------------------------------------

def build_app(cfg: "Config", db: "Database") -> FastAPI:
    """Wire global state and optionally mount MCP at /mcp."""
    global _cfg, _db
    _cfg = cfg
    _db = db

    if cfg.mcp_enabled:
        try:
            from .mcp_server import handle_sse, sse_transport
            app.add_api_route("/mcp/sse", handle_sse, methods=["GET"])
            app.mount("/mcp/messages", app=sse_transport.handle_post_message)
        except ImportError:
            pass  # mcp SDK not installed — skip

    return app
