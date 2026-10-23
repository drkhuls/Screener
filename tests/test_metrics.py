import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metrics  # noqa: E402
from backend import load_universe  # noqa: E402
from sources import DemoSource, chunks  # noqa: E402


def _bars(n, today=date(2026, 10, 22)):
    return [
        {"date": today - timedelta(days=n - i), "open": 10.0 + i, "high": 11.0 + i, "low": 9.0 + i,
         "close": 10.0 + i, "volume": 1000 * (i + 1)}
        for i in range(n)
    ]


def test_daily_stats_uses_last_20_completed_sessions():
    today = date(2026, 10, 22)
    bars = _bars(30, today) + [{"date": today, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    stats = metrics.daily_stats(bars, today)
    assert stats["prev_close"] == 39.0
    assert stats["sma_20"] == sum(10.0 + i for i in range(10, 30)) / 20
    assert stats["avg_volume_20d"] == sum(1000 * (i + 1) for i in range(10, 30)) / 20


def test_daily_stats_short_history():
    stats = metrics.daily_stats(_bars(5), date(2026, 10, 22))
    assert stats["sma_20"] is None and stats["avg_volume_20d"] is None
    assert stats["prev_close"] == 14.0


def test_stock_row_formulas_and_prev_close_preference():
    quote = {"lastPrice": 105, "openPrice": 100, "closePrice": 98, "totalVolume": 1_500_000}
    row = metrics.stock_row("X", quote, {"prev_close": 90, "avg_volume_20d": 1_000_000})
    assert row["pct_from_open"] == 5.0
    assert row["pct_from_close"] == round((105 - 98) / 98 * 100, 2)
    assert row["rvol"] == 1.5


def test_stock_row_guards_missing_and_zero():
    row = metrics.stock_row("X", {"lastPrice": 10, "openPrice": 0, "closePrice": 0}, {"avg_volume_20d": 0})
    assert row["pct_from_open"] is None and row["pct_from_close"] is None and row["rvol"] is None
    row = metrics.stock_row("X", None, None)
    assert row["price"] is None and row["pct_from_open"] is None


def test_breadth_summary_thresholds_inclusive():
    rows = [
        {"price": 102, "open": 100, "prev_close": 99, "pct_from_open": 2.0, "rvol": 1.2},
        {"price": 98, "open": 101, "prev_close": 100, "pct_from_open": -2.97, "rvol": 3.0},
        {"price": None, "open": None, "prev_close": 50, "pct_from_open": None, "rvol": None},
    ]
    s = metrics.breadth_summary(rows, 3)
    assert s == {
        "universe_count": 3, "gapped_up": 2, "positive_since_close": 1,
        "moved_up_since_open": 1, "topd_moved_up_since_open": 1,
    }
    assert metrics.breadth_summary(rows, 3, topd_min_rvol=1.21)["topd_moved_up_since_open"] == 0


def test_moving_averages():
    values = [float(v) for v in range(1, 11)]
    assert metrics.sma(values, 3)[:3] == [None, None, 2.0]
    ema = metrics.ema(values, 3)
    assert ema[2] == 2.0 and ema[3] == 4 * 0.5 + 2.0 * 0.5


def test_chart_payload_has_200_sma_with_enough_history():
    bars = DemoSource().get_daily_history("AAPL", 730)
    payload = metrics.chart_payload("AAPL", bars)
    assert len(payload["candles"]) > 400
    assert len(payload["sma200"]) == len(payload["candles"]) - 199
    assert payload["candles"][0]["time"] < payload["candles"][-1]["time"]


def test_merge_live_bar_replaces_today():
    today = date(2026, 10, 22)
    bars = _bars(3, today) + [{"date": today, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    merged = metrics.merge_live_bar(bars, {"price": 12, "open": 10, "high": 13, "low": 9, "volume": 5}, today)
    assert len(merged) == 4 and merged[-1]["close"] == 12 and merged[-1]["high"] == 13


def test_universe_flattens_and_dedupes(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"groups": {"A": ["spy", "AAPL"], "B": ["AAPL", "MU"]}, "tickers": ["MU", "ZZ"]}))
    symbols, groups = load_universe(path)
    assert symbols == ["SPY", "AAPL", "MU", "ZZ"]
    assert groups["AAPL"] == "A" and groups["ZZ"] == ""
    path.write_text(json.dumps(["a", "b", "a"]))
    assert load_universe(path)[0] == ["A", "B"]


def test_repo_universe_is_24_unique():
    symbols, _ = load_universe()
    assert len(symbols) == 24 == len(set(symbols))


def test_quote_chunking():
    assert [len(c) for c in chunks([str(i) for i in range(250)], 100)] == [100, 100, 50]
