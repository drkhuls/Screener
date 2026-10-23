"""Schwab refresh-token expiry, from the login time schwab-py stores in the token file."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def read_login_time(path: Path) -> datetime | None:
    """``creation_timestamp`` is set at login and kept when the access token refreshes."""
    try:
        raw = json.loads(path.read_text())
        return datetime.fromtimestamp(float(raw["creation_timestamp"]), timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def token_status(
    login_at: datetime | None,
    now: datetime,
    lifetime_days: float = 7,
    warn_days: float = 2,
    urgent_days: float = 1,
) -> dict[str, Any]:
    """Level is ok, warning (within ``warn_days``), urgent (last day), expired, or missing."""
    if login_at is None:
        return {"level": "missing", "login_at": None, "expires_at": None, "seconds_left": None}
    expires_at = login_at + timedelta(days=lifetime_days)
    left = (expires_at - now).total_seconds()
    if left <= 0:
        level = "expired"
    elif left <= urgent_days * 86400:
        level = "urgent"
    elif left <= warn_days * 86400:
        level = "warning"
    else:
        level = "ok"
    return {
        "level": level,
        "login_at": login_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "seconds_left": max(0, int(left)),
    }


def describe(status: dict[str, Any]) -> str:
    if status["level"] == "missing":
        return "No Schwab token. Run: python backend.py login"
    if status["level"] == "expired":
        return f"Schwab token expired at {status['expires_at']}. Run: python backend.py login"
    hours = status["seconds_left"] / 3600
    left = f"{hours / 24:.1f} days" if hours >= 24 else f"{hours:.1f} hours"
    return f"Schwab token expires in {left} ({status['expires_at']}). Run: python backend.py login"
