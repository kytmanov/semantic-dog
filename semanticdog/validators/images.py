"""Image validators: JPEG, PNG, TIFF, HEIC, AVIF, WebP."""

from __future__ import annotations

import subprocess
from typing import ClassVar

from PIL import Image, UnidentifiedImageError

from .base import BaseValidator, ValidationResult
from . import register


def _run_cli(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run CLI tool, return (returncode, combined output). shell=False always."""
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -1, "timeout"


@register
class JpegValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".jpg", ".jpeg"})
    optional_cli: ClassVar[list[str]] = ["jpeginfo"]
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        # 1. jpeginfo (soft dep) — catches truncation and bad Huffman tables
        # jpeginfo exits 1 for both WARNINGs (benign) and ERRORs (real corruption)
        rc, out = _run_cli(["jpeginfo", "-c", path])
        if rc == -1:
            pass  # jpeginfo not installed — skip
        elif rc != 0 and "ERROR" in out:
            return ValidationResult(
                status="corrupt",
                error=out or "jpeginfo reported error",
                suggested_action="Restore from backup or re-download",
            )

        # 2. Pillow — full pixel decode
        try:
            with Image.open(path) as img:
                img.load()
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except (OSError, UnidentifiedImageError) as e:
            return ValidationResult(
                status="corrupt",
                error=str(e),
                suggested_action="Restore from backup or re-download",
            )

        return ValidationResult(status="ok")


@register
class PngValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".png"})
    optional_cli: ClassVar[list[str]] = ["pngcheck"]
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        import os
        if not os.path.exists(path):
            return ValidationResult(status="unreadable", error=f"[Errno 2] No such file or directory: '{path}'")

        # 1. pngcheck (soft dep)
        rc, out = _run_cli(["pngcheck", path])
        if rc == -1:
            pass  # not installed
        elif rc != 0:
            return ValidationResult(
                status="corrupt",
                error=out or "pngcheck reported error",
                suggested_action="Restore from backup or re-download",
            )

        # 2. Pillow
        try:
            with Image.open(path) as img:
                img.load()
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except (OSError, UnidentifiedImageError) as e:
            return ValidationResult(
                status="corrupt",
                error=str(e),
                suggested_action="Restore from backup or re-download",
            )

        return ValidationResult(status="ok")


@register
class TiffValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".tif", ".tiff"})
    optional_cli: ClassVar[list[str]] = ["exiftool"]
    memory_category: ClassVar[str] = "medium"

    def validate(self, path: str) -> ValidationResult:
        # 1. Pillow decode — primary check
        try:
            with Image.open(path) as img:
                img.load()
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except (OSError, UnidentifiedImageError) as e:
            return ValidationResult(
                status="corrupt",
                error=str(e),
                suggested_action="Restore from backup; TIFF IFD chain may be damaged",
            )

        # 2. exiftool -validate (soft dep) — catches corrupt IFD chains Pillow misses
        rc, out = _run_cli(["exiftool", "-validate", "-warning", path])
        if rc not in (-1, 0):
            return ValidationResult(
                status="corrupt",
                error=out or "exiftool validation failed",
                suggested_action="Restore from backup; TIFF IFD chain may be damaged",
            )

        return ValidationResult(status="ok")


@register
class HeicValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".heic", ".heif", ".avif"})
    memory_category: ClassVar[str] = "medium"

    def validate(self, path: str) -> ValidationResult:
        try:
            import pillow_heif  # noqa: F401 — registers HEIF/AVIF opener
        except ImportError:
            return ValidationResult(
                status="error",
                error="pillow-heif not installed",
                suggested_action="pip install pillow-heif",
            )

        try:
            with Image.open(path) as img:
                img.load()
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except (OSError, UnidentifiedImageError) as e:
            err = str(e)
            # pillow-heif surfaces unsupported codec as specific messages
            if "unsupported" in err.lower() or "codec" in err.lower():
                return ValidationResult(
                    status="unsupported",
                    error=err,
                    suggested_action="Update pillow-heif / libheif",
                )
            return ValidationResult(
                status="corrupt",
                error=err,
                suggested_action="Restore from backup or re-download",
            )

        return ValidationResult(status="ok")


@register
class WebpValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".webp"})
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        try:
            with Image.open(path) as img:
                img.load()
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except (OSError, UnidentifiedImageError) as e:
            return ValidationResult(
                status="corrupt",
                error=str(e),
                suggested_action="Restore from backup or re-download",
            )

        return ValidationResult(status="ok")
