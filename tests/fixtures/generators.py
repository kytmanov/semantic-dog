"""Programmatic corrupt file generators — no binary blobs committed."""

from __future__ import annotations

import struct
import zipfile
import io
import xml.etree.ElementTree as ET
from pathlib import Path


# ---------------------------------------------------------------------------
# JPEG fixtures
# ---------------------------------------------------------------------------

# Minimal valid JPEG (1x1 white pixel)
_MINIMAL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000"
    "ffdb004300080606070605080707070909080a0c"
    "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20"
    "242e2720222c231c1c2837292c30313434341f27"
    "39443238323334320000"
    "ffc0000b080001000101011100"
    "ffc4001f0000010501010101010100000000000000"
    "000102030405060708090a0b"
    "ffc40000"  # truncated — replaced by proper DHT below
    "ffda00030101003f00fad40000ffda0"
    "00301010000003f00fad400000000ffda"
    "000301010100013f00fad400000000ffd9"
)

_MINIMAL_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x00"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfa\xd4\x00\x00\xff\xd9"
)


def make_minimal_jpeg(path: str | Path) -> Path:
    """Write a minimal but structurally valid JPEG."""
    p = Path(path)
    p.write_bytes(_MINIMAL_JPEG_BYTES)
    return p


def make_truncated_jpeg(path: str | Path, truncate_at: float = 0.5) -> Path:
    """Write a JPEG truncated to `truncate_at` fraction of its length."""
    p = Path(path)
    data = _MINIMAL_JPEG_BYTES
    p.write_bytes(data[: int(len(data) * truncate_at)])
    return p


def make_bad_sof_marker_jpeg(path: str | Path) -> Path:
    """Write a JPEG with a corrupted SOF0 marker."""
    p = Path(path)
    data = bytearray(_MINIMAL_JPEG_BYTES)
    # Flip the SOF0 marker bytes (0xFF 0xC0 → 0xFF 0x00)
    idx = data.find(b"\xff\xc0")
    if idx != -1:
        data[idx + 1] = 0x00
    p.write_bytes(bytes(data))
    return p


# ---------------------------------------------------------------------------
# PNG fixtures
# ---------------------------------------------------------------------------

# Minimal 1x1 red pixel PNG (hand-crafted, no Pillow dependency)
_MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"            # signature
    b"\x00\x00\x00\rIHDR"           # IHDR length=13
    b"\x00\x00\x00\x01"             # width=1
    b"\x00\x00\x00\x01"             # height=1
    b"\x08\x02"                     # bit depth=8, color type=2 (RGB)
    b"\x00\x00\x00"                 # compression, filter, interlace
    b"\x90wS\xde"                   # IHDR CRC
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x02\x00\x01"  # IDAT
    b"\xe2!\xbc3"                   # IDAT CRC
    b"\x00\x00\x00\x00IEND\xaeB`\x82"  # IEND
)


def make_minimal_png(path: str | Path) -> Path:
    """Write a minimal valid 1x1 PNG using Pillow."""
    import io as _io
    from PIL import Image as _Image
    p = Path(path)
    buf = _io.BytesIO()
    img = _Image.new("RGB", (1, 1), color=(255, 0, 0))
    img.save(buf, format="PNG")
    p.write_bytes(buf.getvalue())
    return p


def make_truncated_png(path: str | Path) -> Path:
    """Write a PNG truncated mid-stream."""
    p = Path(path)
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    img = _Image.new("RGB", (4, 4), color=(0, 255, 0))
    img.save(buf, format="PNG")
    data = buf.getvalue()
    p.write_bytes(data[: len(data) // 2])
    return p


def make_minimal_tiff(path: str | Path) -> Path:
    """Write a minimal valid 1x1 TIFF using Pillow."""
    import io as _io
    from PIL import Image as _Image
    p = Path(path)
    buf = _io.BytesIO()
    img = _Image.new("RGB", (1, 1), color=(0, 0, 255))
    img.save(buf, format="TIFF")
    p.write_bytes(buf.getvalue())
    return p


def make_truncated_tiff(path: str | Path) -> Path:
    """Write a TIFF truncated mid-stream."""
    import io as _io
    from PIL import Image as _Image
    p = Path(path)
    buf = _io.BytesIO()
    img = _Image.new("RGB", (4, 4), color=(128, 128, 128))
    img.save(buf, format="TIFF")
    data = buf.getvalue()
    p.write_bytes(data[: len(data) // 2])
    return p


def make_minimal_webp(path: str | Path) -> Path:
    """Write a minimal valid WebP using Pillow."""
    import io as _io
    from PIL import Image as _Image
    p = Path(path)
    buf = _io.BytesIO()
    img = _Image.new("RGB", (1, 1), color=(0, 128, 255))
    img.save(buf, format="WEBP")
    p.write_bytes(buf.getvalue())
    return p


def make_not_an_image(path: str | Path) -> Path:
    """Write a file with valid extension but not image content."""
    p = Path(path)
    p.write_bytes(b"this is definitely not an image file content here")
    return p


# ---------------------------------------------------------------------------
# PDF fixtures
# ---------------------------------------------------------------------------

_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 3 3]>>endobj
xref
0 4
0000000000 65535 f\r
0000000009 00000 n\r
0000000058 00000 n\r
0000000115 00000 n\r
trailer<</Size 4/Root 1 0 R>>
startxref
190
%%EOF
"""


def make_minimal_pdf(path: str | Path) -> Path:
    p = Path(path)
    p.write_bytes(_MINIMAL_PDF)
    return p


def make_bad_xref_pdf(path: str | Path) -> Path:
    """Write a PDF with a corrupted xref offset."""
    p = Path(path)
    data = _MINIMAL_PDF.replace(b"startxref\n190", b"startxref\n999")
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# DOCX fixtures
# ---------------------------------------------------------------------------

def make_minimal_docx(path: str | Path) -> Path:
    """Write a minimal but valid DOCX (ZIP + required XML parts)."""
    p = Path(path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '</Types>'
        ))
        zf.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>'
        ))
        zf.writestr("word/document.xml", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body>'
            '</w:document>'
        ))
    p.write_bytes(buf.getvalue())
    return p


def make_valid_zip_bad_xml_docx(path: str | Path) -> Path:
    """Valid ZIP structure but malformed word/document.xml."""
    p = Path(path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        ))
        zf.writestr("_rels/.rels", (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ))
        zf.writestr("word/document.xml", "<<NOT VALID XML>>")
    p.write_bytes(buf.getvalue())
    return p


def make_truncated_zip_docx(path: str | Path, truncate_at: float = 0.7) -> Path:
    """Write a DOCX with the ZIP stream truncated."""
    p = Path(path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="..."/>')
        zf.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="..."/>')
        zf.writestr("word/document.xml", "x" * 2000)
    raw = buf.getvalue()
    p.write_bytes(raw[: int(len(raw) * truncate_at)])
    return p


# ---------------------------------------------------------------------------
# RAW fixtures
# ---------------------------------------------------------------------------

def make_truncated_raw(src_path: str | Path, dest_path: str | Path, truncate_at: float = 0.3) -> Path:
    """Truncate an existing RAW file to simulate corruption."""
    src = Path(src_path)
    dest = Path(dest_path)
    data = src.read_bytes()
    dest.write_bytes(data[: int(len(data) * truncate_at)])
    return dest


# ---------------------------------------------------------------------------
# Zero-byte fixture
# ---------------------------------------------------------------------------

def make_zero_byte(path: str | Path) -> Path:
    p = Path(path)
    p.write_bytes(b"")
    return p
