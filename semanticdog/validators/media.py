"""Media validators: video (ffprobe) and audio (mutagen)."""

from __future__ import annotations

import subprocess
from typing import ClassVar

from .base import BaseValidator, ValidationResult
from . import register


@register
class VideoValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".mp4", ".mov", ".mts", ".m4v", ".mkv"})
    requires_cli: ClassVar[list[str]] = ["ffprobe"]
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str, timeout: int = 120) -> ValidationResult:
        try:
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1",
                    "--", path,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return ValidationResult(
                status="error",
                error="ffprobe not found",
                suggested_action="Install ffmpeg (provides ffprobe)",
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(
                status="error",
                error=f"ffprobe timed out after {timeout}s",
                suggested_action="File may be severely corrupt or very large",
            )
        except OSError as e:
            return ValidationResult(status="unreadable", error=str(e))

        if r.returncode != 0:
            err = (r.stdout + r.stderr).strip()
            return ValidationResult(
                status="corrupt",
                error=err or "ffprobe reported error",
                suggested_action="Restore from backup or re-download",
            )

        return ValidationResult(status="ok")


@register
class AudioValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".mp3", ".flac", ".wav", ".aac"})
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        try:
            import mutagen
        except ImportError:
            return ValidationResult(
                status="error",
                error="mutagen not installed",
                suggested_action="pip install mutagen",
            )

        try:
            f = mutagen.File(path, easy=True)
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except OSError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except Exception as e:
            return ValidationResult(
                status="corrupt",
                error=str(e),
                suggested_action="Restore from backup or re-download",
            )

        if f is None:
            # mutagen returns None for unrecognised formats — not corruption
            return ValidationResult(
                status="unsupported",
                error="unrecognized audio format — possibly misnamed or empty file",
            )

        return ValidationResult(status="ok")
