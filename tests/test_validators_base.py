"""Stage 4 tests — validator interface, registry, dependency checking, timeout."""

from __future__ import annotations

import time
from typing import ClassVar
from unittest.mock import patch

import pytest

from semanticdog.validators.base import (
    BaseValidator,
    DependencyReport,
    ValidationResult,
    VALID_STATUSES,
    _cli_version,
)
from semanticdog.validators import (
    _registry,
    get_validator,
    all_extensions,
    all_validators,
    register,
)


# ---------------------------------------------------------------------------
# Concrete stub for testing abstract interface
# ---------------------------------------------------------------------------

class _StubValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".stub", ".stb"})
    requires_cli: ClassVar[list[str]] = []
    optional_cli: ClassVar[list[str]] = []
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        return ValidationResult(status="ok")


class _SlowValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".slow"})
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        time.sleep(10)  # intentionally hangs
        return ValidationResult(status="ok")


# Module-level picklable worker functions (local closures can't cross process boundary)
def _pebble_run_stub(path: str) -> ValidationResult:
    return _StubValidator().validate(path)


def _pebble_hang(path: str) -> None:
    time.sleep(30)


class _HardDepValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".hard"})
    requires_cli: ClassVar[list[str]] = ["ffprobe"]
    optional_cli: ClassVar[list[str]] = ["exiftool"]

    def validate(self, path: str) -> ValidationResult:  # pragma: no cover
        return ValidationResult(status="ok")


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

class TestValidationResult:
    def test_all_valid_statuses_accepted(self):
        for s in VALID_STATUSES:
            r = ValidationResult(status=s)
            assert r.status == s

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid status"):
            ValidationResult(status="broken")

    def test_ok_property_true_only_for_ok(self):
        assert ValidationResult(status="ok").ok is True
        for s in VALID_STATUSES - {"ok"}:
            assert ValidationResult(status=s).ok is False

    def test_error_and_action_stored(self):
        r = ValidationResult(status="corrupt", error="EOF", suggested_action="restore")
        assert r.error == "EOF"
        assert r.suggested_action == "restore"

    def test_defaults_are_none(self):
        r = ValidationResult(status="ok")
        assert r.error is None
        assert r.suggested_action is None


# ---------------------------------------------------------------------------
# BaseValidator interface
# ---------------------------------------------------------------------------

class TestBaseValidator:
    def test_concrete_subclass_instantiates(self):
        v = _StubValidator()
        assert isinstance(v, BaseValidator)

    def test_abstract_validate_must_be_implemented(self):
        with pytest.raises(TypeError):
            BaseValidator()  # type: ignore[abstract]

    def test_extensions_is_classvars_frozenset(self):
        assert isinstance(_StubValidator.extensions, frozenset)

    def test_memory_category_default_is_low(self):
        assert _StubValidator.memory_category == "low"

    def test_memory_category_high(self):
        from semanticdog.validators.raw import RawValidator
        assert RawValidator.memory_category == "high"

    def test_requires_cli_default_empty(self):
        assert _StubValidator.requires_cli == []

    def test_validate_returns_result(self):
        r = _StubValidator().validate("/any/file.stub")
        assert isinstance(r, ValidationResult)


# ---------------------------------------------------------------------------
# check_dependencies
# ---------------------------------------------------------------------------

class TestCheckDependencies:
    def test_no_deps_returns_empty(self):
        reports = _StubValidator().check_dependencies()
        assert reports == []

    def test_required_tool_present(self):
        # 'python3' is always available
        class _PyValidator(BaseValidator):
            extensions = frozenset({".py3test"})
            requires_cli = ["python3"]
            def validate(self, path):
                return ValidationResult(status="ok")

        reports = _PyValidator().check_dependencies()
        assert len(reports) == 1
        r = reports[0]
        assert r.name == "python3"
        assert r.available is True
        assert r.required is True

    def test_missing_required_tool(self):
        class _MissingValidator(BaseValidator):
            extensions = frozenset({".miss"})
            requires_cli = ["__totally_nonexistent_tool__"]
            def validate(self, path):
                return ValidationResult(status="ok")

        reports = _MissingValidator().check_dependencies()
        r = reports[0]
        assert r.available is False
        assert r.required is True
        assert r.version is None

    def test_optional_tool_flagged_not_required(self):
        class _OptValidator(BaseValidator):
            extensions = frozenset({".opt"})
            optional_cli = ["__totally_nonexistent_tool__"]
            def validate(self, path):
                return ValidationResult(status="ok")

        reports = _OptValidator().check_dependencies()
        r = reports[0]
        assert r.required is False
        assert r.available is False

    def test_mixed_required_optional(self):
        reports = _HardDepValidator().check_dependencies()
        names = {r.name for r in reports}
        assert "ffprobe" in names
        assert "exiftool" in names
        req = {r.name: r.required for r in reports}
        assert req["ffprobe"] is True
        assert req["exiftool"] is False

    def test_dependency_report_dataclass(self):
        r = DependencyReport(name="ffprobe", available=True, version="5.0", required=True)
        assert r.name == "ffprobe"
        assert r.version == "5.0"


# ---------------------------------------------------------------------------
# _cli_version helper
# ---------------------------------------------------------------------------

class TestCliVersion:
    def test_existing_tool(self):
        v = _cli_version("python3")
        assert v is not None
        assert len(v) > 0

    def test_missing_tool_returns_none(self):
        assert _cli_version("__nonexistent_tool_xyz__") is None

    def test_timeout_returns_none(self):
        with patch("semanticdog.validators.base.subprocess.run", side_effect=Exception("timeout")):
            assert _cli_version("anything") is None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_register_decorator_adds_extensions(self):
        @register
        class _Tmp(BaseValidator):
            extensions = frozenset({".tmpfmt1", ".tmpfmt2"})
            def validate(self, path):
                return ValidationResult(status="ok")

        assert get_validator(".tmpfmt1") is _Tmp
        assert get_validator(".tmpfmt2") is _Tmp

    def test_lookup_case_insensitive(self):
        @register
        class _CaseFmt(BaseValidator):
            extensions = frozenset({".CASEFMT"})
            def validate(self, path):
                return ValidationResult(status="ok")

        assert get_validator(".casefmt") is _CaseFmt
        assert get_validator(".CASEFMT") is _CaseFmt

    def test_unknown_extension_returns_none(self):
        assert get_validator(".xyzunknown999") is None

    def test_all_extensions_returns_frozenset(self):
        exts = all_extensions()
        assert isinstance(exts, frozenset)

    def test_all_validators_deduplicated(self):
        """all_validators() must not contain the same class twice."""
        validators = all_validators()
        ids = [id(v) for v in validators]
        assert len(ids) == len(set(ids))

    def test_production_validators_registered(self):
        import semanticdog.validators.images    # noqa: F401
        import semanticdog.validators.raw       # noqa: F401
        import semanticdog.validators.documents  # noqa: F401
        import semanticdog.validators.media     # noqa: F401
        exts = all_extensions()
        for expected in (".jpg", ".png", ".tiff", ".cr2", ".pdf", ".mp4"):
            assert expected in exts, f"{expected} not registered"

    def test_jpeg_extensions_complete(self):
        import semanticdog.validators.images  # noqa: F401
        for ext in (".jpg", ".jpeg"):
            assert get_validator(ext) is not None

    def test_raw_extensions(self):
        import semanticdog.validators.raw  # noqa: F401
        for ext in (".cr2", ".cr3", ".nef", ".arw", ".orf", ".dng", ".raf"):
            assert get_validator(ext) is not None, f"{ext} not registered"

    def test_raw_validators_are_high_memory(self):
        import semanticdog.validators.raw  # noqa: F401
        v = get_validator(".cr2")
        assert v is not None
        assert v.memory_category == "high"


# ---------------------------------------------------------------------------
# pebble timeout integration
# ---------------------------------------------------------------------------

class TestPebbleTimeout:
    def test_normal_task_completes(self):
        """pebble runs validator task and returns result."""
        import multiprocessing
        from pebble import ProcessPool

        ctx = multiprocessing.get_context("spawn")
        with ProcessPool(max_workers=1, context=ctx) as pool:
            future = pool.schedule(_pebble_run_stub, args=("/fake.stub",), timeout=5)
            result = future.result()
        assert isinstance(result, ValidationResult)
        assert result.ok

    def test_timed_out_task_raises(self):
        """Task exceeding timeout raises TimeoutError."""
        import multiprocessing
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from pebble import ProcessPool

        ctx = multiprocessing.get_context("spawn")
        with ProcessPool(max_workers=1, context=ctx) as pool:
            future = pool.schedule(_pebble_hang, args=("/fake.slow",), timeout=1)
            with pytest.raises(FuturesTimeoutError):
                future.result()

    def test_timeout_result_maps_to_error_status(self):
        """Scanner pattern: TimeoutError → ValidationResult(status='error')."""
        import multiprocessing
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from pebble import ProcessPool

        ctx = multiprocessing.get_context("spawn")
        with ProcessPool(max_workers=1, context=ctx) as pool:
            future = pool.schedule(_pebble_hang, args=("/fake.slow",), timeout=1)
            try:
                future.result()
                result = None
            except FuturesTimeoutError:
                result = ValidationResult(
                    status="error",
                    error="validation timed out",
                    suggested_action="File may be severely corrupt or hang the decoder",
                )

        assert result is not None
        assert result.status == "error"
        assert result.ok is False
