"""Renew the Schwab access token before it expires, and keep the refresh token on disk.

schwab-py treats an access token as expired 5 minutes early. authlib then refuses the
request with InvalidTokenError (``token_invalid``) when the in-memory token has no
refresh token, so no refresh is attempted. This module exchanges the refresh token
itself and writes the same token file schwab-py uses, without dropping ``refresh_token``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Same early window as schwab-py's OAuth client (leeway=300).
LEEWAY_SECONDS = 300
TOKEN_ENDPOINT = "https://api.schwabapi.com/v1/oauth/token"

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class TokenRefreshError(Exception):
    """A refresh that cannot be done. The message is safe to show; it has no token material."""


def public_error(exc: BaseException) -> str:
    """Text for the chart and the header. Never the raw ``token_invalid`` code."""
    if isinstance(exc, TokenRefreshError):
        return str(exc)
    text = str(exc).strip()
    if type(exc).__name__ == "InvalidTokenError" or text.startswith("token_invalid"):
        return "Schwab access token expired and could not be refreshed. Run: python backend.py login"
    return text


def _lock(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _expires_at(token: dict[str, Any]) -> int | None:
    raw = token.get("expires_at")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _due(expires_at: int | None, now: float, leeway: int) -> bool:
    """True once the access token is inside the early-expiry window, or already past it."""
    return expires_at is not None and expires_at - leeway <= now


def _read(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise TokenRefreshError(
            "Schwab token file could not be read. Run: python backend.py login"
        ) from exc
    if not isinstance(loaded, dict) or "creation_timestamp" not in loaded or not isinstance(loaded.get("token"), dict):
        raise TokenRefreshError(
            "Schwab token file is missing its login time. Run: python backend.py login"
        )
    return loaded


def _write(path: Path, wrapped: dict[str, Any]) -> None:
    """Replace the token file in place. Mode 0600, so the new file is not world-readable."""
    payload = json.dumps(wrapped).encode()
    temporary = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise TokenRefreshError(
            "Could not save the refreshed Schwab token. Run: python backend.py login"
        ) from exc


def _merge(current: dict[str, Any], issued: dict[str, Any], now: float) -> dict[str, Any]:
    """Take the new access token. Keep the previous refresh token when the response omits it."""
    merged = dict(current)
    previous_refresh = merged.get("refresh_token")
    for key, value in issued.items():
        if value is None or value == "":
            continue
        merged[key] = value
    if not merged.get("refresh_token") and previous_refresh:
        merged["refresh_token"] = previous_refresh
    expires_in = issued.get("expires_in")
    if expires_in is not None:
        try:
            merged["expires_at"] = int(now) + int(expires_in)
        except (TypeError, ValueError):
            pass
    if not merged.get("access_token") or _expires_at(merged) is None:
        raise TokenRefreshError(
            "Schwab access-token refresh returned an unusable token. Run: python backend.py login"
        )
    if not merged.get("refresh_token"):
        raise TokenRefreshError(
            "Schwab access-token refresh dropped the refresh token. Run: python backend.py login"
        )
    return merged


def request_refresh(app_key: str, app_secret: str, refresh_token: str) -> dict[str, Any]:
    """POST the refresh grant. The body is not logged."""
    import httpx

    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(app_key, app_secret),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise TokenRefreshError(
            "Schwab access-token refresh could not reach the token endpoint. Run: python backend.py login"
        ) from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        code = body.get("error") if isinstance(body.get("error"), str) else ""
        detail = f" ({code})" if code else ""
        raise TokenRefreshError(
            f"Schwab rejected the access-token refresh{detail}. Run: python backend.py login"
        )
    return body


def renew(
    path: Path,
    app_key: str,
    app_secret: str,
    *,
    now: float | None = None,
    leeway: int = LEEWAY_SECONDS,
    post: Callable[[str, str, str], dict[str, Any]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Refresh when the access token is inside the early window.

    Returns ``(refreshed, token)``. ``token`` is the inner token dict the caller
    installs on the live client. Callers must not log it.
    """
    if now is None:
        now = time.time()
    poster = post or request_refresh
    with _lock(path):
        wrapped = _read(path)
        token = wrapped["token"]
        if not _due(_expires_at(token), now, leeway):
            return False, token
        refresh = token.get("refresh_token")
        if not isinstance(refresh, str) or not refresh.strip():
            raise TokenRefreshError(
                "Schwab access token expired and the token file has no refresh token. Run: python backend.py login"
            )
        merged = _merge(token, poster(app_key, app_secret, refresh), now)
        wrapped["token"] = merged
        _write(path, wrapped)
        return True, merged


def sync_client(client: Any, token: dict[str, Any], *, replaced: bool) -> None:
    """Point the live Schwab client at ``token``.

    A full replace follows a refresh. Otherwise only fill in a missing refresh token,
    so a newer in-memory access token is left in place.
    """
    if replaced:
        _install(client, token)
        return
    refresh = token.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return
    current = getattr(getattr(client, "session", None), "token", None)
    if isinstance(current, dict) and not current.get("refresh_token"):
        current["refresh_token"] = refresh
    meta_token = getattr(getattr(client, "token_metadata", None), "token", None)
    if isinstance(meta_token, dict) and meta_token is not current and not meta_token.get("refresh_token"):
        meta_token["refresh_token"] = refresh


def _install(client: Any, token: dict[str, Any]) -> None:
    fresh = dict(token)
    session = getattr(client, "session", None)
    if session is None or not hasattr(session, "token"):
        return
    session.token = fresh
    installed = session.token
    meta = getattr(client, "token_metadata", None)
    if meta is not None:
        meta.token = installed
