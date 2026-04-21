"""HTTP server — FastAPI: /metrics, /health, /trigger, /status, /mcp."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import secrets
import time
from typing import Any, TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import RESTART_REQUIRED_CONFIG_FIELDS
from .notify import Notifier, ScanSummary
from .runtime import AppRuntime
from .services.diagnostics import collect_setup_diagnostics

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


def _is_public_route(path: str) -> bool:
    return path == "/health" or path.startswith("/static/")


def _is_authorized(request: Request, runtime: AppRuntime) -> bool:
    cfg = runtime.cfg
    if cfg is None or not cfg.http_basic_enabled:
        return True

    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    username, sep, password = decoded.partition(":")
    if not sep:
        return False
    return secrets.compare_digest(username, cfg.http_basic_username) and secrets.compare_digest(
        password, cfg.http_basic_password
    )


def _dashboard_banner(status_payload: dict[str, Any], setup: dict[str, Any], runtime: AppRuntime) -> dict[str, str]:
    if runtime.config_error or runtime.db_error:
        return {
            "state": "Configuration needed",
            "detail": "The server started in degraded mode. Review setup warnings before scanning.",
        }
    if any("not readable" in warning or "missing" in warning for warning in setup.get("warnings", [])):
        return {
            "state": "Access problem suspected",
            "detail": "One or more scan roots are missing or unreadable inside the current runtime.",
        }
    if status_payload.get("status") == "scanning":
        return {
            "state": "Scan running",
            "detail": "SemanticDog is validating files in the background. You can refresh safely.",
        }
    by_status = status_payload.get("by_status", {})
    if by_status.get("corrupt", 0) or by_status.get("unreadable", 0):
        return {
            "state": "Issues found",
            "detail": "Corrupt or unreadable files need attention. Check the latest scan details below.",
        }
    if status_payload.get("files_indexed", 0) > 0:
        return {
            "state": "Healthy",
            "detail": "No current corruption or access issues are recorded in the indexed library.",
        }
    return {
        "state": "Configuration needed",
        "detail": "Finish setup and run the first scan to establish a baseline.",
    }


def create_app(runtime: AppRuntime | None = None) -> FastAPI:
    target_app = FastAPI(title="SemanticDog", version="0.1.0")
    target_app.state.runtime = runtime or AppRuntime()
    target_app.state.mcp_mounted = False

    web_root = Path(__file__).parent / "web"
    templates = Jinja2Templates(directory=str(web_root / "templates"))
    target_app.mount("/static", StaticFiles(directory=str(web_root / "static")), name="static")

    _mount_mcp(target_app, target_app.state.runtime)

    @target_app.middleware("http")
    async def require_auth(request: Request, call_next):
        runtime = _get_runtime(request)
        if _is_public_route(request.url.path) or _is_authorized(request, runtime):
            return await call_next(request)
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="SemanticDog"'},
        )

    # -----------------------------------------------------------------------
    # /health
    # -----------------------------------------------------------------------

    @target_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @target_app.get("/")
    async def index(request: Request):
        runtime = _get_runtime(request)
        setup = collect_setup_diagnostics(runtime)
        status_payload = await status(request)
        store = runtime.config_store
        config_view = store.get_view() if store is not None else {"effective": runtime.cfg or {}}
        if runtime.config_error or not setup["scan_roots"]:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {"title": "SemanticDog Setup", "setup": setup, "config": config_view},
            )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "SemanticDog Dashboard",
                "status": status_payload,
                "setup": setup,
                "banner": _dashboard_banner(status_payload, setup, runtime),
                "current_scan": status_payload.get("current_scan"),
            },
        )

    @target_app.get("/dashboard")
    async def dashboard(request: Request):
        runtime = _get_runtime(request)
        setup = collect_setup_diagnostics(runtime)
        status_payload = await status(request)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "SemanticDog Dashboard",
                "status": status_payload,
                "setup": setup,
                "banner": _dashboard_banner(status_payload, setup, runtime),
                "current_scan": status_payload.get("current_scan"),
            },
        )

    @target_app.get("/setup")
    async def setup_page(request: Request):
        runtime = _get_runtime(request)
        store = runtime.config_store
        config_view = store.get_view() if store is not None else {"effective": runtime.cfg or {}}
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "title": "SemanticDog Setup",
                "setup": collect_setup_diagnostics(runtime),
                "config": config_view,
            },
        )

    @target_app.get("/config")
    async def config_page(request: Request):
        runtime = _get_runtime(request)
        store = runtime.config_store
        config_view = store.get_view() if store is not None else {"effective": runtime.cfg or {}, "sources": {}}
        return templates.TemplateResponse(
            request,
            "config.html",
            {
                "title": "SemanticDog Configuration",
                "config": config_view,
                "restart_required": RESTART_REQUIRED_CONFIG_FIELDS,
            },
        )

    @target_app.get("/issues")
    async def issues_page(request: Request):
        runtime = _get_runtime(request)
        db = runtime.db
        issues = [] if db is None else db.list_issue_files(statuses=["corrupt", "unreadable"], limit=50)
        return templates.TemplateResponse(
            request,
            "issues.html",
            {"title": "SemanticDog Issues", "issues": issues},
        )

    @target_app.get("/history")
    async def history_page(request: Request):
        runtime = _get_runtime(request)
        db = runtime.db
        scans = [] if db is None else db.list_scans(limit=20)
        return templates.TemplateResponse(
            request,
            "history.html",
            {"title": "SemanticDog History", "scans": scans},
        )

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
            manager = runtime.scan_manager
            current_scan = manager.current_snapshot() if manager is not None else None
            return {
                "status": "scanning" if current_scan and current_scan.state in {"starting", "running"} else "idle",
                "files_indexed": stats.get("total", 0),
                "by_status": stats.get("by_status", {}),
                "last_scan": last_scan,
                "current_scan": None if current_scan is None else current_scan.__dict__,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @target_app.get("/api/app")
    async def api_app(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        manager = runtime.scan_manager
        current = manager.current_snapshot() if manager is not None else None
        return {
            "ready": runtime.ready,
            "config_path": runtime.config_path,
            "config_error": runtime.config_error,
            "db_error": runtime.db_error,
            "current_scan": None if current is None else current.__dict__,
        }

    @target_app.get("/api/config")
    async def api_config(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        store = runtime.config_store
        if store is None:
            return {"path": runtime.config_path, "raw": {}, "effective": {}, "sources": {}}
        return store.get_view()

    @target_app.post("/api/config/validate")
    async def api_config_validate(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        store = runtime.config_store
        if store is None:
            return {"valid": False, "error": "config store unavailable"}
        payload = await request.json()
        try:
            cfg = store.validate_update(payload)
        except Exception as e:
            return {"valid": False, "error": str(e)}
        return {
            "valid": True,
            "effective": {field: getattr(cfg, field) for field in cfg.__dataclass_fields__},
        }

    @target_app.put("/api/config")
    async def api_config_save(request: Request) -> JSONResponse:
        runtime = _get_runtime(request)
        store = runtime.config_store
        manager = runtime.scan_manager
        if manager is not None and manager.is_running():
            return JSONResponse(status_code=409, content={"error": "cannot save config while scan is running"})
        if store is None:
            return JSONResponse(status_code=500, content={"error": "config store unavailable"})

        payload = await request.json()
        try:
            saved = store.save(payload)
            runtime.config_path = saved["path"]
            runtime.config_store = store
            runtime.cfg = store.load_effective()
            runtime.config_error = None
            restart_fields = set(payload) & RESTART_REQUIRED_CONFIG_FIELDS
            if not restart_fields and runtime.db is not None:
                from .services.scan_manager import ScanManager

                runtime.scan_manager = ScanManager(runtime.cfg, runtime.db)
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

        return JSONResponse({"status": "saved", "restart_required": sorted(restart_fields), **saved})

    @target_app.get("/api/setup")
    async def api_setup(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        return collect_setup_diagnostics(runtime)

    @target_app.get("/api/scan/current")
    async def api_scan_current(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        manager = runtime.scan_manager
        current = manager.current_snapshot() if manager is not None else None
        last = manager.last_snapshot() if manager is not None else None
        return {
            "current": None if current is None else current.__dict__,
            "last": None if last is None else last.__dict__,
            "last_error": manager.last_error() if manager is not None else None,
            "notification_errors": manager.last_notification_errors() if manager is not None else [],
        }

    @target_app.post("/api/notify/test")
    async def api_notify_test(request: Request) -> JSONResponse:
        runtime = _get_runtime(request)
        cfg = runtime.cfg
        if cfg is None:
            return JSONResponse(status_code=503, content={"error": "server not configured"})

        summary = ScanSummary(
            scan_id="test-notification",
            scope=",".join(cfg.paths) if cfg.paths else "/data",
            duration_s=0.1,
            total_checked=1,
            corrupt=[],
            unreadable=[],
        )
        errors = Notifier(cfg).notify(summary)
        return JSONResponse({"status": "sent" if not errors else "partial", "errors": errors})

    @target_app.get("/api/issues")
    async def api_issues(
        request: Request,
        status: str | None = None,
        ext: str | None = None,
        path_prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        runtime = _get_runtime(request)
        db = runtime.db
        if db is None:
            return {"issues": []}
        statuses = [status] if status else ["corrupt", "unreadable"]
        return {
            "issues": db.list_issue_files(
                statuses=statuses,
                ext=ext,
                path_prefix=path_prefix,
                limit=limit,
                offset=offset,
            )
        }

    @target_app.get("/api/scans")
    async def api_scans(request: Request) -> dict[str, Any]:
        runtime = _get_runtime(request)
        db = runtime.db
        if db is None:
            return {"scans": []}
        return {"scans": db.list_scans(limit=20)}

    @target_app.get("/api/scans/{scan_id}")
    async def api_scan_by_id(scan_id: str, request: Request) -> JSONResponse:
        runtime = _get_runtime(request)
        db = runtime.db
        if db is None:
            return JSONResponse(status_code=404, content={"error": "scan not found"})
        scan = db.get_scan(scan_id)
        if scan is None:
            return JSONResponse(status_code=404, content={"error": "scan not found"})
        return JSONResponse(scan)

    # -----------------------------------------------------------------------
    # /trigger
    # -----------------------------------------------------------------------

    @target_app.post("/trigger")
    async def trigger(request: Request) -> JSONResponse:
        global _last_trigger_time

        runtime = _get_runtime(request)
        cfg = runtime.cfg
        db = runtime.db
        manager = runtime.scan_manager

        if manager is not None and manager.is_running():
            current = manager.current_snapshot()
            return JSONResponse(
                status_code=409,
                content={
                    "error": "scan already running",
                    "scan_id": current.scan_id if current else None,
                },
            )

        if cfg is not None:
            cooldown = cfg.trigger_cooldown_s
            elapsed = time.monotonic() - _last_trigger_time
            if _last_trigger_time > 0 and elapsed < cooldown:
                retry_after = int(cooldown - elapsed) + 1
                return JSONResponse(
                    status_code=429,
                    content={"error": "cooldown active", "retry_after_s": retry_after},
                )

        if not runtime.ready or cfg is None or db is None or manager is None:
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

        _last_trigger_time = time.monotonic()
        result = manager.start(scope=scope)
        if not result.accepted:
            return JSONResponse(
                status_code=409,
                content={"error": result.error or "scan already running", "scan_id": result.scan_id},
            )

        deadline = time.monotonic() + 1.0
        scan_id = None
        while time.monotonic() < deadline:
            current = manager.current_snapshot()
            if current is not None:
                scan_id = current.scan_id
                break
            time.sleep(0.01)

        return JSONResponse({"status": "started", "scan_id": scan_id})

    return target_app


app = create_app()


def build_app(cfg: "Config", db: "Database") -> FastAPI:
    """Compatibility shim for tests and older call sites."""
    global _cfg, _db
    _cfg = cfg
    _db = db

    from .config_store import ConfigStore
    from .services.scan_manager import ScanManager

    runtime = AppRuntime(
        cfg=cfg,
        db=db,
        config_store=ConfigStore(),
        scan_manager=ScanManager(cfg, db),
    )
    app.state.runtime = runtime
    _mount_mcp(app, runtime)
    return app
