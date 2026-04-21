"""Configuration — YAML file + environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigError


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "paths": [],
    "exclude": ["**/@eaDir/**", "**/.DS_Store", "**/*.lrprev"],
    "follow_symlinks": False,
    "db_path": "/data/state/state.db",
    "log_path": "/data/logs/sdog.log",
    "log_max_bytes": 52_428_800,   # 50 MB
    "log_backup_count": 5,
    "schedule": "0 2 * * *",
    "workers": 4,
    "raw_workers": 2,
    "raw_decode_depth": "structure",
    "validation_timeout_s": 120,
    "force_recheck_days": 90,
    "io_throttle_ms": 0,
    "enable_hash": False,
    "hash_full_threshold_mb": 10,
    "memory_limit_mb": 0,
    "http_port": 9090,
    "trigger_cooldown_s": 60,
    "http_basic_enabled": False,
    "http_basic_username": "",
    "http_basic_password": "",
    # Notifications
    "notify_email": "",
    "smtp_host": "",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_tls": "starttls",
    "smtp_port": 0,
    "webhook_url": "",
    "webhook_allow_private": False,
    # TrueNAS
    "truenas_url": "",
    "truenas_key": "",
    "truenas_alerts_experimental": False,
    # MCP
    "mcp_enabled": False,
    "mcp_auth_token": "",
    "mcp_allow_write": False,
    "mcp_expose_resources": False,
    "mcp_rate_limit_s": 60,
}

# Env var prefix
_PREFIX = "SDOG_"

# Env vars that are colon-separated lists
_LIST_VARS = {"SDOG_PATHS", "SDOG_EXCLUDE"}

EDITABLE_CONFIG_FIELDS = frozenset(
    {
        "paths",
        "exclude",
        "follow_symlinks",
        "db_path",
        "workers",
        "raw_workers",
        "raw_decode_depth",
        "validation_timeout_s",
        "force_recheck_days",
        "http_port",
        "trigger_cooldown_s",
        "notify_email",
        "smtp_host",
        "smtp_user",
        "smtp_tls",
        "smtp_port",
        "webhook_url",
        "webhook_allow_private",
        "http_basic_enabled",
        "http_basic_username",
    }
)

ENV_ONLY_CONFIG_FIELDS = frozenset({"smtp_pass", "http_basic_password", "mcp_auth_token", "truenas_key"})

RESTART_REQUIRED_CONFIG_FIELDS = frozenset(
    {"db_path", "http_port", "http_basic_enabled", "http_basic_username", "http_basic_password"}
)

HIDDEN_CONFIG_FIELDS = frozenset({"log_path", "log_max_bytes", "log_backup_count", "schedule"})


@dataclass
class Config:
    # Scan roots
    paths: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    follow_symlinks: bool = False

    # Paths
    db_path: str = "/data/state/state.db"
    log_path: str = "/data/logs/sdog.log"
    log_max_bytes: int = 52_428_800
    log_backup_count: int = 5

    # Schedule
    schedule: str = "0 2 * * *"

    # Workers
    workers: int = 4
    raw_workers: int = 2

    # Validation
    raw_decode_depth: str = "structure"
    validation_timeout_s: int = 120
    force_recheck_days: int = 90
    io_throttle_ms: int = 0

    # Hashing
    enable_hash: bool = False
    hash_full_threshold_mb: int = 10

    # Resources
    memory_limit_mb: int = 0
    http_port: int = 9090
    trigger_cooldown_s: int = 60
    http_basic_enabled: bool = False
    http_basic_username: str = ""
    http_basic_password: str = ""

    # Notifications
    notify_email: str = ""
    smtp_host: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_tls: str = "starttls"
    smtp_port: int = 0
    webhook_url: str = ""
    webhook_allow_private: bool = False

    # TrueNAS
    truenas_url: str = ""
    truenas_key: str = ""
    truenas_alerts_experimental: bool = False

    # MCP
    mcp_enabled: bool = False
    mcp_auth_token: str = ""
    mcp_allow_write: bool = False
    mcp_expose_resources: bool = False
    mcp_rate_limit_s: int = 60

    def validate(self) -> None:
        """Raise ConfigError for invalid settings."""
        if not self.paths:
            raise ConfigError(
                "No scan paths configured. Set SDOG_PATHS or add 'paths' to config.yaml."
            )
        if self.raw_decode_depth not in ("structure", "full"):
            raise ConfigError(
                f"raw_decode_depth must be 'structure' or 'full', got {self.raw_decode_depth!r}"
            )
        if self.workers < 1:
            raise ConfigError(f"workers must be >= 1, got {self.workers}")
        if self.raw_workers < 1:
            raise ConfigError(f"raw_workers must be >= 1, got {self.raw_workers}")
        if self.validation_timeout_s < 1:
            raise ConfigError(f"validation_timeout_s must be >= 1, got {self.validation_timeout_s}")
        if self.trigger_cooldown_s < 0:
            raise ConfigError(f"trigger_cooldown_s must be >= 0, got {self.trigger_cooldown_s}")
        if self.smtp_tls not in ("starttls", "ssl", "none"):
            raise ConfigError(
                f"smtp_tls must be one of 'starttls', 'ssl', or 'none', got {self.smtp_tls!r}"
            )
        if self.smtp_port < 0:
            raise ConfigError(f"smtp_port must be >= 0, got {self.smtp_port}")
        if self.http_basic_enabled:
            if not self.http_basic_username:
                raise ConfigError("HTTP basic auth is enabled but http_basic_username is empty.")
            if not self.http_basic_password:
                raise ConfigError("HTTP basic auth is enabled but http_basic_password is empty.")
        if self.mcp_enabled and not self.mcp_auth_token:
            raise ConfigError(
                "MCP is enabled but SDOG_MCP_AUTH_TOKEN is not set. "
                "Set a bearer token or disable MCP."
            )

    def is_path_allowed(self, path: str) -> bool:
        """Check whether `path` is under one of the configured scan roots."""
        p = Path(path).resolve()
        return any(
            p == Path(root).resolve() or Path(root).resolve() in p.parents
            for root in self.paths
        )

    @classmethod
    def field_metadata(cls) -> dict[str, frozenset[str]]:
        return {
            "editable": EDITABLE_CONFIG_FIELDS,
            "env_only": ENV_ONLY_CONFIG_FIELDS,
            "restart_required": RESTART_REQUIRED_CONFIG_FIELDS,
            "hidden": HIDDEN_CONFIG_FIELDS,
        }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return bool(val)


def _parse_list(val: Any) -> list[str]:
    """Accept a YAML list or a colon-separated string."""
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        return [v.strip() for v in val.split(":") if v.strip()]
    return []


def _apply_env(data: dict[str, Any]) -> None:
    """Override dict values with SDOG_* environment variables."""
    for key, raw in os.environ.items():
        if not key.startswith(_PREFIX):
            continue
        cfg_key = key[len(_PREFIX):].lower()  # SDOG_HTTP_PORT → http_port
        if cfg_key not in _DEFAULTS:
            continue

        default = _DEFAULTS[cfg_key]
        if key in _LIST_VARS:
            data[cfg_key] = _parse_list(raw)
        elif isinstance(default, bool):
            data[cfg_key] = _parse_bool(raw)
        elif isinstance(default, int):
            try:
                data[cfg_key] = int(raw)
            except ValueError:
                raise ConfigError(f"Env var {key} must be an integer, got {raw!r}")
        else:
            data[cfg_key] = raw


def load_config(config_path: str | Path | None = None) -> Config:
    """
    Load configuration from optional YAML file, then apply env var overrides.
    Environment variables always win.
    """
    data: dict[str, Any] = dict(_DEFAULTS)

    # 1. YAML file
    if config_path is not None:
        p = Path(config_path)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        try:
            loaded = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {p}: {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file must be a YAML mapping, got {type(loaded).__name__}")
        # Normalise paths/exclude if provided as YAML lists
        for list_key in ("paths", "exclude"):
            if list_key in loaded:
                loaded[list_key] = _parse_list(loaded[list_key])
        data.update({k: v for k, v in loaded.items() if k in _DEFAULTS})

    # 2. Environment variables override everything
    _apply_env(data)

    cfg = Config(**{k: data[k] for k in Config.__dataclass_fields__})
    return cfg
