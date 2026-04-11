"""Stage 6 tests — RAW photo validators."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from semanticdog.validators.raw import RawValidator
from tests.fixtures.generators import make_truncated_raw, make_zero_byte, make_not_an_image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(r): assert r.status == "ok", f"Expected ok, got {r.status!r}: {r.error}"
def _corrupt(r): assert r.status == "corrupt", f"Expected corrupt, got {r.status!r}: {r.error}"
def _unsupported(r): assert r.status == "unsupported", f"Expected unsupported, got {r.status!r}"
def _unreadable(r): assert r.status == "unreadable", f"Expected unreadable, got {r.status!r}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_raw(tmp_path):
    """A fake .cr2 file with garbage content (not a real RAW)."""
    p = tmp_path / "fake.cr2"
    p.write_bytes(b"II*\x00" + b"\x00" * 100)  # TIFF-ish header, garbage body
    return str(p)


@pytest.fixture
def fake_dng(tmp_path):
    """A fake .dng file."""
    p = tmp_path / "fake.dng"
    p.write_bytes(b"II*\x00" + b"\x00" * 100)
    return str(p)


# ---------------------------------------------------------------------------
# Registration + metadata
# ---------------------------------------------------------------------------

class TestRawValidatorMeta:
    def test_all_raw_extensions_registered(self):
        from semanticdog.validators import get_validator
        for ext in (".cr2", ".cr3", ".nef", ".nrw", ".arw", ".orf", ".rw2", ".pef", ".dng", ".raf"):
            assert get_validator(ext) is RawValidator, f"{ext} not registered"

    def test_memory_category_high(self):
        assert RawValidator.memory_category == "high"

    def test_optional_exiftool(self):
        assert "exiftool" in RawValidator.optional_cli

    def test_no_required_cli(self):
        assert RawValidator.requires_cli == []


# ---------------------------------------------------------------------------
# rawpy not installed
# ---------------------------------------------------------------------------

class TestRawpyMissing:
    def test_rawpy_missing_returns_error(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "rawpy", None)  # type: ignore[arg-type]
        p = tmp_path / "test.cr2"
        p.write_bytes(b"\x00" * 10)
        r = RawValidator().validate(str(p))
        assert r.status == "error"
        assert "rawpy" in (r.error or "")


# ---------------------------------------------------------------------------
# Happy path — mocked rawpy
# ---------------------------------------------------------------------------

class TestRawValidatorHappyPath:
    def _mock_rawpy(self):
        """Build a mock rawpy module that succeeds."""
        mock_raw = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_raw)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_rawpy = MagicMock()
        mock_rawpy.imread.return_value = mock_ctx
        # Attach exception classes
        mock_rawpy.LibRawFileUnsupportedError = Exception
        mock_rawpy.LibRawIOError = Exception
        mock_rawpy.LibRawDataError = Exception
        return mock_rawpy

    def test_valid_raw_returns_ok(self, fake_raw):
        mock_rawpy = self._mock_rawpy()
        # Give each exception its own distinct type so except clauses don't collide
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})
        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw)
        _ok(r)

    def test_full_decode_depth_calls_postprocess(self, fake_raw):
        mock_raw = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_raw)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_rawpy = MagicMock()
        mock_rawpy.imread.return_value = mock_ctx
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw, decode_depth="full")
        _ok(r)
        mock_raw.postprocess.assert_called_once_with(half_size=True, output_bps=8)

    def test_structure_depth_no_postprocess(self, fake_raw):
        mock_raw = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_raw)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_rawpy = MagicMock()
        mock_rawpy.imread.return_value = mock_ctx
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            RawValidator().validate(fake_raw, decode_depth="structure")
        mock_raw.postprocess.assert_not_called()


# ---------------------------------------------------------------------------
# Error paths — mocked rawpy exceptions
# ---------------------------------------------------------------------------

class TestRawValidatorErrors:
    def _make_rawpy_mock(self, raise_exc):
        """rawpy that raises raise_exc on imread."""
        UnsupportedErr = type("LibRawFileUnsupportedError", (Exception,), {})
        IOErr = type("LibRawIOError", (Exception,), {})
        DataErr = type("LibRawDataError", (Exception,), {})

        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = UnsupportedErr
        mock_rawpy.LibRawIOError = IOErr
        mock_rawpy.LibRawDataError = DataErr

        exc_instance = raise_exc("simulated error")
        mock_rawpy.imread.side_effect = exc_instance
        return mock_rawpy

    def test_unsupported_returns_unsupported(self, fake_raw):
        UnsupportedErr = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy = self._make_rawpy_mock(UnsupportedErr)
        mock_rawpy.LibRawFileUnsupportedError = UnsupportedErr

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw)
        _unsupported(r)
        assert "Update rawpy" in (r.suggested_action or "")

    def test_io_error_returns_corrupt(self, fake_raw):
        IOErr = type("LibRawIOError", (Exception,), {})
        mock_rawpy = self._make_rawpy_mock(IOErr)
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = IOErr
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw)
        _corrupt(r)

    def test_data_error_returns_corrupt(self, fake_raw):
        DataErr = type("LibRawDataError", (Exception,), {})
        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = DataErr
        mock_rawpy.imread.side_effect = DataErr("bad data")

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw)
        _corrupt(r)

    def test_missing_file_unreadable(self, tmp_path):
        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})
        mock_rawpy.imread.side_effect = FileNotFoundError("no such file")

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(str(tmp_path / "ghost.cr2"))
        _unreadable(r)

    def test_os_error_unreadable(self, fake_raw):
        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})
        mock_rawpy.imread.side_effect = OSError("permission denied")

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw)
        _unreadable(r)


# ---------------------------------------------------------------------------
# DNG fallback path
# ---------------------------------------------------------------------------

class TestDngFallback:
    def test_dng_unsupported_by_rawpy_falls_back_to_pillow_ok(self, tmp_path):
        """LibRawFileUnsupportedError on .dng → Pillow fallback → ok."""
        from PIL import Image
        import io as _io

        # Write a valid DNG-named TIFF (Pillow can open it)
        buf = _io.BytesIO()
        Image.new("RGB", (1, 1)).save(buf, format="TIFF")
        p = tmp_path / "valid.dng"
        p.write_bytes(buf.getvalue())

        UnsupportedErr = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = UnsupportedErr
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})
        mock_rawpy.imread.side_effect = UnsupportedErr("unsupported")

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(str(p))
        _ok(r)

    def test_dng_unsupported_pillow_also_fails_returns_unsupported(self, tmp_path):
        """LibRawFileUnsupportedError on .dng + Pillow also fails → unsupported."""
        p = tmp_path / "garbage.dng"
        p.write_bytes(b"not a tiff not a dng just garbage")

        UnsupportedErr = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = UnsupportedErr
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})
        mock_rawpy.imread.side_effect = UnsupportedErr("unsupported")

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(str(p))
        _unsupported(r)
        assert r.error is not None

    def test_non_dng_unsupported_no_fallback(self, fake_raw):
        """LibRawFileUnsupportedError on .cr2 → unsupported, no Pillow attempt."""
        UnsupportedErr = type("LibRawFileUnsupportedError", (Exception,), {})
        mock_rawpy = MagicMock()
        mock_rawpy.LibRawFileUnsupportedError = UnsupportedErr
        mock_rawpy.LibRawIOError = type("LibRawIOError", (Exception,), {})
        mock_rawpy.LibRawDataError = type("LibRawDataError", (Exception,), {})
        mock_rawpy.imread.side_effect = UnsupportedErr("unsupported")

        with patch.dict("sys.modules", {"rawpy": mock_rawpy}):
            r = RawValidator().validate(fake_raw)
        _unsupported(r)


# ---------------------------------------------------------------------------
# Contract: never raises
# ---------------------------------------------------------------------------

class TestRawContract:
    def test_never_raises_on_garbage(self, tmp_path):
        """RawValidator must never raise — always return ValidationResult."""
        p = make_not_an_image(tmp_path / "garbage.cr2")
        # Use real rawpy if available, otherwise mock a hard failure
        try:
            import rawpy  # noqa: F401
            r = RawValidator().validate(str(p))
        except NotImplementedError:
            pytest.skip("rawpy unavailable in environment")
        assert r.status in ("ok", "corrupt", "unsupported", "unreadable", "error")
