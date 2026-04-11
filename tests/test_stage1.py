"""Stage 1 tests — scaffolding, registry, ValidationResult, CLI skeleton."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from semanticdog.cli import app
from semanticdog.exceptions import (
    SdogError, ConfigError, DatabaseError, ScanError, DependencyError, LockError,
)
from semanticdog.validators.base import ValidationResult, BaseValidator, DependencyReport
from semanticdog.validators import (
    get_validator, all_extensions, all_validators, register,
)
import semanticdog.validators.images    # noqa: F401 — trigger registration
import semanticdog.validators.raw       # noqa: F401
import semanticdog.validators.documents  # noqa: F401
import semanticdog.validators.media     # noqa: F401
from tests.conftest import strip_ansi

runner = CliRunner()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TestExceptions:
    def test_hierarchy(self):
        assert issubclass(ConfigError, SdogError)
        assert issubclass(DatabaseError, SdogError)
        assert issubclass(ScanError, SdogError)
        assert issubclass(DependencyError, SdogError)
        assert issubclass(LockError, SdogError)

    def test_raise_catch(self):
        with pytest.raises(SdogError):
            raise ConfigError("bad config")


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

class TestValidationResult:
    def test_ok_property_derived(self):
        r = ValidationResult(status="ok")
        assert r.ok is True

    def test_ok_property_false_for_corrupt(self):
        r = ValidationResult(status="corrupt", error="truncated")
        assert r.ok is False

    def test_ok_property_false_for_unreadable(self):
        assert ValidationResult(status="unreadable").ok is False

    def test_ok_cannot_desync_with_status(self):
        """ok is always derived from status — no separate bool field."""
        r = ValidationResult(status="corrupt")
        # There is no way to set ok=True while status='corrupt'
        assert r.ok is False

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid status"):
            ValidationResult(status="broken")

    def test_all_valid_statuses(self):
        for s in ("ok", "corrupt", "unsupported", "unreadable", "error"):
            r = ValidationResult(status=s)
            assert r.status == s

    def test_optional_fields_default_none(self):
        r = ValidationResult(status="ok")
        assert r.error is None
        assert r.suggested_action is None

    def test_fields_set(self):
        r = ValidationResult(
            status="corrupt",
            error="truncated at byte 512",
            suggested_action="Re-transfer from source",
        )
        assert r.error == "truncated at byte 512"
        assert r.suggested_action == "Re-transfer from source"


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_jpeg_registered(self):
        assert get_validator(".jpg") is not None
        assert get_validator(".jpeg") is not None

    def test_raw_formats_registered(self):
        for ext in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf"):
            assert get_validator(ext) is not None, f"{ext} not registered"

    def test_document_formats_registered(self):
        for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls"):
            assert get_validator(ext) is not None, f"{ext} not registered"

    def test_video_formats_registered(self):
        for ext in (".mp4", ".mov", ".mkv"):
            assert get_validator(ext) is not None, f"{ext} not registered"

    def test_audio_formats_registered(self):
        for ext in (".mp3", ".flac", ".wav"):
            assert get_validator(ext) is not None, f"{ext} not registered"

    def test_unknown_extension_returns_none(self):
        assert get_validator(".xyz123") is None

    def test_case_insensitive(self):
        assert get_validator(".JPG") is get_validator(".jpg")
        assert get_validator(".CR2") is get_validator(".cr2")

    def test_all_extensions_non_empty(self):
        exts = all_extensions()
        assert len(exts) > 20

    def test_all_validators_deduplicated(self):
        validators = all_validators()
        # RawValidator covers 10 extensions but should appear once
        classes = [type(v()) if not isinstance(v, type) else v for v in validators]
        assert len(classes) == len(set(classes))

    def test_memory_categories_valid(self):
        valid = {"low", "medium", "high"}
        for v in all_validators():
            assert v.memory_category in valid, f"{v.__name__} has invalid memory_category"

    def test_raw_validator_is_high_memory(self):
        from semanticdog.validators.raw import RawValidator
        assert RawValidator.memory_category == "high"

    def test_extensions_are_frozensets(self):
        for v in all_validators():
            assert isinstance(v.extensions, frozenset), f"{v.__name__}.extensions must be ClassVar frozenset"


# ---------------------------------------------------------------------------
# BaseValidator.check_dependencies
# ---------------------------------------------------------------------------

class TestDependencyCheck:
    def test_returns_list_of_reports(self):
        from semanticdog.validators.images import JpegValidator
        reports = JpegValidator().check_dependencies()
        assert isinstance(reports, list)
        assert all(isinstance(r, DependencyReport) for r in reports)

    def test_jpeginfo_is_optional(self):
        from semanticdog.validators.images import JpegValidator
        reports = JpegValidator().check_dependencies()
        jpeginfo = next((r for r in reports if r.name == "jpeginfo"), None)
        assert jpeginfo is not None
        assert jpeginfo.required is False

    def test_ffprobe_is_required_for_video(self):
        from semanticdog.validators.media import VideoValidator
        reports = VideoValidator().check_dependencies()
        ffprobe = next((r for r in reports if r.name == "ffprobe"), None)
        assert ffprobe is not None
        assert ffprobe.required is True


# ---------------------------------------------------------------------------
# CLI skeleton
# ---------------------------------------------------------------------------

class TestCliSkeleton:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "scan" in result.output

    def test_all_commands_present(self):
        result = runner.invoke(app, ["--help"])
        for cmd in ("scan", "estimate", "status", "report", "reset", "check-deps"):
            assert cmd in result.output, f"Command '{cmd}' missing from --help"

    def test_scan_help(self):
        result = runner.invoke(app, ["scan", "--help"])
        assert result.exit_code == 0
        out = strip_ansi(result.output)
        assert "--dry-run" in out
        assert "--strict" in out

    def test_scan_exits_nonzero_without_config(self):
        # scan without config or SDOG_PATHS fails with config error
        result = runner.invoke(app, ["scan"])
        assert result.exit_code != 0

    def test_db_import_has_force_flag(self):
        result = runner.invoke(app, ["db-import", "--help"])
        assert "--force" in strip_ansi(result.output)

    def test_db_import_has_path_map(self):
        result = runner.invoke(app, ["db-import", "--help"])
        assert "--path-map" in strip_ansi(result.output)

    def test_check_deps_runs(self):
        result = runner.invoke(app, ["check-deps"])
        # exit 0 = all hard deps present; exit 1 = something missing — both are valid runs
        assert result.exit_code in (0, 1)
        assert "ffprobe" in result.output

    def test_verify_hashes_present(self):
        result = runner.invoke(app, ["verify-hashes", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# MCP skeleton imports
# ---------------------------------------------------------------------------

class TestMcpSkeleton:
    def test_mcp_server_importable(self):
        from semanticdog.mcp_server import mcp_server, sse_transport
        assert mcp_server is not None
        assert sse_transport is not None

    def test_server_importable(self):
        from semanticdog.server import app, _scan_lock
        assert app is not None

    def test_scan_lock_is_asyncio_lock(self):
        import asyncio
        from semanticdog.server import _scan_lock
        assert isinstance(_scan_lock, asyncio.Lock)
