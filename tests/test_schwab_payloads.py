"""Shapes captured from live Schwab responses after the 2026-10-22 close (trimmed)."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metrics  # noqa: E402
from sources import ET  # noqa: E402

SPY_AFTER_HOURS = {
    "symbol": "SPY",
    "quote": {
        "lastPrice": 764.1157, "openPrice": 766.37, "closePrice": 773.09, "highPrice": 767.71,
        "lowPrice": 761.165, "totalVolume": 54117128, "netChange": -8.9743, "tradeTime": 1792713588998,
    },
    "regular": {"regularMarketLastPrice": 763.96, "regularMarketTradeTime": 1792713600003},
    "extended": {"lastPrice": 764.24, "totalVolume": 0, "tradeTime": 1792716071581},
}


def test_after_hours_uses_newer_extended_trade():
    quote = metrics.schwab_quote(SPY_AFTER_HOURS)
    assert quote["lastPrice"] == 764.24 and quote["extendedHours"] is True
    row = metrics.stock_row("SPY", quote, {"prev_close": 773.09, "avg_volume_20d": 40298265})
    assert row["extended_hours"] and row["regular_price"] == 763.96
    assert row["pct_from_close"] == round((764.24 - 773.09) / 773.09 * 100, 2)
    assert row["volume"] == 54117128


def test_regular_session_keeps_quote_last_price():
    body = {**SPY_AFTER_HOURS, "extended": {"lastPrice": 760.0, "tradeTime": 1792650000000}}
    quote = metrics.schwab_quote(body)
    assert quote["lastPrice"] == 764.1157 and "extendedHours" not in quote
    row = metrics.stock_row("SPY", quote, None)
    assert row["extended_hours"] is False and row["regular_price"] == 764.1157
    assert metrics.schwab_quote({"symbol": "X"}) is None
    assert metrics.schwab_quote({**SPY_AFTER_HOURS, "extended": {"lastPrice": 0}})["lastPrice"] == 764.1157


def test_live_bar_uses_regular_price_after_hours():
    quote = metrics.schwab_quote(SPY_AFTER_HOURS)
    row = metrics.stock_row("SPY", quote, None)
    merged = metrics.merge_live_bar([], row, date(2026, 10, 22))
    assert merged[-1]["close"] == 763.96


def test_daily_candles_are_midnight_et_and_today_is_excluded_from_stats():
    payload = {"symbol": "SPY", "empty": False, "candles": [
        {"open": 770.0, "high": 775.0, "low": 768.0, "close": 773.09, "volume": 40000000, "datetime": 1792555200000},
        {"open": 766.37, "high": 767.71, "low": 761.165, "close": 763.96, "volume": 54117113, "datetime": 1792641600000},
    ]}
    bars = metrics.normalize_schwab_candles(payload, ET)
    assert [b["date"] for b in bars] == [date(2026, 10, 21), date(2026, 10, 22)]
    stats = metrics.daily_stats(bars, date(2026, 10, 22))
    assert stats["prev_close"] == 773.09 == SPY_AFTER_HOURS["quote"]["closePrice"]
