"""Document validators: PDF, Office OOXML, legacy OLE."""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from typing import ClassVar

from .base import BaseValidator, ValidationResult
from . import register


@register
class PdfValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".pdf"})
    memory_category: ClassVar[str] = "low"

    def validate(self, path: str) -> ValidationResult:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError, PdfStreamError
        except ImportError:
            return ValidationResult(
                status="error",
                error="pypdf not installed",
                suggested_action="pip install pypdf",
            )

        try:
            reader = PdfReader(path, strict=False)
            # Force page-tree traversal — catches corrupt xref / missing pages
            _ = len(reader.pages)
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

        return ValidationResult(status="ok")


@register
class OoxmlValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".docx", ".xlsx", ".pptx"})
    memory_category: ClassVar[str] = "low"

    # Required parts that must be present and parseable XML
    _REQUIRED_PARTS = ("[Content_Types].xml", "_rels/.rels")

    def validate(self, path: str) -> ValidationResult:
        # 1. Open ZIP and check CRC integrity
        try:
            zf = zipfile.ZipFile(path, "r")
        except FileNotFoundError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except OSError as e:
            return ValidationResult(status="unreadable", error=str(e))
        except zipfile.BadZipFile as e:
            return ValidationResult(
                status="corrupt",
                error=f"Bad ZIP container: {e}",
                suggested_action="Restore from backup or re-download",
            )

        try:
            crc_error = zf.testzip()
            if crc_error:
                return ValidationResult(
                    status="corrupt",
                    error=f"CRC failure in ZIP entry: {crc_error}",
                    suggested_action="Restore from backup or re-download",
                )

            # 2. Parse required XML parts
            for part in self._REQUIRED_PARTS:
                try:
                    ET.fromstring(zf.read(part))
                except KeyError:
                    return ValidationResult(
                        status="corrupt",
                        error=f"Missing required part: {part}",
                        suggested_action="Restore from backup or re-download",
                    )
                except ET.ParseError as e:
                    return ValidationResult(
                        status="corrupt",
                        error=f"Bad XML in {part}: {e}",
                        suggested_action="Restore from backup or re-download",
                    )
        finally:
            zf.close()

        return ValidationResult(status="ok")


@register
class OleValidator(BaseValidator):
    extensions: ClassVar[frozenset[str]] = frozenset({".doc", ".xls", ".ppt"})
    memory_category: ClassVar[str] = "low"

    # Minimum expected streams per format
    _EXPECTED_STREAMS: dict[str, list[str]] = {
        ".doc": ["WordDocument"],
        ".xls": ["Workbook"],
        ".ppt": ["PowerPoint Document"],
    }

    def validate(self, path: str) -> ValidationResult:
        try:
            import olefile
        except ImportError:
            return ValidationResult(
                status="error",
                error="olefile not installed",
                suggested_action="pip install olefile",
            )

        try:
            if not olefile.isOleFile(path):
                return ValidationResult(
                    status="corrupt",
                    error="Not a valid OLE2 container",
                    suggested_action="Restore from backup or re-download",
                )

            with olefile.OleFileIO(path) as ole:
                streams = {"/".join(entry) for entry in ole.listdir()}
                ext = path.lower().rsplit(".", 1)[-1]
                ext_key = f".{ext}"
                for stream in self._EXPECTED_STREAMS.get(ext_key, []):
                    if stream not in streams:
                        return ValidationResult(
                            status="corrupt",
                            error=f"Missing expected OLE stream: {stream!r}",
                            suggested_action="Restore from backup or re-download",
                        )

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

        return ValidationResult(status="ok")
