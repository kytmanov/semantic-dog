"""Notification system — SMTP email and HTTP webhook."""

from __future__ import annotations

import ipaddress
import smtplib
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# Notification cap per status category
_CAP = 50
# Systemic failure threshold (fraction of scan path that is unreadable)
_DEFAULT_SYSTEMIC_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# ScanSummary
# ---------------------------------------------------------------------------

@dataclass
class ScanSummary:
    scan_id: str
    scope: str
    duration_s: float
    total_checked: int
    corrupt: list[dict] = field(default_factory=list)
    unreadable: list[dict] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.corrupt or self.unreadable)

    def corrupt_count(self) -> int:
        return len(self.corrupt)

    def unreadable_count(self) -> int:
        return len(self.unreadable)


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(host: str) -> bool:
    try:
        resolved = socket.getaddrinfo(host, None)[0][4][0]
        addr = ipaddress.ip_address(resolved)
        return any(addr in net for net in _PRIVATE_NETS)
    except (socket.gaierror, ValueError):
        return False


def validate_webhook_url(url: str, allow_private: bool = False) -> None:
    """Raise ValueError if webhook URL resolves to a private/internal address."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL must use http/https, got {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"Webhook URL {url!r} has no hostname")
    if not allow_private and _is_private_ip(host):
        raise ValueError(
            f"Webhook URL {url!r} resolves to a private IP. "
            "Set SDOG_WEBHOOK_ALLOW_PRIVATE=true to allow internal webhooks."
        )


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_message(summary: ScanSummary, systemic_threshold: float = _DEFAULT_SYSTEMIC_THRESHOLD) -> str:
    lines = [
        f"SemanticDog Scan Report",
        f"Scan ID:  {summary.scan_id}",
        f"Scope:    {summary.scope}",
        f"Duration: {summary.duration_s:.1f}s",
        f"Checked:  {summary.total_checked} files",
        "",
    ]

    # Systemic failure detection
    if summary.total_checked > 0:
        unread_ratio = len(summary.unreadable) / summary.total_checked
        if unread_ratio >= systemic_threshold:
            lines.append(
                f"⚠ Suspected mount/network failure on {summary.scope} — "
                f"{len(summary.unreadable)}/{summary.total_checked} files unreadable."
            )
            lines.append("")

    if summary.corrupt:
        lines.append(f"Corrupt files ({len(summary.corrupt)}):")
        for r in summary.corrupt[:_CAP]:
            err = f"  [{r.get('error','')}]" if r.get("error") else ""
            lines.append(f"  {r.get('path','')}{err}")
        if len(summary.corrupt) > _CAP:
            lines.append(f"  ... and {len(summary.corrupt) - _CAP} more")
        lines.append("")

    if summary.unreadable:
        lines.append(
            f"Unreadable files ({len(summary.unreadable)}):\n"
            "  (These may indicate a mount/network issue rather than file corruption.)"
        )
        for r in summary.unreadable[:_CAP]:
            lines.append(f"  {r.get('path','')}")
        if len(summary.unreadable) > _CAP:
            lines.append(f"  ... and {len(summary.unreadable) - _CAP} more")
        lines.append("")

    if not summary.corrupt and not summary.unreadable:
        lines.append("✓ All files OK.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SMTP notifier
# ---------------------------------------------------------------------------

class SmtpNotifier:
    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg

    def send(self, summary: ScanSummary) -> None:
        if not self.cfg.notify_email or not self.cfg.smtp_host:
            return

        body = _build_message(summary)
        subject = (
            f"[SemanticDog] {summary.corrupt_count()} corrupt, "
            f"{summary.unreadable_count()} unreadable — {summary.scope}"
        )
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"semanticdog@{self.cfg.smtp_host}"
        msg["To"] = self.cfg.notify_email

        tls = getattr(self.cfg, "smtp_tls", "starttls")
        port = getattr(self.cfg, "smtp_port", None)

        try:
            if tls == "ssl":
                p = port or 465
                with smtplib.SMTP_SSL(self.cfg.smtp_host, p) as s:
                    if self.cfg.smtp_user:
                        s.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    s.send_message(msg)
            elif tls == "none":
                p = port or 25
                with smtplib.SMTP(self.cfg.smtp_host, p) as s:
                    if self.cfg.smtp_user:
                        s.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    s.send_message(msg)
            else:  # starttls (default)
                p = port or 587
                with smtplib.SMTP(self.cfg.smtp_host, p) as s:
                    s.ehlo()
                    s.starttls()
                    if self.cfg.smtp_user:
                        s.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    s.send_message(msg)
        except smtplib.SMTPException as e:
            raise RuntimeError(f"SMTP send failed: {e}") from e


# ---------------------------------------------------------------------------
# Webhook notifier
# ---------------------------------------------------------------------------

class WebhookNotifier:
    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg

    def send(self, summary: ScanSummary) -> None:
        if not self.cfg.webhook_url:
            return

        allow_private = getattr(self.cfg, "webhook_allow_private", False)
        try:
            validate_webhook_url(self.cfg.webhook_url, allow_private=allow_private)
        except ValueError as e:
            raise RuntimeError(f"Webhook URL rejected: {e}") from e

        import json
        body = _build_message(summary)
        payload = json.dumps({
            "title": f"SemanticDog: {summary.corrupt_count()} corrupt",
            "message": body,
            "scan_id": summary.scan_id,
        }).encode()

        req = urllib.request.Request(
            self.cfg.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Webhook returned HTTP {resp.status}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Webhook send failed: {e}") from e


# ---------------------------------------------------------------------------
# Notifier — orchestrates all channels
# ---------------------------------------------------------------------------

class Notifier:
    """Send notifications via all configured channels."""

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg
        self._smtp = SmtpNotifier(cfg)
        self._webhook = WebhookNotifier(cfg)

    def notify(self, summary: ScanSummary) -> list[str]:
        """Send to all channels. Returns list of error messages (empty = all ok)."""
        errors: list[str] = []
        for sender, name in ((self._smtp, "SMTP"), (self._webhook, "Webhook")):
            try:
                sender.send(summary)
            except Exception as e:
                errors.append(f"{name}: {e}")
        return errors
