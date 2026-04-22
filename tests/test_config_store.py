"""Stage 19 tests — config store and write API helpers."""

from __future__ import annotations

import os

import pytest

from semanticdog.config_store import ConfigStore
from semanticdog.exceptions import ConfigError


class TestConfigStore:
    def test_get_view_preserves_unknown_yaml_keys(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\nfuture_key: 42\n")
        store = ConfigStore(str(config_path))

        view = store.get_view()

        assert view["raw"]["future_key"] == 42
        assert view["sources"]["paths"] == "yaml"

    def test_validate_update_accepts_schedule_field(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\n")
        store = ConfigStore(str(config_path))

        cfg = store.validate_update({"schedule": "* * * * *"})

        assert cfg.schedule == "* * * * *"

    def test_save_preserves_unknown_keys(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\nfuture_key: 42\n")
        store = ConfigStore(str(config_path))

        store.save({"workers": 8})

        saved = config_path.read_text()
        assert "future_key: 42" in saved
        assert "workers: 8" in saved

    def test_sources_report_env_override(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\nworkers: 2\n")
        monkeypatch.setenv("SDOG_WORKERS", "9")
        store = ConfigStore(str(config_path))

        view = store.get_view()

        assert view["effective"]["workers"] == 9
        assert view["sources"]["workers"] == "env"

    def test_sources_prefer_direct_env_over_env_file(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  - /library\n")
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD", "from-env")
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD_FILE", "/tmp/secret")
        store = ConfigStore(str(config_path))

        view = store.get_view()

        assert view["sources"]["http_basic_password"] == "env"
