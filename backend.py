"""Breadth monitor + screener API (FastAPI, Charles Schwab Trader API via schwab-py).

    python backend.py            # serve on http://0.0.0.0:8000 (HOST / PORT override)
    python backend.py login      # Schwab OAuth in the browser, writes the token file (--manual: paste flow)
    SCREENER_MODE=demo python backend.py   # fake data, no credentials

Settings and secrets come from the environment, or from a .env file next to this script.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

import metrics
import token_status
from market_calendar import holiday_set
from sources import ET, HERE, make_source, token_path

load_dotenv(HERE / ".env")

log = logging.getLogger("screener")

CONFIG_PATH = HERE / "config.json"
UNIVERSE_PATH = Path(os.environ.get("SCREENER_UNIVERSE", HERE / "universe.json"))
SESSION_OPEN, SESSION_CLOSE = dtime(9, 30), dtime(16, 0)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def load_universe(path: Path = UNIVERSE_PATH) -> tuple[list[str], dict[str, str]]:
    """Flat, de-duplicated ticker list plus symbol -> group.

    Accepts a plain list, {"tickers": [...]}, or {"groups": {"Name": [...]}} (or both keys).
    """
    raw = json.loads(path.read_text())
    groups: dict[str, list[str]] = {}
    if isinstance(raw, list):
        groups[""] = raw
    else:
        groups.update(raw.get("groups") or {})
        if raw.get("tickers"):
            groups.setdefault("", []).extend(raw["tickers"])
    symbols: list[str] = []
    group_of: dict[str, str] = {}
    for group, tickers in groups.items():
        for ticker in tickers:
            symbol = str(ticker).strip().upper()
            if symbol and symbol not in group_of:
                symbols.append(symbol)
                group_of[symbol] = group
    return symbols, group_of


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holiday_set(day.year)


def market_open(now: datetime) -> bool:
    return is_trading_day(now.date()) and SESSION_OPEN <= now.time() < SESSION_CLOSE


def market_session(now: datetime) -> str:
    """regular, pre, post, or closed (weekends and holidays)."""
    if not is_trading_day(now.date()):
        return "closed"
    if now.time() < SESSION_OPEN:
        return "pre"
    return "regular" if now.time() < SESSION_CLOSE else "post"


def poll_interval(config: dict[str, Any], now: datetime, mode: str) -> float:
    """Fast polling in the regular session; slower outside it so extended-hours prices still update."""
    if mode == "demo" or market_session(now) == "regular":
        return config["poll_seconds"]
    return config["off_hours_poll_seconds"]


def session_fraction(now: datetime) -> float | None:
    """Share of the regular session elapsed, for the time-of-day-paced RVOL."""
    if not is_trading_day(now.date()) or now.time() < SESSION_OPEN:
        return None
    minutes = (now.hour * 60 + now.minute + now.second / 60) - (9 * 60 + 30)
    return min(1.0, max(minutes, 1) / 390)


class Screener:
    def __init__(self, config: dict[str, Any], mode: str):
        self.config = config
        self.mode = mode
        self.source = None
        self.source_login_at: datetime | None = None
        self.lock = threading.Lock()
        self.universe: list[str] = []
        self.groups: dict[str, str] = {}
        self.universe_mtime = 0.0
        self.stats: dict[str, dict[str, Any]] = {}
        self.stats_loaded_at: datetime | None = None
        self.quotes: dict[str, dict[str, Any]] = {}
        self.quotes_at: datetime | None = None
        self.last_poll: float | None = None
        self.errors: dict[str, str] = {}
        self.chart_cache: dict[str, tuple[date, list[dict[str, Any]]]] = {}
        self.token_logged: tuple[str, float] = ("", 0.0)
        self.demo_login_at: datetime | None = None
        demo_age = os.environ.get("SCREENER_DEMO_TOKEN_AGE_HOURS")
        if mode == "demo" and demo_age:
            self.demo_login_at = datetime.now(ET) - timedelta(hours=float(demo_age))
        self.stop = threading.Event()
        self.ensure_source()
        self.reload_universe()

    def ensure_source(self) -> None:
        """Create the data source; in Schwab mode, (re)create it when a token appears or a new
        login replaces it, so the server can start before the first login and needs no restart
        after the weekly re-login."""
        if self.mode == "demo":
            if self.source is None:
                self.source = make_source("demo", 0, 0)
            return
        login_at = token_status.read_login_time(token_path())
        if login_at is None:
            if self.source is None:
                self.errors["schwab"] = f"No Schwab token at {token_path()}. Run: python backend.py login"
            return
        if self.source is not None and login_at == self.source_login_at:
            return
        try:
            source = make_source("schwab", self.config["quote_chunk_size"], self.config["history_requests_per_minute"])
        except Exception as exc:
            self.errors["schwab"] = f"Schwab client: {exc}"
            log.warning("Schwab client: %s", exc)
            return
        log.info("Schwab client loaded (login %s)", login_at.isoformat())
        self.source, self.source_login_at = source, login_at
        self.last_poll = None
        self.errors.pop("schwab", None)
        self.errors.pop("quotes", None)

    def reload_universe(self) -> bool:
        mtime = UNIVERSE_PATH.stat().st_mtime
        if mtime == self.universe_mtime:
            return False
        symbols, groups = load_universe()
        with self.lock:
            self.universe, self.groups, self.universe_mtime = symbols, groups, mtime
        log.info("Universe: %d symbols", len(symbols))
        return True

    def load_stats(self, symbols: list[str]) -> None:
        today = datetime.now(ET).date()
        for symbol in symbols:
            try:
                candles = self.source.get_daily_history(symbol, self.config["stats_history_days"])
                stats = metrics.daily_stats(candles, today)
                with self.lock:
                    self.stats[symbol] = stats
                    self.errors.pop(f"history:{symbol}", None)
            except Exception as exc:
                log.warning("History %s: %s", symbol, exc)
                with self.lock:
                    self.errors[f"history:{symbol}"] = str(exc)

    def history_loop(self) -> None:
        """Daily init at the configured ET time; also on startup and when new tickers appear."""
        hour, minute = map(int, self.config["history_init_time_et"].split(":"))
        while not self.stop.is_set():
            if self.source is None:
                self.stop.wait(15)
                continue
            now = datetime.now(ET)
            init_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            due = self.stats_loaded_at is None or (now >= init_at > self.stats_loaded_at)
            if due:
                self.load_stats(list(self.universe))
                self.stats_loaded_at = now
                self.chart_cache.clear()
            else:
                missing = [s for s in self.universe if s not in self.stats]
                if missing:
                    self.load_stats(missing)
            self.stop.wait(15)

    def token(self) -> dict[str, Any] | None:
        """Refresh-token expiry; None in demo mode unless SCREENER_DEMO_TOKEN_AGE_HOURS is set."""
        if self.mode == "demo" and self.demo_login_at is None:
            return None
        login_at = self.demo_login_at if self.mode == "demo" else token_status.read_login_time(token_path())
        return token_status.token_status(
            login_at,
            datetime.now(ET),
            self.config.get("token_lifetime_days", 7),
            self.config.get("token_warn_days", 2),
            self.config.get("token_urgent_days", 1),
        )

    def check_token(self) -> None:
        """Log a warning when the level changes, then hourly while it is not ok."""
        status = self.token()
        if status is None or status["level"] == "ok":
            self.token_logged = (status["level"] if status else "", 0.0)
            return
        level, logged_at = self.token_logged
        if level != status["level"] or time.monotonic() - logged_at >= 3600:
            log.warning(token_status.describe(status))
            self.token_logged = (status["level"], time.monotonic())

    def quote_loop(self) -> None:
        tick = self.config["poll_seconds"]
        while not self.stop.is_set():
            now = datetime.now(ET)
            try:
                self.reload_universe()
            except Exception as exc:
                self.errors["universe"] = f"universe.json: {exc}"
            self.ensure_source()
            self.check_token()
            interval = poll_interval(self.config, now, self.mode)
            if self.last_poll is None or time.monotonic() - self.last_poll >= interval - tick / 2:
                self.poll_quotes()
            self.stop.wait(tick)

    def poll_quotes(self) -> None:
        if self.source is None:
            return
        self.last_poll = time.monotonic()
        try:
            quotes = self.source.get_quotes(list(self.universe))
        except Exception as exc:
            log.warning("Quotes: %s", exc)
            self.errors["quotes"] = str(exc)
            return
        with self.lock:
            self.quotes = quotes
            self.quotes_at = datetime.now(ET)
            self.errors.pop("quotes", None)

    def clock(self) -> datetime:
        """Wall clock in ET; in demo mode SCREENER_DEMO_TIME=HH:MM pins the time of day."""
        now = datetime.now(ET)
        pinned = os.environ.get("SCREENER_DEMO_TIME") if self.mode == "demo" else None
        if pinned:
            hour, minute = map(int, pinned.split(":"))
            now = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now

    def rows(self) -> list[dict[str, Any]]:
        fraction = session_fraction(self.clock())
        with self.lock:
            out = []
            for symbol in self.universe:
                row = metrics.stock_row(symbol, self.quotes.get(symbol), self.stats.get(symbol), fraction)
                row["group"] = self.groups.get(symbol, "")
                out.append(row)
            return out

    def snapshot(self) -> dict[str, Any]:
        rows = self.rows()
        excluded = set(self.config.get("exclude_groups_from_breadth") or [])
        counted = [row for row in rows if row["group"] not in excluded]
        summary = metrics.breadth_summary(
            counted,
            len(counted),
            self.config["topd_min_pct_from_open"],
            self.config["topd_min_rvol"],
        )
        now = self.clock()
        return {
            "summary": summary,
            "stocks": rows,
            "meta": {
                "mode": self.mode,
                "market_open": market_open(now),
                "session": market_session(now),
                "poll_seconds": poll_interval(self.config, now, self.mode),
                "session_date": now.date().isoformat(),
                "quotes_at": self.quotes_at.isoformat() if self.quotes_at else None,
                "stats_loaded_at": self.stats_loaded_at.isoformat() if self.stats_loaded_at else None,
                "thresholds": {
                    "topd_min_pct_from_open": self.config["topd_min_pct_from_open"],
                    "topd_min_rvol": self.config["topd_min_rvol"],
                },
                "breadth_excluded_groups": sorted(excluded),
                "token": self.token(),
                "errors": dict(self.errors),
            },
        }

    def candles(self, symbol: str) -> dict[str, Any]:
        today = datetime.now(ET).date()
        cached = self.chart_cache.get(symbol)
        if cached and cached[0] == today:
            history = cached[1]
        else:
            if self.source is None:
                raise RuntimeError(self.errors.get("schwab", "No data source"))
            history = self.source.get_daily_history(symbol, self.config["chart_history_days"])
            self.chart_cache[symbol] = (today, history)
        live = next((r for r in self.rows() if r["symbol"] == symbol), None)
        if live and is_trading_day(today) and datetime.now(ET).time() >= SESSION_OPEN:
            history = metrics.merge_live_bar(history, live, today)
        return metrics.chart_payload(symbol, history)


def resolve_mode() -> str:
    mode = os.environ.get("SCREENER_MODE", "").lower()
    if mode in ("demo", "schwab"):
        return mode
    return "schwab" if os.environ.get("SCHWAB_APP_KEY") else "demo"


screener: Screener | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global screener
    screener = Screener(load_config(), resolve_mode())
    log.info("Mode: %s", screener.mode)
    threads = [
        threading.Thread(target=screener.history_loop, daemon=True, name="history"),
        threading.Thread(target=screener.quote_loop, daemon=True, name="quotes"),
    ]
    for thread in threads:
        thread.start()
    yield
    screener.stop.set()


app = FastAPI(title="Screener", lifespan=lifespan)


def basic_auth_ok(header: str | None, user: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        given_user, _, given_password = base64.b64decode(header[6:]).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user_ok = secrets.compare_digest(given_user.encode(), user.encode())
    password_ok = secrets.compare_digest(given_password.encode(), password.encode())
    return user_ok and password_ok


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    """HTTP basic auth on every route when DASHBOARD_USER and DASHBOARD_PASSWORD are both set."""
    user, password = os.environ.get("DASHBOARD_USER"), os.environ.get("DASHBOARD_PASSWORD")
    if user and password and not basic_auth_ok(request.headers.get("authorization"), user, password):
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Screener"'})
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/api/screener-data")
def screener_data() -> dict[str, Any]:
    return screener.snapshot()


@app.get("/api/candles")
def candles(symbol: str = Query(..., min_length=1, max_length=12)) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-/$^]+", symbol):
        raise HTTPException(400, "Bad symbol")
    try:
        return screener.candles(symbol)
    except Exception as exc:
        raise HTTPException(502, f"{symbol}: {exc}") from exc


def login(manual: bool = False) -> None:
    """Browser flow by default (catches the redirect on the callback port); --manual for headless."""
    from schwab import auth

    path = str(token_path())
    args = (
        os.environ["SCHWAB_APP_KEY"],
        os.environ["SCHWAB_APP_SECRET"],
        os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
        path,
    )
    if manual:
        auth.client_from_manual_flow(*args)
    else:
        auth.client_from_login_flow(*args, callback_timeout=1800.0, interactive=False)
    os.chmod(path, 0o600)
    status = token_status.token_status(token_status.read_login_time(Path(path)), datetime.now(ET))
    print(f"Token written to {path}; refresh token expires {status['expires_at']}")
    print("A running server picks up the new token within a few seconds; no restart needed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:2] == ["login"]:
        login(manual="--manual" in sys.argv[2:])
    else:
        import uvicorn

        host = os.environ.get("HOST") or "0.0.0.0"
        if host not in ("127.0.0.1", "localhost") and not (
            os.environ.get("DASHBOARD_USER") and os.environ.get("DASHBOARD_PASSWORD")
        ):
            log.warning("Listening on %s without DASHBOARD_USER/DASHBOARD_PASSWORD: no login required", host)
        uvicorn.run(app, host=host, port=int(os.environ.get("PORT") or "8000"))
