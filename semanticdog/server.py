"""HTTP server — FastAPI: /metrics, /health, /trigger, /status, /mcp."""

from __future__ import annotations

import asyncio
import time
from typing import Any, TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .runtime import AppRuntime

if TYPE_CHECKING:
    from .config import Config
    from .db import Database


# ---------------------------------------------------------------------------
# Module-level app — routes defined here, wired to real state by build_app()
# ---------------------------------------------------------------------------

app = FastAPI(title="SemanticDog", version="0.1.0")

# Compatibility globals kept for older tests and call sites.
# Route handlers use request.app.state.runtime instead.
_cfg: "Config | None" = None
_db: "Database | None" = None
_scan_lock = asyncio.Lock()
_last_trigger_time: float = 0.0


def _mount_mcp(target_app: FastAPI, runtime: AppRuntime) -> None:
    cfg = runtime.cfg
    if cfg is None or not cfg.mcp_enabled:
        return
    if getattr(target_app.state, "mcp_mounted", False):
        return

    try:
        from .mcp_server import handle_sse, sse_transport

        target_app.add_api_route("/mcp/sse", handle_sse, methods=["GET"])
        target_app.mount("/mcp/messages", app=sse_transport.handle_post_message)
        target_app.state.mcp_mounted = True
    except ImportError:
        pass  # mcp SDK not installed — skip


def _get_runtime(request: Request) -> AppRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        runtime = AppRuntime()
        request.app.state.runtime = runtime
    return runtime


def _unconfigured_response(runtime: AppRuntime) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "server not configured",
            "config_error": runtime.config_error,
            "db_error": runtime.db_error,
        },
    )


def create_app(runtime: AppRuntime | None = None) -> FastAPI:
    target_app = FastAPI(title="SemanticDog", version="0.1.0")
    target_app.state.runtime = runtime or AppRuntime()
    target_app.state.mcp_mounted = False

    _mount_mcp(target_app, target_app.state.runtime)

    # -----------------------------------------------------------------------
    # /health
    # -----------------------------------------------------------------------

    @target_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # -----------------------------------------------------------------------
    # /metrics  (Prometheus text format)
    # -----------------------------------------------------------------------

    @target_app.get("/metrics")
    async def metrics(request: Request) -> Response:
        lines: list[str] = []
        runtime = _get_runtime(request)
        db = runtime.db

        if db is not None:
            try:
                stats = db.get_stats()
                by_status = stats.get("by_status", {})
                for status, count in by_status.items():
                    lines.append(f'sdog_files_total{{status="{status}"}} {count}')
                lines.append(f'sdog_files_indexed_total {stats.get("total", 0)}')

                fps = db.get_last_files_per_sec()
                if fps is not None:
                    lines.append(f"sdog_scan_rate_files_per_second {fps:.3f}")

                scans = db.list_scans(limit=1)
                if scans and scans[0].get("finished_at"):
                    lines.append(f'sdog_last_scan_timestamp{{scan_id="{scans[0]["id"]}"}} 1')
            except Exception:
                pass

            try:
                import os

                db_size = os.path.getsize(str(db.db_path))
                lines.append(f"sdog_db_size_bytes {db_size}")
            except Exception:
                pass

        lines.append("# END")
        return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # -----------------------------------------------------------------------
    # /status
    # -----------------------------------------------------------------------

    @target_app.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        db = runtime.db

        if runtime.config_error or runtime.db_error:
            return {
                "status": "degraded",
                "config_error": runtime.config_error,
                "db_error": runtime.db_error,
                "files_indexed": 0,
                "by_status": {},
                "last_scan": None,
            }

        if db is None:
            return {"status": "unconfigured"}

        try:
            stats = db.get_stats()
            scans = db.list_scans(limit=1)
            last_scan = scans[0] if scans else None
            return {
                "status": "scanning" if _scan_lock.locked() else "idle",
                "files_indexed": stats.get("total", 0),
                "by_status": stats.get("by_status", {}),
                "last_scan": last_scan,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # -----------------------------------------------------------------------
    # /trigger
    # -----------------------------------------------------------------------

    @target_app.post("/trigger")
    async def trigger(request: Request) -> JSONResponse:
        global _last_trigger_time

        runtime = _get_runtime(request)
        cfg = runtime.cfg
        db = runtime.db

        if _scan_lock.locked():
            scans = db.list_scans(limit=1) if db else []
            running_id = scans[0]["id"] if scans else None
            return JSONResponse(
                status_code=409,
                content={"error": "scan already running", "scan_id": running_id},
            )

        if cfg is not None:
            cooldown = getattr(cfg, "trigger_cooldown_s", 60)
            elapsed = time.monotonic() - _last_trigger_time
            if _last_trigger_time > 0 and elapsed < cooldown:
                retry_after = int(cooldown - elapsed) + 1
                return JSONResponse(
                    status_code=429,
                    content={"error": "cooldown active", "retry_after_s": retry_after},
                )

        if not runtime.ready or cfg is None or db is None:
            return _unconfigured_response(runtime)

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        scope = body.get("scope") or None
        if scope is not None and not cfg.is_path_allowed(scope):
            return JSONResponse(
                status_code=400,
                content={"error": f"scope {scope!r} is not under a configured scan root"},
            )

        async with _scan_lock:
            _last_trigger_time = time.monotonic()
            loop = asyncio.get_running_loop()
            from .scanner import Scanner

            def _run_scan() -> str:
                scanner = Scanner(cfg, db)
                paths = [scope] if scope else None
                scanner.scan(paths)
                scans = db.list_scans(limit=1)
                return scans[0]["id"] if scans else "unknown"

            try:
                scan_id = await loop.run_in_executor(None, _run_scan)
                return JSONResponse({"status": "complete", "scan_id": scan_id})
            except Exception as e:
                return JSONResponse(status_code=500, content={"error": str(e)})

    return target_app


app = create_app()


def build_app(cfg: "Config", db: "Database") -> FastAPI:
    """Compatibility shim for tests and older call sites."""
    global _cfg, _db
    _cfg = cfg
    _db = db

    runtime = AppRuntime(cfg=cfg, db=db)
    app.state.runtime = runtime
    _mount_mcp(app, runtime)
    return app
