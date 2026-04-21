"""Safe config file access for the Web UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .cli import _find_config
from .config import Config, EDITABLE_CONFIG_FIELDS, _DEFAULTS, load_config
from .exceptions import ConfigError


class ConfigStore:
    def __init__(self, config_path: str | None = None) -> None:
        resolved = config_path or _find_config() or "/data/config/config.yaml"
        self.path = Path(resolved).expanduser()

    def load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            loaded = yaml.safe_load(self.path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {self.path}: {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file must be a YAML mapping, got {type(loaded).__name__}")
        return dict(loaded)

    def load_effective(self) -> Config:
        return load_config(self.path if self.path.exists() else None)

    def field_sources(self, raw: dict[str, Any]) -> dict[str, str]:
        sources: dict[str, str] = {}
        for field in Config.__dataclass_fields__:
            env_name = f"SDOG_{field.upper()}"
            if env_name in os.environ:
                sources[field] = "env"
            elif field in raw:
                sources[field] = "yaml"
            else:
                sources[field] = "default"
        return sources

    def get_view(self) -> dict[str, Any]:
        raw = self.load_raw()
        effective = self.load_effective()
        return {
            "path": str(self.path),
            "raw": raw,
            "effective": {field: getattr(effective, field) for field in Config.__dataclass_fields__},
            "sources": self.field_sources(raw),
        }

    def validate_update(self, updates: dict[str, Any]) -> Config:
        unsupported = sorted(set(updates) - EDITABLE_CONFIG_FIELDS)
        if unsupported:
            raise ConfigError(f"Unsupported config fields for UI update: {', '.join(unsupported)}")

        raw = self.load_raw()
        merged = dict(raw)
        merged.update(updates)

        normalized = dict(_DEFAULTS)
        for list_key in ("paths", "exclude"):
            if list_key in merged and isinstance(merged[list_key], str):
                merged[list_key] = [v.strip() for v in merged[list_key].split(":") if v.strip()]
        normalized.update({k: v for k, v in merged.items() if k in _DEFAULTS})
        cfg = Config(**{k: normalized[k] for k in Config.__dataclass_fields__})
        cfg.validate()
        return cfg

    def save(self, updates: dict[str, Any]) -> dict[str, Any]:
        cfg = self.validate_update(updates)
        raw = self.load_raw()
        merged = dict(raw)
        merged.update(updates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(merged, sort_keys=False))
        return {
            "path": str(self.path),
            "saved": updates,
            "effective": {field: getattr(cfg, field) for field in Config.__dataclass_fields__},
        }
