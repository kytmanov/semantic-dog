"""Stage 10 tests — CLI interface."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semanticdog.cli import app
from tests.fixtures.generators import make_minimal_jpeg, make_minimal_png
from tests.conftest import strip_ansi

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg_yaml(tmp_path, **overrides) -> str:
    """Write a minimal config.yaml and return its path."""
    p = tmp_path / "config.yaml"
    scan_path = overrides.pop("paths", [str(tmp_path)])
    lines = ["paths:"]
    for sp in scan_path:
        lines.append(f"  - {sp}")
    for k, v in overrides.items():
        lines.append(f"{k}: {v}")
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _cfg_env(monkeypatch, tmp_path, **overrides):
    """Set SDOG_* env vars for a test."""
    monkeypatch.setenv("SDOG_PATHS", str(tmp_path))
    monkeypatch.setenv("SDOG_DB_PATH", str(tmp_path / "state.db"))
    for k, v in overrides.items():
        monkeypatch.setenv(f"SDOG_{k.upper()}", str(v))


# ---------------------------------------------------------------------------
# Command presence / help
# ---------------------------------------------------------------------------

class TestCliHelp:
    @pytest.mark.parametrize("cmd", [
        "scan", "estimate", "status", "list-scans", "report",
        "show-corrupt", "show-stats", "reset", "check-deps",
        "db-export", "db-import", "verify-hashes",
    ])
    def test_help_exits_zero(self, cmd):
        r = runner.invoke(app, [cmd, "--help"])
        assert r.exit_code == 0, f"{cmd} --help failed: {r.output}"

    def test_scan_has_dry_run(self):
        r = runner.invoke(app, ["scan", "--help"])
        assert "--dry-run" in strip_ansi(r.output)

    def test_scan_has_exclude(self):
        r = runner.invoke(app, ["scan", "--help"])
        assert "--exclude" in strip_ansi(r.output)

    def test_scan_has_resume(self):
        r = runner.invoke(app, ["scan", "--help"])
        assert "--resume" in strip_ansi(r.output)

    def test_db_import_has_force(self):
        r = runner.invoke(app, ["db-import", "--help"])
        assert "--force" in strip_ansi(r.output)

    def test_db_import_has_path_map(self):
        r = runner.invoke(app, ["db-import", "--help"])
        assert "--path-map" in strip_ansi(r.output)


# ---------------------------------------------------------------------------
# check-deps
# ---------------------------------------------------------------------------

class TestCheckDeps:
    def test_exits_zero(self):
        r = runner.invoke(app, ["check-deps"])
        # 0 = all hard deps present; 1 = something missing — both are valid runs
        assert r.exit_code in (0, 1)

    def test_shows_ffprobe(self):
        r = runner.invoke(app, ["check-deps"])
        assert "ffprobe" in r.output

    def test_shows_found_or_missing(self):
        r = runner.invoke(app, ["check-deps"])
        assert "found" in r.output or "missing" in r.output


# ---------------------------------------------------------------------------
# scan --dry-run
# ---------------------------------------------------------------------------

class TestScanDryRun:
    def test_dry_run_no_db_writes(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        make_minimal_jpeg(tmp_path / "img.jpg")
        r = runner.invoke(app, ["scan", "--dry-run"])
        assert r.exit_code == 0
        assert not (tmp_path / "state.db").exists()

    def test_dry_run_counts_files(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        make_minimal_jpeg(tmp_path / "a.jpg")
        make_minimal_png(tmp_path / "b.png")
        r = runner.invoke(app, ["scan", "--dry-run"])
        assert r.exit_code == 0
        assert "2" in r.output or ".jpg" in r.output

    def test_dry_run_with_config(self, tmp_path):
        cfg = _cfg_yaml(tmp_path, paths=[str(tmp_path)])
        make_minimal_jpeg(tmp_path / "img.jpg")
        r = runner.invoke(app, ["scan", "--dry-run", "--config", cfg])
        assert r.exit_code == 0


# ---------------------------------------------------------------------------
# scan (real)
# ---------------------------------------------------------------------------

class TestScanCommand:
    def test_scan_empty_dir_exits_zero(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["scan"])
        assert r.exit_code == 0

    def test_scan_with_valid_jpeg(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        make_minimal_jpeg(tmp_path / "img.jpg")
        r = runner.invoke(app, ["scan"])
        # exit 0 if all ok, 2 if corrupt found
        assert r.exit_code in (0, 2)
        assert "validated" in r.output or "Done" in r.output


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------

class TestEstimateCommand:
    def test_estimate_empty_dir(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["estimate"])
        assert r.exit_code == 0
        assert "0" in r.output

    def test_estimate_counts_jpeg(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        make_minimal_jpeg(tmp_path / "img.jpg")
        r = runner.invoke(app, ["estimate"])
        assert r.exit_code == 0


# ---------------------------------------------------------------------------
# status / list-scans
# ---------------------------------------------------------------------------

class TestStatusCommands:
    def test_status_no_scans(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["status"])
        assert r.exit_code == 0
        assert "No scans" in r.output

    def test_list_scans_no_scans(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["list-scans"])
        assert r.exit_code == 0
        assert "No scans" in r.output

    def test_status_after_scan(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        runner.invoke(app, ["scan"])  # run a scan first
        r = runner.invoke(app, ["status"])
        assert r.exit_code == 0
        # Should show scan ID (UUID format)
        assert "Last scan ID" in r.output


# ---------------------------------------------------------------------------
# report / show-corrupt / show-stats
# ---------------------------------------------------------------------------

class TestReportCommands:
    def test_report_empty_db(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["report"])
        assert r.exit_code == 0

    def test_report_json_format(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["report", "--format", "json"])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert "stats" in data

    def test_show_corrupt_empty(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["show-corrupt"])
        assert r.exit_code == 0
        assert "No corrupt" in r.output

    def test_show_stats_empty(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["show-stats"])
        assert r.exit_code == 0
        assert "0" in r.output


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

class TestResetCommand:
    def test_reset_with_yes_flag(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["reset", "--yes"])
        assert r.exit_code == 0
        assert "Deleted" in r.output

    def test_reset_without_yes_aborts(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["reset"], input="n\n")
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# db-export / db-import
# ---------------------------------------------------------------------------

class TestDbExportImport:
    def test_export_empty_db(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        r = runner.invoke(app, ["db-export"])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data == []

    def test_export_to_file(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        out = tmp_path / "export.json"
        r = runner.invoke(app, ["db-export", "--output", str(out)])
        assert r.exit_code == 0
        assert out.exists()
        assert json.loads(out.read_text()) == []

    def test_import_empty_json(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        inp = tmp_path / "import.json"
        inp.write_text("[]")
        r = runner.invoke(app, ["db-import", "--input", str(inp)])
        assert r.exit_code == 0
        assert "0" in r.output

    def test_import_invalid_json(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        inp = tmp_path / "bad.json"
        inp.write_text("not json {")
        r = runner.invoke(app, ["db-import", "--input", str(inp)])
        assert r.exit_code != 0

    def test_import_bad_path_map(self, tmp_path, monkeypatch):
        _cfg_env(monkeypatch, tmp_path)
        inp = tmp_path / "empty.json"
        inp.write_text("[]")
        r = runner.invoke(app, ["db-import", "--input", str(inp), "--path-map", "invalid"])
        assert r.exit_code != 0
