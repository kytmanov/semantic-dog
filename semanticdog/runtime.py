"""Application runtime state for the HTTP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from .config import Config, load_config

if TYPE_CHECKING:
    from .db import Database


@dataclass
class AppRuntime:
    """Mutable server runtime shared through ``app.state``."""

    config_path: str | None = None
    cfg: Config | None = None
    db: "Database | None" = None
    config_error: str | None = None
    db_error: str | None = None
    scan_manager: Any | None = None

    @property
    def ready(self) -> bool:
        return (
            self.cfg is not None
            and self.db is not None
            and self.config_error is None
            and self.db_error is None
        )


def load_runtime(config_path: str | None = None) -> AppRuntime:
    """Load config and DB for the HTTP server.

    The runtime may be partially configured. This lets the Web UI start in a
    degraded state and guide the user through fixing config or storage issues.
    """

    runtime = AppRuntime(config_path=config_path)

    try:
        cfg = load_config(config_path)
    except Exception as e:
        runtime.cfg = Config()
        runtime.config_error = str(e)
        return runtime

    runtime.cfg = cfg

    try:
        cfg.validate()
    except Exception as e:
        runtime.config_error = str(e)

    try:
        from .db import Database

        runtime.db = Database(cfg.db_path)
    except Exception as e:
        runtime.db_error = str(e)

    return runtime
