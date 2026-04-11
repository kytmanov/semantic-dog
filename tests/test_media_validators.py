"""Stage 8 tests — media validators (video/audio)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from semanticdog.validators.media import VideoValidator, AudioValidator
from tests.fixtures.generators import make_zero_byte, make_not_an_image


def _ok(r): assert r.status == "ok", f"Expected ok, got {r.status!r}: {r.error}"
def _corrupt(r): assert r.status == "corrupt", f"Expected corrupt, got {r.status!r}: {r.error}"
def _unreadable(r): assert r.status == "unreadable", f"Expected unreadable, got {r.status!r}"
def _unsupported(r): assert r.status == "unsupported", f"Expected unsupported, got {r.status!r}"


# ---------------------------------------------------------------------------
# VideoValidator — registration / metadata
# ---------------------------------------------------------------------------

class TestVideoValidatorMeta:
    def test_video_extensions_registered(self):
        from semanticdog.validators import get_validator
        for ext in (".mp4", ".mov", ".mkv", ".mts", ".m4v"):
            assert get_validator(ext) is VideoValidator

    def test_requires_ffprobe(self):
        assert "ffprobe" in VideoValidator.requires_cli

    def test_memory_category_low(self):
        assert VideoValidator.memory_category == "low"


# ---------------------------------------------------------------------------
# VideoValidator — happy path (mock subprocess)
# ---------------------------------------------------------------------------

class TestVideoValidatorHappyPath:
    def test_ffprobe_success_returns_ok(self, tmp_path):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 10)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "duration=10.0\n"
        mock_result.stderr = ""
        with patch("semanticdog.validators.media.subprocess.run", return_value=mock_result):
            r = VideoValidator().validate(str(p))
        _ok(r)

    def test_ffprobe_called_with_shell_false(self, tmp_path):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 10)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("semanticdog.validators.media.subprocess.run", return_value=mock_result) as m:
            VideoValidator().validate(str(p))
        call_args = m.call_args
        # First positional arg is the command list
        cmd = call_args[0][0]
        assert isinstance(cmd, list), "shell=False: must pass a list"
        assert cmd[0] == "ffprobe"
        # shell=True would have been caught if passed as kwarg
        assert call_args[1].get("shell", False) is False


# ---------------------------------------------------------------------------
# VideoValidator — error paths
# ---------------------------------------------------------------------------

class TestVideoValidatorErrors:
    def test_ffprobe_nonzero_returns_corrupt(self, tmp_path):
        p = tmp_path / "bad.mp4"
        p.write_bytes(b"\x00" * 10)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "moov atom not found"
        with patch("semanticdog.validators.media.subprocess.run", return_value=mock_result):
            r = VideoValidator().validate(str(p))
        _corrupt(r)
        assert "moov atom not found" in (r.error or "")

    def test_ffprobe_not_found_returns_error(self, tmp_path):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 10)
        with patch("semanticdog.validators.media.subprocess.run", side_effect=FileNotFoundError):
            r = VideoValidator().validate(str(p))
        assert r.status == "error"
        assert "ffprobe" in (r.error or "")

    def test_ffprobe_timeout_returns_error(self, tmp_path):
        p = tmp_path / "huge.mp4"
        p.write_bytes(b"\x00" * 10)
        with patch(
            "semanticdog.validators.media.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=120),
        ):
            r = VideoValidator().validate(str(p))
        assert r.status == "error"
        assert "timed out" in (r.error or "")

    def test_os_error_returns_unreadable(self, tmp_path):
        p = tmp_path / "perm.mp4"
        p.write_bytes(b"\x00" * 10)
        with patch(
            "semanticdog.validators.media.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
            r = VideoValidator().validate(str(p))
        _unreadable(r)


# ---------------------------------------------------------------------------
# AudioValidator — registration / metadata
# ---------------------------------------------------------------------------

class TestAudioValidatorMeta:
    def test_audio_extensions_registered(self):
        from semanticdog.validators import get_validator
        for ext in (".mp3", ".flac", ".wav", ".aac"):
            assert get_validator(ext) is AudioValidator

    def test_no_required_cli(self):
        assert AudioValidator.requires_cli == []

    def test_memory_category_low(self):
        assert AudioValidator.memory_category == "low"


# ---------------------------------------------------------------------------
# AudioValidator — happy path
# ---------------------------------------------------------------------------

class TestAudioValidatorHappyPath:
    def test_valid_wav_ok(self, tmp_path):
        """Generate a minimal WAV (mutagen can parse it)."""
        import struct
        p = tmp_path / "tone.wav"
        # 44-byte WAV header with 0 samples
        data_size = 0
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE",
            b"fmt ", 16, 1, 1, 44100, 88200, 2, 16,
            b"data", data_size,
        )
        p.write_bytes(header)
        r = AudioValidator().validate(str(p))
        # mutagen may return None for zero-sample WAV — that's unsupported, not corrupt
        assert r.status in ("ok", "unsupported")

    def test_mutagen_ok_result(self, tmp_path):
        p = tmp_path / "audio.mp3"
        p.write_bytes(b"\x00" * 10)
        mock_mutagen = MagicMock()
        mock_file = MagicMock()
        mock_mutagen.File.return_value = mock_file
        with patch.dict("sys.modules", {"mutagen": mock_mutagen}):
            r = AudioValidator().validate(str(p))
        _ok(r)


# ---------------------------------------------------------------------------
# AudioValidator — error paths
# ---------------------------------------------------------------------------

class TestAudioValidatorErrors:
    def test_mutagen_none_returns_unsupported(self, tmp_path):
        """mutagen.File returns None → unsupported (not corruption)."""
        p = make_not_an_image(tmp_path / "garbage.mp3")
        mock_mutagen = MagicMock()
        mock_mutagen.File.return_value = None
        with patch.dict("sys.modules", {"mutagen": mock_mutagen}):
            r = AudioValidator().validate(str(p))
        _unsupported(r)

    def test_mutagen_exception_returns_corrupt(self, tmp_path):
        p = tmp_path / "corrupt.flac"
        p.write_bytes(b"\x00" * 10)
        mock_mutagen = MagicMock()
        mock_mutagen.File.side_effect = Exception("bad frame")
        with patch.dict("sys.modules", {"mutagen": mock_mutagen}):
            r = AudioValidator().validate(str(p))
        _corrupt(r)

    def test_file_not_found_unreadable(self, tmp_path):
        mock_mutagen = MagicMock()
        mock_mutagen.File.side_effect = FileNotFoundError("no such file")
        with patch.dict("sys.modules", {"mutagen": mock_mutagen}):
            r = AudioValidator().validate(str(tmp_path / "ghost.mp3"))
        _unreadable(r)

    def test_os_error_unreadable(self, tmp_path):
        p = tmp_path / "perm.mp3"
        p.write_bytes(b"\x00")
        mock_mutagen = MagicMock()
        mock_mutagen.File.side_effect = OSError("permission denied")
        with patch.dict("sys.modules", {"mutagen": mock_mutagen}):
            r = AudioValidator().validate(str(p))
        _unreadable(r)

    def test_mutagen_missing_returns_error(self, tmp_path, monkeypatch):
        p = tmp_path / "audio.mp3"
        p.write_bytes(b"\x00")
        monkeypatch.setitem(sys.modules, "mutagen", None)  # type: ignore[arg-type]
        r = AudioValidator().validate(str(p))
        assert r.status == "error"
        assert "mutagen" in (r.error or "")

    def test_never_raises(self, tmp_path):
        p = make_not_an_image(tmp_path / "garbage.mp3")
        try:
            r = AudioValidator().validate(str(p))
            assert r.status in ("ok", "corrupt", "unsupported", "unreadable", "error")
        except Exception as exc:
            pytest.fail(f"AudioValidator raised {type(exc).__name__}: {exc}")
