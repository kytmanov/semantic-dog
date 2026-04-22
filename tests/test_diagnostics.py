"""Stage 14 tests — setup diagnostics."""

from __future__ import annotations

from pathlib import Path

from semanticdog.config import Config
from semanticdog.runtime import AppRuntime
from semanticdog.services.diagnostics import collect_readiness, collect_setup_diagnostics


class TestDiagnostics:
    def test_collect_setup_diagnostics_reports_missing_root(self, tmp_path):
        missing = tmp_path / "missing"
        runtime = AppRuntime(
            config_path=str(tmp_path / "config.yaml"),
            cfg=Config(paths=[str(missing)], db_path=str(tmp_path / "state.db")),
        )

        result = collect_setup_diagnostics(runtime)

        assert result["scan_roots"][0]["exists"] is False
        assert any("Scan root missing" in warning for warning in result["warnings"])

    def test_collect_setup_diagnostics_reports_writable_db_parent(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("paths: []\n")
        runtime = AppRuntime(
            config_path=str(cfg_path),
            cfg=Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state" / "state.db")),
        )

        result = collect_setup_diagnostics(runtime)

        assert result["config"]["exists"] is True
        assert result["db"]["parent_writable"] is True

    def test_collect_setup_diagnostics_includes_dependencies(self, tmp_path):
        runtime = AppRuntime(
            config_path=str(tmp_path / "config.yaml"),
            cfg=Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db")),
        )

        result = collect_setup_diagnostics(runtime)

        assert result["dependencies"] != []

    def test_collect_readiness_reports_ready_for_writable_runtime(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths: []\n")
        runtime = AppRuntime(
            config_path=str(config_path),
            cfg=Config(paths=[str(tmp_path)], db_path=str(tmp_path / "state.db")),
            db=object(),
        )

        result = collect_readiness(runtime)

        assert result["ready"] is True
        assert result["checks"]["config_valid"] is True
        assert result["checks"]["db_parent_writable"] is True

    def test_collect_readiness_reports_missing_scan_roots(self, tmp_path):
        missing = tmp_path / "missing"
        runtime = AppRuntime(
            config_path=str(tmp_path / "config.yaml"),
            cfg=Config(paths=[str(missing)], db_path=str(tmp_path / "state.db")),
            db=object(),
        )

        result = collect_readiness(runtime)

        assert result["ready"] is False
        assert result["checks"]["scan_roots_accessible"] is False
