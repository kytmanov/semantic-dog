"""RAW photo validators: CR2, CR3, NEF, ARW, ORF, RW2, PEF, DNG, RAF, NRW."""

from __future__ import annotations

from typing import ClassVar

from .base import BaseValidator, ValidationResult
from . import register


@register
class RawValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({
        ".cr2", ".cr3", ".nef", ".nrw",
        ".arw", ".orf", ".rw2", ".pef",
        ".dng", ".raf",
    })
    optional_cli: ClassVar[list[str]] = ["exiftool"]
    memory_category: ClassVar[str] = "high"

    def validate(self, path: str, decode_depth: str = "structure") -> ValidationResult:
        try:
            import rawpy
        except ImportError:
            return ValidationResult(
                status="error",
                error="rawpy not installed",
                suggested_action="pip install rawpy",
            )

        try:
            with rawpy.imread(path) as raw:
                if decode_depth == "full":
                    raw.postprocess(half_size=True, output_bps=8)

        except rawpy.LibRawFileUnsupportedError:
            # DNG fallback — try Pillow as TIFF before giving up
            if path.lower().endswith(".dng"):
                try:
                    from PIL import Image
                    with Image.open(path) as img:
                        img.load()
                    return ValidationResult(status="ok")
                except Exception as e:
                    return ValidationResult(
                        status="unsupported",
                        error=f"LibRaw unsupported, Pillow fallback failed: {e}",
                        suggested_action="Update rawpy or check DNG variant",
                    )
            return ValidationResult(
                status="unsupported",
                error="Camera model not supported by installed LibRaw version",
                suggested_action="Update rawpy/LibRaw for this camera model",
            )

        except rawpy.LibRawIOError as e:
            return ValidationResult(status="corrupt", error=str(e))

        except rawpy.LibRawDataError as e:
            return ValidationResult(status="corrupt", error=str(e))

        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))

        except OSError as e:
            return ValidationResult(status="unreadable", error=str(e))

        return ValidationResult(status="ok")
