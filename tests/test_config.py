"""Stage 2 tests — configuration layer."""

from __future__ import annotations

import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from semanticdog.config import Config, load_config
from semanticdog.exceptions import ConfigError


class TestLoadDefaults:
    def test_empty_env_no_yaml_returns_defaults(self, monkeypatch):
        # Wipe all SDOG_ env vars
        for k in list(os.environ):
            if k.startswith("SDOG_"):
                monkeypatch.delenv(k)
        cfg = load_config()
        assert cfg.workers == 4
        assert cfg.raw_workers == 2
        assert cfg.raw_decode_depth == "structure"
        assert cfg.validation_timeout_s == 120
        assert cfg.force_recheck_days == 90
        assert cfg.mcp_enabled is False
        assert cfg.follow_symlinks is False

    def test_default_exclude_patterns(self, monkeypatch):
        for k in list(os.environ):
            if k.startswith("SDOG_"):
                monkeypatch.delenv(k)
        cfg = load_config()
        assert "**/@eaDir/**" in cfg.exclude
        assert "**/.DS_Store" in cfg.exclude


class TestYamlLoading:
    def _write_yaml(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
        f.write(textwrap.dedent(content))
        f.close()
        return Path(f.name)

    def test_yaml_paths_list(self):
        p = self._write_yaml("""
            paths:
              - /photos
              - /documents
        """)
        cfg = load_config(p)
        assert cfg.paths == ["/photos", "/documents"]

    def test_yaml_workers_override(self):
        p = self._write_yaml("workers: 8\npaths:\n  - /x\n")
        cfg = load_config(p)
        assert cfg.workers == 8

    def test_yaml_bool_false(self):
        p = self._write_yaml("follow_symlinks: false\npaths:\n  - /x\n")
        cfg = load_config(p)
        assert cfg.follow_symlinks is False

    def test_yaml_bool_true(self):
        p = self._write_yaml("follow_symlinks: true\npaths:\n  - /x\n")
        cfg = load_config(p)
        assert cfg.follow_symlinks is True

    def test_missing_yaml_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/config.yaml")

    def test_invalid_yaml_raises(self):
        p = self._write_yaml(": invalid: yaml: [\n")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(p)

    def test_unknown_keys_ignored(self):
        p = self._write_yaml("paths:\n  - /x\nfuture_unknown_key: 42\n")
        cfg = load_config(p)  # should not raise
        assert cfg.paths == ["/x"]


class TestEnvOverrides:
    def test_paths_colon_separated(self, monkeypatch):
        monkeypatch.setenv("SDOG_PATHS", "/photos:/documents:/videos")
        cfg = load_config()
        assert cfg.paths == ["/photos", "/documents", "/videos"]

    def test_exclude_colon_separated(self, monkeypatch):
        monkeypatch.setenv("SDOG_EXCLUDE", "**/.cache/**:**/tmp/**")
        cfg = load_config()
        assert "**/.cache/**" in cfg.exclude
        assert "**/tmp/**" in cfg.exclude

    def test_int_override(self, monkeypatch):
        monkeypatch.setenv("SDOG_WORKERS", "16")
        cfg = load_config()
        assert cfg.workers == 16

    def test_bool_true_variants(self, monkeypatch):
        for val in ("1", "true", "True", "yes", "on"):
            monkeypatch.setenv("SDOG_FOLLOW_SYMLINKS", val)
            cfg = load_config()
            assert cfg.follow_symlinks is True, f"Failed for {val!r}"

    def test_bool_false_variants(self, monkeypatch):
        for val in ("0", "false", "False", "no", "off"):
            monkeypatch.setenv("SDOG_FOLLOW_SYMLINKS", val)
            cfg = load_config()
            assert cfg.follow_symlinks is False, f"Failed for {val!r}"

    def test_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("SDOG_WORKERS", "99")
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
        f.write("workers: 2\npaths:\n  - /x\n")
        f.close()
        cfg = load_config(f.name)
        assert cfg.workers == 99

    def test_invalid_int_raises(self, monkeypatch):
        monkeypatch.setenv("SDOG_WORKERS", "notanint")
        with pytest.raises(ConfigError, match="must be an integer"):
            load_config()

    def test_mcp_auth_token_from_env(self, monkeypatch):
        monkeypatch.setenv("SDOG_MCP_ENABLED", "true")
        monkeypatch.setenv("SDOG_MCP_AUTH_TOKEN", "secret")
        monkeypatch.setenv("SDOG_PATHS", "/x")
        cfg = load_config()
        assert cfg.mcp_auth_token == "secret"

    def test_trigger_cooldown_from_env(self, monkeypatch):
        monkeypatch.setenv("SDOG_PATHS", "/x")
        monkeypatch.setenv("SDOG_TRIGGER_COOLDOWN_S", "75")
        cfg = load_config()
        assert cfg.trigger_cooldown_s == 75

    def test_smtp_port_from_env(self, monkeypatch):
        monkeypatch.setenv("SDOG_PATHS", "/x")
        monkeypatch.setenv("SDOG_SMTP_PORT", "465")
        cfg = load_config()
        assert cfg.smtp_port == 465

    def test_http_basic_password_from_file_env(self, monkeypatch, tmp_path):
        secret = tmp_path / "http-basic-password"
        secret.write_text("supersecret\n")
        monkeypatch.setenv("SDOG_PATHS", "/x")
        monkeypatch.setenv("SDOG_HTTP_BASIC_ENABLED", "true")
        monkeypatch.setenv("SDOG_HTTP_BASIC_USERNAME", "admin")
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD_FILE", str(secret))
        cfg = load_config()
        assert cfg.http_basic_password == "supersecret"

    def test_direct_env_wins_over_file_env(self, monkeypatch, tmp_path):
        secret = tmp_path / "http-basic-password"
        secret.write_text("from-file\n")
        monkeypatch.setenv("SDOG_PATHS", "/x")
        monkeypatch.setenv("SDOG_HTTP_BASIC_ENABLED", "true")
        monkeypatch.setenv("SDOG_HTTP_BASIC_USERNAME", "admin")
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD_FILE", str(secret))
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD", "from-env")
        cfg = load_config()
        assert cfg.http_basic_password == "from-env"

    def test_missing_file_env_raises(self, monkeypatch):
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD_FILE", "/no/such/file")
        with pytest.raises(ConfigError, match="unreadable file"):
            load_config()

    def test_missing_file_env_is_ignored_when_direct_env_present(self, monkeypatch):
        monkeypatch.setenv("SDOG_PATHS", "/x")
        monkeypatch.setenv("SDOG_HTTP_BASIC_ENABLED", "true")
        monkeypatch.setenv("SDOG_HTTP_BASIC_USERNAME", "admin")
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD", "from-env")
        monkeypatch.setenv("SDOG_HTTP_BASIC_PASSWORD_FILE", "/no/such/file")

        cfg = load_config()

        assert cfg.http_basic_password == "from-env"


class TestValidation:
    def test_no_paths_raises(self):
        cfg = Config(paths=[])
        with pytest.raises(ConfigError, match="No scan paths"):
            cfg.validate()

    def test_bad_decode_depth_raises(self):
        cfg = Config(paths=["/x"], raw_decode_depth="turbo")
        with pytest.raises(ConfigError, match="raw_decode_depth"):
            cfg.validate()

    def test_zero_workers_raises(self):
        cfg = Config(paths=["/x"], workers=0)
        with pytest.raises(ConfigError, match="workers"):
            cfg.validate()

    def test_mcp_enabled_no_token_raises(self):
        cfg = Config(paths=["/x"], mcp_enabled=True, mcp_auth_token="")
        with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
            cfg.validate()

    def test_mcp_enabled_with_token_ok(self):
        cfg = Config(paths=["/x"], mcp_enabled=True, mcp_auth_token="tok")
        cfg.validate()  # must not raise

    def test_valid_config_passes(self):
        cfg = Config(paths=["/photos", "/documents"])
        cfg.validate()  # must not raise

    def test_http_basic_enabled_requires_username(self):
        cfg = Config(paths=["/x"], http_basic_enabled=True, http_basic_password="secret")
        with pytest.raises(ConfigError, match="http_basic_username"):
            cfg.validate()

    def test_http_basic_enabled_requires_password(self):
        cfg = Config(paths=["/x"], http_basic_enabled=True, http_basic_username="admin")
        with pytest.raises(ConfigError, match="http_basic_password"):
            cfg.validate()

    def test_http_basic_enabled_with_credentials_ok(self):
        cfg = Config(
            paths=["/x"],
            http_basic_enabled=True,
            http_basic_username="admin",
            http_basic_password="secret",
        )
        cfg.validate()

    def test_negative_trigger_cooldown_rejected(self):
        cfg = Config(paths=["/x"], trigger_cooldown_s=-1)
        with pytest.raises(ConfigError, match="trigger_cooldown_s"):
            cfg.validate()

    def test_invalid_smtp_tls_rejected(self):
        cfg = Config(paths=["/x"], smtp_tls="maybe")
        with pytest.raises(ConfigError, match="smtp_tls"):
            cfg.validate()

    def test_negative_smtp_port_rejected(self):
        cfg = Config(paths=["/x"], smtp_port=-25)
        with pytest.raises(ConfigError, match="smtp_port"):
            cfg.validate()

    def test_config_field_metadata_exposes_sets(self):
        metadata = Config.field_metadata()
        assert "trigger_cooldown_s" in metadata["editable"]
        assert "schedule" in metadata["editable"]
        assert "smtp_pass" in metadata["env_only"]
        assert "http_port" in metadata["restart_required"]
        assert "schedule" not in metadata["hidden"]


class TestPathAllowlist:
    def test_path_under_root_allowed(self):
        cfg = Config(paths=["/photos"])
        assert cfg.is_path_allowed("/photos/2024/img.cr2") is True

    def test_path_outside_root_denied(self):
        cfg = Config(paths=["/photos"])
        assert cfg.is_path_allowed("/etc/passwd") is False

    def test_exact_root_allowed(self):
        cfg = Config(paths=["/photos"])
        assert cfg.is_path_allowed("/photos") is True

    def test_multiple_roots(self):
        cfg = Config(paths=["/photos", "/documents"])
        assert cfg.is_path_allowed("/documents/report.pdf") is True
        assert cfg.is_path_allowed("/videos/movie.mp4") is False
