"""Market data sources: Charles Schwab Trader API (schwab-py) and a credential-free demo.

Quotes use the market-data app. Account lookup and orders use a second app.
Schwab allows one product on an app, so the two clients never share a token file.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from metrics import normalize_schwab_candles, schwab_quote

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
TRADE_CALLBACK_DEFAULT = "https://127.0.0.1:8182"
TRADE_LOGIN_HINT = "No trading token. From /root/Screener run: python backend.py login-trade --manual"


def token_path() -> Path:
    raw = os.environ.get("SCHWAB_TOKEN_PATH", "").strip()
    return Path(raw) if raw else HERE / "token.json"


def trade_token_path() -> Path:
    """Trading token beside the quote token unless SCHWAB_TRADE_TOKEN_PATH is set."""
    raw = os.environ.get("SCHWAB_TRADE_TOKEN_PATH", "").strip()
    return Path(raw) if raw else token_path().with_name("token-trade.json")


def trade_callback_url() -> str:
    return os.environ.get("SCHWAB_TRADE_CALLBACK_URL", "").strip() or TRADE_CALLBACK_DEFAULT


def trade_credentials() -> tuple[str, str] | None:
    key = os.environ.get("SCHWAB_TRADE_APP_KEY", "").strip()
    secret = os.environ.get("SCHWAB_TRADE_APP_SECRET", "").strip()
    if not key or not secret:
        return None
    return key, secret


class TradeUnavailable(Exception):
    """The trading app is not logged in. The message is safe to show."""


def _usable_trade_token(path: Path) -> bool:
    """True when the file has its own refresh token. The token value is not logged."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    token = loaded.get("token") if isinstance(loaded, dict) else None
    return isinstance(token, dict) and bool(str(token.get("refresh_token") or "").strip())


def token_writer(path: Path) -> Callable[..., None]:
    """Write one token file, mode 600. A refresh keeps that file's refresh token when Schwab omits it."""

    def write(wrapped: dict[str, Any], *_args: Any, **_kwargs: Any) -> None:
        body = wrapped
        if isinstance(wrapped, dict) and isinstance(wrapped.get("token"), dict):
            token = dict(wrapped["token"])
            if not str(token.get("refresh_token") or "").strip() and path.exists():
                try:
                    previous = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError, UnicodeError):
                    previous = None
                old = previous.get("token") if isinstance(previous, dict) else None
                if isinstance(old, dict) and old.get("refresh_token"):
                    token["refresh_token"] = old["refresh_token"]
            body = dict(wrapped)
            body["token"] = token
        payload = json.dumps(body).encode()
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    return write


def open_client(path: Path, api_key: str, app_secret: str):
    """A schwab-py client that refreshes only the token file it was given."""
    from schwab.auth import client_from_access_functions

    def read() -> dict[str, Any]:
        return json.loads(path.read_text())

    return client_from_access_functions(api_key, app_secret, read, token_writer(path))


def load_trade_client():
    """Trading client, or TradeUnavailable when the key or token is not ready."""
    creds = trade_credentials()
    path = trade_token_path()
    if creds is None or not _usable_trade_token(path):
        raise TradeUnavailable(TRADE_LOGIN_HINT)
    try:
        return open_client(path, creds[0], creds[1])
    except Exception as exc:
        raise TradeUnavailable(TRADE_LOGIN_HINT) from exc


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]


class SchwabSource:
    """Thin wrapper over schwab-py. Credentials come from the environment."""

    name = "schwab"

    def __init__(self, chunk_size: int = 100, history_per_minute: int = 100):
        path = token_path()
        if not path.exists():
            raise RuntimeError(f"No Schwab token at {path}. Run: python backend.py login")
        self.client = open_client(path, os.environ["SCHWAB_APP_KEY"], os.environ["SCHWAB_APP_SECRET"])
        self.chunk_size = chunk_size
        self._min_gap = 60.0 / max(1, history_per_minute)
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self._min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        quotes: dict[str, dict[str, Any]] = {}
        for batch in chunks(symbols, self.chunk_size):
            response = self.client.get_quotes(batch)
            response.raise_for_status()
            for symbol, body in response.json().items():
                quote = schwab_quote(body)
                if quote is not None:
                    quotes[symbol] = quote
        return quotes

    def get_daily_history(self, symbol: str, days: int) -> list[dict[str, Any]]:
        self._throttle()
        response = self.client.get_price_history_every_day(
            symbol,
            start_datetime=datetime.now(ET) - timedelta(days=days),
            end_datetime=datetime.now(ET),
        )
        response.raise_for_status()
        return normalize_schwab_candles(response.json(), ET)


_DEMO_BASES = {
    "SPY": 580, "QQQ": 500, "DIA": 430, "IWM": 225, "IWO": 290, "AAPL": 230, "GOOGL": 170,
    "META": 580, "MSFT": 420, "NFLX": 750, "NVDA": 135, "AMZN": 190, "DELL": 130, "LITE": 80,
    "AMD": 150, "NBIS": 35, "MU": 105, "SNDK": 60, "TSLA": 250, "CRWD": 320, "ALAB": 110,
    "ARM": 145, "INTC": 22, "PANW": 360,
}


class DemoSource:
    """Deterministic fake history plus a jittering live session, for running without Schwab."""

    name = "demo"

    def __init__(self):
        self._started = time.monotonic()
        self._live: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _rng(symbol: str, salt: str = "") -> random.Random:
        seed = int(hashlib.sha256((symbol + salt).encode()).hexdigest()[:12], 16)
        return random.Random(seed)

    def _full_history(self, symbol: str) -> list[dict[str, Any]]:
        if symbol in self._history:
            return self._history[symbol]
        rng = self._rng(symbol)
        today = datetime.now(ET).date()
        sessions: list[date] = []
        cursor = today - timedelta(days=1)
        while len(sessions) < 800:
            if cursor.weekday() < 5:
                sessions.append(cursor)
            cursor -= timedelta(days=1)
        sessions.reverse()
        target = _DEMO_BASES.get(symbol, rng.uniform(20, 400))
        drift = rng.uniform(-0.0002, 0.0012)
        vol = rng.uniform(0.012, 0.03)
        base_volume = rng.uniform(2e6, 6e7)
        price = target / ((1 + drift) ** len(sessions))
        bars = []
        for session in sessions:
            open_price = price * (1 + rng.gauss(0, vol / 3))
            close = open_price * (1 + rng.gauss(drift, vol))
            high = max(open_price, close) * (1 + abs(rng.gauss(0, vol / 2)))
            low = min(open_price, close) * (1 - abs(rng.gauss(0, vol / 2)))
            bars.append(
                {
                    "date": session,
                    "open": round(open_price, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close, 2),
                    "volume": int(base_volume * rng.uniform(0.5, 1.8)),
                }
            )
            price = close
        scale = target / bars[-1]["close"]
        for bar in bars:
            for key in ("open", "high", "low", "close"):
                bar[key] = round(bar[key] * scale, 2)
        self._history[symbol] = bars
        return bars

    def get_daily_history(self, symbol: str, days: int) -> list[dict[str, Any]]:
        start = datetime.now(ET).date() - timedelta(days=days)
        return [bar for bar in self._full_history(symbol) if bar["date"] >= start]

    def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        # Session volume ramps over the first 20 minutes the server is up.
        pace = min(1.0, 0.6 + (time.monotonic() - self._started) / 1200)
        quotes = {}
        for symbol in symbols:
            history = self._full_history(symbol)
            prev = history[-1]
            avg_volume = sum(bar["volume"] for bar in history[-20:]) / 20
            state = self._live.get(symbol)
            if state is None:
                rng = self._rng(symbol, datetime.now(ET).date().isoformat())
                open_price = prev["close"] * (1 + rng.gauss(0.002, 0.012))
                price = open_price * (1 + rng.gauss(0.004, 0.015))
                # Keep a range so order sizing can divide by (last − low).
                spread = max(0.005, abs(rng.gauss(0.01, 0.004)))
                state = {
                    "rng": rng,
                    "open": open_price,
                    "price": price,
                    "high": max(open_price, price) * (1 + spread),
                    "low": min(open_price, price) * (1 - spread),
                    "rvol_target": rng.uniform(0.4, 2.4),
                }
                self._live[symbol] = state
            state["price"] *= 1 + state["rng"].gauss(0, 0.0015)
            state["high"] = max(state["high"], state["price"])
            state["low"] = min(state["low"], state["price"])
            quotes[symbol] = {
                "lastPrice": round(state["price"], 2),
                "openPrice": round(state["open"], 2),
                "closePrice": prev["close"],
                "highPrice": round(state["high"], 2),
                "lowPrice": round(state["low"], 2),
                "totalVolume": int(avg_volume * state["rvol_target"] * pace),
            }
        return quotes


def make_source(mode: str, chunk_size: int, history_per_minute: int):
    if mode == "demo":
        return DemoSource()
    return SchwabSource(chunk_size=chunk_size, history_per_minute=history_per_minute)
