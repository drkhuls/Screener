"""Breadth monitor + screener API (FastAPI, Charles Schwab Trader API via schwab-py).

    python backend.py            # serve on http://127.0.0.1:8000
    python backend.py login      # one-time Schwab OAuth, writes the token file
    SCREENER_MODE=demo python backend.py   # fake data, no credentials
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import metrics
from market_calendar import holiday_set
from sources import ET, HERE, make_source

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
        self.source = make_source(mode, config["quote_chunk_size"], config["history_requests_per_minute"])
        self.lock = threading.Lock()
        self.universe: list[str] = []
        self.groups: dict[str, str] = {}
        self.universe_mtime = 0.0
        self.stats: dict[str, dict[str, Any]] = {}
        self.stats_loaded_at: datetime | None = None
        self.quotes: dict[str, dict[str, Any]] = {}
        self.quotes_at: datetime | None = None
        self.errors: dict[str, str] = {}
        self.chart_cache: dict[str, tuple[date, list[dict[str, Any]]]] = {}
        self.stop = threading.Event()
        self.reload_universe()

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

    def quote_loop(self) -> None:
        while not self.stop.is_set():
            now = datetime.now(ET)
            try:
                self.reload_universe()
            except Exception as exc:
                self.errors["universe"] = f"universe.json: {exc}"
            live = market_open(now) or self.mode == "demo"
            stale = self.quotes_at is None or (
                (now - self.quotes_at).total_seconds() >= self.config["off_hours_poll_seconds"]
            )
            if live or stale:
                self.poll_quotes()
            self.stop.wait(self.config["poll_seconds"])

    def poll_quotes(self) -> None:
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

    def rows(self) -> list[dict[str, Any]]:
        fraction = session_fraction(datetime.now(ET))
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
        now = datetime.now(ET)
        return {
            "summary": summary,
            "stocks": rows,
            "meta": {
                "mode": self.mode,
                "market_open": market_open(now),
                "session_date": now.date().isoformat(),
                "quotes_at": self.quotes_at.isoformat() if self.quotes_at else None,
                "stats_loaded_at": self.stats_loaded_at.isoformat() if self.stats_loaded_at else None,
                "thresholds": {
                    "topd_min_pct_from_open": self.config["topd_min_pct_from_open"],
                    "topd_min_rvol": self.config["topd_min_rvol"],
                },
                "errors": dict(self.errors),
            },
        }

    def candles(self, symbol: str) -> dict[str, Any]:
        today = datetime.now(ET).date()
        cached = self.chart_cache.get(symbol)
        if cached and cached[0] == today:
            history = cached[1]
        else:
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


def login() -> None:
    from schwab import auth

    token_path = os.environ.get("SCHWAB_TOKEN_PATH", str(HERE / "token.json"))
    auth.client_from_manual_flow(
        os.environ["SCHWAB_APP_KEY"],
        os.environ["SCHWAB_APP_SECRET"],
        os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
        token_path,
    )
    print(f"Token written to {token_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:2] == ["login"]:
        login()
    else:
        import uvicorn

        uvicorn.run(
            app,
            host=os.environ.get("SCREENER_HOST", "127.0.0.1"),
            port=int(os.environ.get("SCREENER_PORT", "8000")),
        )
