"""Stage 5 tests — image validators (JPEG, PNG, TIFF, HEIC/AVIF, WebP)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from semanticdog.validators.images import (
    JpegValidator,
    PngValidator,
    TiffValidator,
    HeicValidator,
    WebpValidator,
)
from tests.fixtures.generators import (
    make_minimal_jpeg,
    make_truncated_jpeg,
    make_bad_sof_marker_jpeg,
    make_minimal_png,
    make_truncated_png,
    make_minimal_tiff,
    make_truncated_tiff,
    make_minimal_webp,
    make_not_an_image,
    make_zero_byte,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_ok(result):
    assert result.status == "ok", f"Expected ok, got {result.status!r}: {result.error}"

def _assert_corrupt(result):
    assert result.status == "corrupt", f"Expected corrupt, got {result.status!r}: {result.error}"

def _assert_unreadable(result):
    assert result.status == "unreadable", f"Expected unreadable, got {result.status!r}"


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

class TestJpegValidator:
    def test_valid_jpeg_ok(self, tmp_path):
        p = make_minimal_jpeg(tmp_path / "valid.jpg")
        r = JpegValidator().validate(str(p))
        _assert_ok(r)

    def test_truncated_jpeg_corrupt(self, tmp_path):
        p = make_truncated_jpeg(tmp_path / "trunc.jpg")
        r = JpegValidator().validate(str(p))
        _assert_corrupt(r)

    def test_zero_byte_corrupt(self, tmp_path):
        p = make_zero_byte(tmp_path / "empty.jpg")
        r = JpegValidator().validate(str(p))
        assert r.status in ("corrupt", "unreadable")

    def test_not_an_image_corrupt(self, tmp_path):
        p = make_not_an_image(tmp_path / "fake.jpg")
        r = JpegValidator().validate(str(p))
        _assert_corrupt(r)

    def test_missing_file_unreadable(self, tmp_path):
        r = JpegValidator().validate(str(tmp_path / "ghost.jpg"))
        _assert_unreadable(r)

    def test_jpeg_extension_registered(self):
        from semanticdog.validators import get_validator
        assert get_validator(".jpg") is JpegValidator
        assert get_validator(".jpeg") is JpegValidator

    def test_memory_category_low(self):
        assert JpegValidator.memory_category == "low"

    def test_result_has_suggested_action_on_corrupt(self, tmp_path):
        p = make_truncated_jpeg(tmp_path / "t.jpg")
        r = JpegValidator().validate(str(p))
        if r.status == "corrupt":
            assert r.suggested_action is not None

    def test_valid_jpeg_ok_property(self, tmp_path):
        p = make_minimal_jpeg(tmp_path / "v.jpg")
        r = JpegValidator().validate(str(p))
        assert r.ok is (r.status == "ok")


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

class TestPngValidator:
    def test_valid_png_ok(self, tmp_path):
        p = make_minimal_png(tmp_path / "valid.png")
        r = PngValidator().validate(str(p))
        _assert_ok(r)

    def test_truncated_png_corrupt(self, tmp_path):
        p = make_truncated_png(tmp_path / "trunc.png")
        r = PngValidator().validate(str(p))
        _assert_corrupt(r)

    def test_zero_byte_png(self, tmp_path):
        p = make_zero_byte(tmp_path / "empty.png")
        r = PngValidator().validate(str(p))
        assert r.status in ("corrupt", "unreadable")

    def test_not_an_image_corrupt(self, tmp_path):
        p = make_not_an_image(tmp_path / "fake.png")
        r = PngValidator().validate(str(p))
        _assert_corrupt(r)

    def test_missing_file_unreadable(self, tmp_path):
        r = PngValidator().validate(str(tmp_path / "ghost.png"))
        _assert_unreadable(r)

    def test_png_extension_registered(self):
        from semanticdog.validators import get_validator
        assert get_validator(".png") is PngValidator

    def test_memory_category_low(self):
        assert PngValidator.memory_category == "low"


# ---------------------------------------------------------------------------
# TIFF
# ---------------------------------------------------------------------------

class TestTiffValidator:
    def test_valid_tiff_ok(self, tmp_path):
        p = make_minimal_tiff(tmp_path / "valid.tiff")
        r = TiffValidator().validate(str(p))
        _assert_ok(r)

    def test_valid_tif_extension(self, tmp_path):
        p = make_minimal_tiff(tmp_path / "valid.tif")
        r = TiffValidator().validate(str(p))
        _assert_ok(r)

    def test_truncated_tiff_corrupt(self, tmp_path):
        p = make_truncated_tiff(tmp_path / "trunc.tiff")
        r = TiffValidator().validate(str(p))
        _assert_corrupt(r)

    def test_not_an_image_corrupt(self, tmp_path):
        p = make_not_an_image(tmp_path / "fake.tiff")
        r = TiffValidator().validate(str(p))
        _assert_corrupt(r)

    def test_missing_file_unreadable(self, tmp_path):
        r = TiffValidator().validate(str(tmp_path / "ghost.tiff"))
        _assert_unreadable(r)

    def test_tiff_extensions_registered(self):
        from semanticdog.validators import get_validator
        assert get_validator(".tiff") is TiffValidator
        assert get_validator(".tif") is TiffValidator

    def test_memory_category_medium(self):
        assert TiffValidator.memory_category == "medium"


# ---------------------------------------------------------------------------
# WebP
# ---------------------------------------------------------------------------

class TestWebpValidator:
    def test_valid_webp_ok(self, tmp_path):
        p = make_minimal_webp(tmp_path / "valid.webp")
        r = WebpValidator().validate(str(p))
        _assert_ok(r)

    def test_not_a_webp_corrupt(self, tmp_path):
        p = make_not_an_image(tmp_path / "fake.webp")
        r = WebpValidator().validate(str(p))
        _assert_corrupt(r)

    def test_missing_file_unreadable(self, tmp_path):
        r = WebpValidator().validate(str(tmp_path / "ghost.webp"))
        _assert_unreadable(r)

    def test_webp_extension_registered(self):
        from semanticdog.validators import get_validator
        assert get_validator(".webp") is WebpValidator

    def test_memory_category_low(self):
        assert WebpValidator.memory_category == "low"

    def test_zero_byte_webp(self, tmp_path):
        p = make_zero_byte(tmp_path / "empty.webp")
        r = WebpValidator().validate(str(p))
        assert r.status in ("corrupt", "unreadable")


# ---------------------------------------------------------------------------
# HEIC / AVIF (pillow-heif)
# ---------------------------------------------------------------------------

class TestHeicValidator:
    def test_missing_file_unreadable(self, tmp_path):
        r = HeicValidator().validate(str(tmp_path / "ghost.heic"))
        # Either unreadable (file not found) or error (pillow-heif missing)
        assert r.status in ("unreadable", "error")

    def test_not_a_heic_corrupt_or_unsupported(self, tmp_path):
        p = make_not_an_image(tmp_path / "fake.heic")
        try:
            import pillow_heif  # noqa: F401
        except ImportError:
            pytest.skip("pillow-heif not installed")
        r = HeicValidator().validate(str(p))
        assert r.status in ("corrupt", "unsupported", "unreadable")

    def test_heic_extensions_registered(self):
        from semanticdog.validators import get_validator
        assert get_validator(".heic") is HeicValidator
        assert get_validator(".heif") is HeicValidator
        assert get_validator(".avif") is HeicValidator

    def test_memory_category_medium(self):
        assert HeicValidator.memory_category == "medium"

    def test_no_pillow_heif_returns_error(self, tmp_path, monkeypatch):
        """If pillow-heif missing, validator returns error not exception."""
        p = make_not_an_image(tmp_path / "test.heic")
        # Hide pillow_heif from import
        monkeypatch.setitem(sys.modules, "pillow_heif", None)  # type: ignore[arg-type]
        r = HeicValidator().validate(str(p))
        assert r.status == "error"
        assert "pillow-heif" in (r.error or "")


# ---------------------------------------------------------------------------
# Cross-cutting: all image validators return ValidationResult
# ---------------------------------------------------------------------------

class TestImageValidatorsContract:
    @pytest.mark.parametrize("validator_cls,ext,generator", [
        (JpegValidator, ".jpg", make_minimal_jpeg),
        (PngValidator, ".png", make_minimal_png),
        (TiffValidator, ".tiff", make_minimal_tiff),
        (WebpValidator, ".webp", make_minimal_webp),
    ])
    def test_returns_validation_result(self, validator_cls, ext, generator, tmp_path):
        from semanticdog.validators.base import ValidationResult
        p = generator(tmp_path / f"test{ext}")
        r = validator_cls().validate(str(p))
        assert isinstance(r, ValidationResult)

    @pytest.mark.parametrize("validator_cls,ext,generator", [
        (JpegValidator, ".jpg", make_not_an_image),
        (PngValidator, ".png", make_not_an_image),
        (TiffValidator, ".tiff", make_not_an_image),
        (WebpValidator, ".webp", make_not_an_image),
    ])
    def test_garbage_content_never_raises(self, validator_cls, ext, generator, tmp_path):
        """Validators must never raise — always return a result."""
        p = generator(tmp_path / f"garbage{ext}")
        try:
            r = validator_cls().validate(str(p))
            assert r.status in ("ok", "corrupt", "unsupported", "unreadable", "error")
        except Exception as exc:
            pytest.fail(f"{validator_cls.__name__} raised {type(exc).__name__}: {exc}")
