"""Setup and environment diagnostics for the Web UI."""

from __future__ import annotations

import os
from pathlib import Path

from semanticdog.validators import all_validators


def _can_write(path: Path) -> bool:
    try:
        if path.exists():
            return os.access(path, os.W_OK)
        parent = path.parent
        if not parent.exists():
            return _can_write(parent)
        return os.access(parent, os.W_OK)
    except OSError:
        return False


def collect_setup_diagnostics(runtime) -> dict:
    cfg = runtime.cfg
    config_path = Path(runtime.config_path).expanduser() if runtime.config_path else None
    db_path = Path(cfg.db_path).expanduser() if cfg is not None else None

    scan_roots = []
    if cfg is not None:
        for raw_path in cfg.paths:
            root = Path(raw_path).expanduser()
            scan_roots.append(
                {
                    "path": str(root),
                    "exists": root.exists(),
                    "is_dir": root.is_dir(),
                    "readable": os.access(root, os.R_OK),
                }
            )

    deps = []
    seen = set()
    for validator_cls in all_validators():
        for report in validator_cls().check_dependencies():
            if report.name in seen:
                continue
            seen.add(report.name)
            deps.append(
                {
                    "name": report.name,
                    "available": report.available,
                    "version": report.version,
                    "required": report.required,
                }
            )

    warnings = []
    if runtime.config_error:
        warnings.append(runtime.config_error)
    if runtime.db_error:
        warnings.append(runtime.db_error)
    for root in scan_roots:
        if not root["exists"]:
            warnings.append(f"Scan root missing: {root['path']}")
        elif not root["readable"]:
            warnings.append(f"Scan root not readable: {root['path']}")

    config_info = {
        "path": str(config_path) if config_path else None,
        "exists": config_path.exists() if config_path else False,
        "parent_writable": _can_write(config_path.parent) if config_path else False,
    }
    db_info = {
        "path": str(db_path) if db_path else None,
        "parent_exists": db_path.parent.exists() if db_path else False,
        "parent_writable": _can_write(db_path.parent) if db_path else False,
    }

    return {
        "config": config_info,
        "db": db_info,
        "scan_roots": scan_roots,
        "dependencies": deps,
        "warnings": warnings,
    }


def collect_readiness(runtime) -> dict:
    setup = collect_setup_diagnostics(runtime)
    checks = {
        "config_path_writable": setup["config"]["parent_writable"],
        "db_parent_writable": setup["db"]["parent_writable"],
        "db_available": runtime.db_error is None and runtime.db is not None,
        "config_valid": runtime.config_error is None,
        "scan_roots_configured": bool(runtime.cfg and runtime.cfg.paths),
        "scan_roots_accessible": bool(setup["scan_roots"]) and all(
            root["exists"] and root["is_dir"] and root["readable"] for root in setup["scan_roots"]
        ),
    }
    ready = all(checks.values())
    return {
        "ready": ready,
        "checks": checks,
        "config_error": runtime.config_error,
        "db_error": runtime.db_error,
        "warnings": setup["warnings"],
    }
