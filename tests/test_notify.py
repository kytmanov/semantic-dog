"""Stage 11 tests — notification system."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from semanticdog.notify import (
    ScanSummary,
    Notifier,
    SmtpNotifier,
    WebhookNotifier,
    _build_message,
    validate_webhook_url,
    _CAP,
)
from semanticdog.config import Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summary(**kwargs) -> ScanSummary:
    defaults = dict(
        scan_id="abc-123",
        scope="/photos",
        duration_s=12.5,
        total_checked=100,
    )
    defaults.update(kwargs)
    return ScanSummary(**defaults)


def _corrupt_file(path: str, error: str = "") -> dict:
    return {"path": path, "status": "corrupt", "error": error}


def _unreadable_file(path: str) -> dict:
    return {"path": path, "status": "unreadable"}


# ---------------------------------------------------------------------------
# ScanSummary
# ---------------------------------------------------------------------------

class TestScanSummary:
    def test_has_issues_false_when_clean(self):
        s = _summary()
        assert s.has_issues is False

    def test_has_issues_true_with_corrupt(self):
        s = _summary(corrupt=[_corrupt_file("/a.jpg")])
        assert s.has_issues is True

    def test_corrupt_count(self):
        s = _summary(corrupt=[_corrupt_file("/a.jpg"), _corrupt_file("/b.jpg")])
        assert s.corrupt_count() == 2

    def test_unreadable_count(self):
        s = _summary(unreadable=[_unreadable_file("/x.jpg")])
        assert s.unreadable_count() == 1


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def test_all_ok_message(self):
        msg = _build_message(_summary())
        assert "All files OK" in msg

    def test_corrupt_files_listed(self):
        s = _summary(corrupt=[_corrupt_file("/photos/bad.jpg", "truncated")])
        msg = _build_message(s)
        assert "/photos/bad.jpg" in msg
        assert "truncated" in msg

    def test_unreadable_mount_hint(self):
        s = _summary(unreadable=[_unreadable_file("/nas/img.jpg")])
        msg = _build_message(s)
        assert "mount/network" in msg.lower() or "unreadable" in msg.lower()

    def test_cap_applied_to_corrupt(self):
        corrupt = [_corrupt_file(f"/f{i}.jpg") for i in range(_CAP + 10)]
        s = _summary(corrupt=corrupt)
        msg = _build_message(s)
        assert f"and {10} more" in msg

    def test_cap_applied_to_unreadable(self):
        unread = [_unreadable_file(f"/f{i}.jpg") for i in range(_CAP + 5)]
        s = _summary(unreadable=unread)
        msg = _build_message(s)
        assert f"and {5} more" in msg

    def test_systemic_failure_flagged(self):
        """> 50% unreadable → systemic failure warning."""
        unread = [_unreadable_file(f"/f{i}.jpg") for i in range(60)]
        s = _summary(total_checked=100, unreadable=unread)
        msg = _build_message(s, systemic_threshold=0.5)
        assert "Suspected mount" in msg or "network failure" in msg

    def test_systemic_failure_not_flagged_below_threshold(self):
        unread = [_unreadable_file(f"/f{i}.jpg") for i in range(10)]
        s = _summary(total_checked=100, unreadable=unread)
        msg = _build_message(s, systemic_threshold=0.5)
        assert "Suspected mount" not in msg

    def test_includes_scan_metadata(self):
        s = _summary(scan_id="test-scan-id", scope="/photos", duration_s=42.0)
        msg = _build_message(s)
        assert "test-scan-id" in msg
        assert "/photos" in msg
        assert "42.0" in msg


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

class TestValidateWebhookUrl:
    def test_public_url_ok(self):
        with patch("semanticdog.notify.socket.getaddrinfo") as m:
            m.return_value = [(None, None, None, None, ("8.8.8.8", 0))]
            validate_webhook_url("https://gotify.example.com/message")  # must not raise

    def test_private_ip_rejected(self):
        with patch("semanticdog.notify.socket.getaddrinfo") as m:
            m.return_value = [(None, None, None, None, ("192.168.1.1", 0))]
            with pytest.raises(ValueError, match="private IP"):
                validate_webhook_url("http://192.168.1.1/webhook")

    def test_loopback_rejected(self):
        with patch("semanticdog.notify.socket.getaddrinfo") as m:
            m.return_value = [(None, None, None, None, ("127.0.0.1", 0))]
            with pytest.raises(ValueError, match="private IP"):
                validate_webhook_url("http://localhost/webhook")

    def test_allow_private_bypasses_check(self):
        with patch("semanticdog.notify.socket.getaddrinfo") as m:
            m.return_value = [(None, None, None, None, ("192.168.1.1", 0))]
            validate_webhook_url("http://192.168.1.1/webhook", allow_private=True)  # no raise

    def test_non_http_scheme_rejected(self):
        with pytest.raises(ValueError, match="http/https"):
            validate_webhook_url("ftp://example.com/hook")


# ---------------------------------------------------------------------------
# SmtpNotifier
# ---------------------------------------------------------------------------

class TestSmtpNotifier:
    def _cfg(self, **kwargs) -> Config:
        defaults = dict(
            paths=["/x"],
            notify_email="admin@example.com",
            smtp_host="smtp.example.com",
            smtp_user="user",
            smtp_pass="pass",
        )
        defaults.update(kwargs)
        return Config(**defaults)

    def test_sends_starttls_by_default(self):
        cfg = self._cfg()
        notifier = SmtpNotifier(cfg)
        s = _summary(corrupt=[_corrupt_file("/bad.jpg")])

        mock_smtp = MagicMock()
        with patch("semanticdog.notify.smtplib.SMTP", return_value=mock_smtp):
            mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp.__exit__ = MagicMock(return_value=False)
            notifier.send(s)
        mock_smtp.starttls.assert_called_once()
        mock_smtp.send_message.assert_called_once()

    def test_no_op_when_no_email(self):
        cfg = Config(paths=["/x"], smtp_host="smtp.ex.com")
        notifier = SmtpNotifier(cfg)
        with patch("semanticdog.notify.smtplib.SMTP") as m:
            notifier.send(_summary())
        m.assert_not_called()

    def test_no_op_when_no_host(self):
        cfg = Config(paths=["/x"], notify_email="a@b.com")
        notifier = SmtpNotifier(cfg)
        with patch("semanticdog.notify.smtplib.SMTP") as m:
            notifier.send(_summary())
        m.assert_not_called()

    def test_smtp_exception_raises_runtime(self):
        cfg = self._cfg()
        notifier = SmtpNotifier(cfg)
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)
        mock_smtp.send_message.side_effect = smtplib.SMTPException("connection refused")
        with patch("semanticdog.notify.smtplib.SMTP", return_value=mock_smtp):
            with pytest.raises(RuntimeError, match="SMTP send failed"):
                notifier.send(_summary(corrupt=[_corrupt_file("/x.jpg")]))


# ---------------------------------------------------------------------------
# WebhookNotifier
# ---------------------------------------------------------------------------

class TestWebhookNotifier:
    def _cfg(self, url: str = "https://gotify.example.com/message") -> Config:
        return Config(paths=["/x"], webhook_url=url)

    def test_sends_post_request(self):
        cfg = self._cfg()
        notifier = WebhookNotifier(cfg)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("semanticdog.notify.urllib.request.urlopen", return_value=mock_resp):
            notifier.send(_summary())

    def test_no_op_when_no_url(self):
        cfg = Config(paths=["/x"])
        notifier = WebhookNotifier(cfg)
        with patch("semanticdog.notify.urllib.request.urlopen") as m:
            notifier.send(_summary())
        m.assert_not_called()

    def test_url_error_raises_runtime(self):
        import urllib.error
        cfg = self._cfg()
        notifier = WebhookNotifier(cfg)
        with patch(
            "semanticdog.notify.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="Webhook send failed"):
                notifier.send(_summary())


# ---------------------------------------------------------------------------
# Notifier (orchestrator)
# ---------------------------------------------------------------------------

class TestNotifier:
    def test_notify_collects_errors(self):
        cfg = Config(
            paths=["/x"],
            notify_email="a@b.com",
            smtp_host="smtp.ex.com",
            webhook_url="https://hook.example.com",
        )
        notifier = Notifier(cfg)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)
        with patch("semanticdog.notify.smtplib.SMTP", return_value=mock_smtp), \
             patch("semanticdog.notify.urllib.request.urlopen", return_value=mock_resp):
            errors = notifier.notify(_summary())
        assert errors == []

    def test_notify_returns_error_on_smtp_failure(self):
        cfg = Config(paths=["/x"], notify_email="a@b.com", smtp_host="smtp.ex.com")
        notifier = Notifier(cfg)
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)
        mock_smtp.send_message.side_effect = smtplib.SMTPException("refused")
        with patch("semanticdog.notify.smtplib.SMTP", return_value=mock_smtp):
            errors = notifier.notify(_summary(corrupt=[_corrupt_file("/x.jpg")]))
        assert len(errors) == 1
        assert "SMTP" in errors[0]

    def test_notify_no_channels_no_error(self):
        cfg = Config(paths=["/x"])
        notifier = Notifier(cfg)
        errors = notifier.notify(_summary())
        assert errors == []
