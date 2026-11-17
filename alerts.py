"""Order alerts: log, optional SMTP email, and an SMS placeholder.

Recipients come from ORDER_ALERT_EMAIL and ORDER_ALERT_PHONE. Both default to
empty. This module never contains a destination address.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("screener.orders")


def dispatch_order_alert(kind: str, message: str) -> None:
    """Log every alert. Email when SMTP is configured; otherwise log that it was not sent."""
    log.info("Order alert [%s]: %s", kind, message)
    email = os.environ.get("ORDER_ALERT_EMAIL", "").strip()
    phone = os.environ.get("ORDER_ALERT_PHONE", "").strip()
    if email:
        _email(email, kind, message)
    if phone:
        # Placeholder only: nothing is handed to a carrier.
        log.info("SMS placeholder to %s: %s", phone, message)


def _email(to: str, kind: str, message: str) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        log.info("Order email not sent (SMTP unset). To %s: %s", to, message)
        return
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user or to
    use_tls = os.environ.get("SMTP_TLS", "1").strip().lower() not in {"0", "false", "no"}
    msg = EmailMessage()
    msg["Subject"] = f"Screener order: {kind}"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(message)
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:
        log.warning("Order email failed: %s", exc)
