"""Access-token renewal. Fixture values are fake; nothing here is a real Schwab token."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import token_refresh  # noqa: E402
from backend import Screener, load_config  # noqa: E402

NOW = 1_700_000_000


class _Session:
    def __init__(self, token):
        self.token = token


class _Meta:
    def __init__(self, token):
        self.token = token


class _Client:
    def __init__(self, token):
        self.session = _Session(token)
        self.token_metadata = _Meta(token)


class _Source:
    name = "schwab"

    def __init__(self):
        self.client = _Client({"access_token": "memory-access", "expires_at": NOW + 301})
        self.history_calls = 0

    def get_daily_history(self, symbol, days):
        self.history_calls += 1
        return []

    def get_quotes(self, symbols):
        return {}


def _write(path: Path, expires_at: int, refresh_token: str | None, access_token: str = "access-old") -> None:
    token = {"access_token": access_token, "expires_at": expires_at, "token_type": "Bearer"}
    if refresh_token is not None:
        token["refresh_token"] = refresh_token
    path.write_text(json.dumps({"creation_timestamp": NOW - 86_400, "token": token}))


def _screener(monkeypatch, path: Path, source: _Source, post) -> Screener:
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(path))
    monkeypatch.setenv("SCHWAB_APP_KEY", "app-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "app-secret")
    monkeypatch.setattr(token_refresh.time, "time", lambda: NOW)
    monkeypatch.setattr(token_refresh, "request_refresh", post)
    monkeypatch.setattr("backend.make_source", lambda *args: source)
    return Screener(load_config(), "schwab")


def test_refresh_token_renews_the_access_token_before_it_expires(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    # 301 seconds of life is outside schwab-py's 5-minute early window.
    _write(path, NOW + 301, "refresh-1")
    calls = []

    def post(app_key, app_secret, refresh_token):
        calls.append((app_key, app_secret, refresh_token))
        # Schwab sometimes omits a new refresh token. The old one must stay on disk.
        return {"access_token": "access-new", "expires_in": 1800, "token_type": "Bearer"}

    source = _Source()
    screener = _screener(monkeypatch, path, source, post)
    assert calls == []
    assert source.client.session.token["refresh_token"] == "refresh-1"
    assert source.client.session.token["access_token"] == "memory-access"

    # Still valid, but inside the 5-minute window. This is the chart-failure point.
    _write(path, NOW + 120, "refresh-1")
    payload = screener.candles("QQQ")
    assert payload["symbol"] == "QQQ"
    assert calls == [("app-key", "app-secret", "refresh-1")]
    saved = json.loads(path.read_text())
    assert saved["creation_timestamp"] == NOW - 86_400
    assert saved["token"]["refresh_token"] == "refresh-1"
    assert saved["token"]["access_token"] == "access-new"
    assert saved["token"]["expires_at"] == NOW + 1800
    assert source.client.session.token["refresh_token"] == "refresh-1"
    assert source.client.session.token["access_token"] == "access-new"
    assert source.client.token_metadata.token["refresh_token"] == "refresh-1"
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)

    screener.renew_access_token()
    assert calls == [("app-key", "app-secret", "refresh-1")]


def test_missing_refresh_token_surfaces_a_clear_error(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    _write(path, NOW - 5, None)
    source = _Source()

    def post(*_args):
        raise AssertionError("refresh was attempted")

    screener = _screener(monkeypatch, path, source, post)
    with pytest.raises(token_refresh.TokenRefreshError) as caught:
        screener.candles("QQQ")
    message = str(caught.value)
    detail = f"QQQ: {token_refresh.public_error(caught.value)}"
    assert "no refresh token" in message
    assert "python backend.py login" in message
    assert "token_invalid" not in detail
    assert "access-old" not in message
    assert source.history_calls == 0
    assert "refresh_token" not in json.loads(path.read_text())["token"]

    class InvalidTokenError(Exception):
        pass

    rewritten = token_refresh.public_error(InvalidTokenError("token_invalid: "))
    assert rewritten.startswith("Schwab access token expired")
    assert "token_invalid" not in rewritten
