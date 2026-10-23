"""Email for approvals and escalations (REQUIREMENTS C9), through the SMTP settings Break Point already reads from
.env (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM). Silent when SMTP is not configured; the
Control Room badge and browser notification still fire."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import break_point_settings


def send(settings, subject: str, body: str) -> str:
    """Returns what happened, for the event log. Never raises."""
    try:
        bp = break_point_settings()
    except Exception as exc:
        return f"email skipped: settings unavailable ({exc})"
    to = settings.notify_email or getattr(bp, "admin_email", "")
    if not bp.smtp_host or not to:
        return "email skipped: SMTP or recipient not configured"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, bp.smtp_from or bp.smtp_user, to
    msg.set_content(body)
    try:
        with smtplib.SMTP(bp.smtp_host, bp.smtp_port, timeout=15) as s:
            if bp.smtp_starttls:
                s.starttls(context=ssl.create_default_context())
            if bp.smtp_user:
                s.login(bp.smtp_user, bp.smtp_password)
            s.send_message(msg)
        return f"email sent to {to}"
    except Exception as exc:
        return f"email failed: {exc.__class__.__name__}: {exc}"
