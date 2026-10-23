"""Pure calculations for the screener: daily stats, live rows, breadth, chart series."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Sequence


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _pct(price: float | None, base: float | None) -> float | None:
    if price is None or not base:
        return None
    return round((price - base) / base * 100, 2)


def completed_candles(candles: Sequence[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Daily candles strictly before ``today``; drops a partial bar for the live session."""
    return [bar for bar in candles if bar["date"] < today]


def daily_stats(candles: Sequence[dict[str, Any]], today: date, window: int = 20) -> dict[str, Any]:
    """prev_close, sma_20 and avg_volume_20d from the last ``window`` completed sessions."""
    done = completed_candles(candles, today)
    closes = [bar["close"] for bar in done if bar.get("close") is not None]
    volumes = [bar["volume"] for bar in done if bar.get("volume") is not None]
    recent_closes = closes[-window:]
    recent_volumes = volumes[-window:]
    return {
        "prev_close": closes[-1] if closes else None,
        "prev_close_date": done[-1]["date"].isoformat() if done else None,
        "sma_20": round(sum(recent_closes) / window, 4) if len(recent_closes) == window else None,
        "avg_volume_20d": sum(recent_volumes) / window if len(recent_volumes) == window else None,
    }


def schwab_quote(body: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one get_quotes entry. Outside the session ``quote.lastPrice`` stays at the 16:00
    print; the live pre/post-market trade is in ``extended``, so use it when it is newer."""
    quote = body.get("quote") if isinstance(body, dict) else None
    if not isinstance(quote, dict):
        return None
    out = dict(quote)
    extended = body.get("extended") or {}
    ext_price = _num(extended.get("lastPrice"))
    if ext_price and (_num(extended.get("tradeTime")) or 0) > (_num(quote.get("tradeTime")) or 0):
        regular = body.get("regular") or {}
        out["regularLastPrice"] = _num(regular.get("regularMarketLastPrice")) or _num(quote.get("lastPrice"))
        out["lastPrice"] = ext_price
        out["extendedHours"] = True
    return out


def stock_row(
    symbol: str,
    quote: dict[str, Any] | None,
    stats: dict[str, Any] | None,
    session_fraction: float | None = None,
) -> dict[str, Any]:
    """One table row. Missing or zero inputs leave the dependent fields as None."""
    quote = quote or {}
    stats = stats or {}
    price = _num(quote.get("lastPrice"))
    open_price = _num(quote.get("openPrice")) or None
    # Schwab's closePrice is the prior session's close while the market is open.
    prev_close = _num(quote.get("closePrice")) or _num(stats.get("prev_close")) or None
    volume = _num(quote.get("totalVolume"))
    avg_volume = _num(stats.get("avg_volume_20d")) or None
    rvol = round(volume / avg_volume, 2) if volume is not None and avg_volume else None
    rvol_pace = None
    if volume is not None and avg_volume and session_fraction:
        rvol_pace = round(volume / (avg_volume * session_fraction), 2)
    extended = bool(quote.get("extendedHours"))
    return {
        "symbol": symbol,
        "price": price,
        "extended_hours": extended,
        "regular_price": _num(quote.get("regularLastPrice")) if extended else price,
        "open": open_price,
        "prev_close": prev_close,
        "high": _num(quote.get("highPrice")),
        "low": _num(quote.get("lowPrice")),
        "pct_from_open": _pct(price, open_price),
        "pct_from_close": _pct(price, prev_close),
        "volume": int(volume) if volume is not None else None,
        "avg_volume_20d": round(avg_volume) if avg_volume else None,
        "rvol": rvol,
        "rvol_pace": rvol_pace,
        "sma_20": stats.get("sma_20"),
    }


def breadth_summary(
    rows: Iterable[dict[str, Any]],
    universe_count: int,
    topd_min_pct_from_open: float = 2.0,
    topd_min_rvol: float = 1.2,
) -> dict[str, int]:
    def above(a: Any, b: Any) -> bool:
        return a is not None and b is not None and a > b

    gapped_up = positive = moved_up = topd = 0
    for row in rows:
        gapped_up += above(row["open"], row["prev_close"])
        positive += above(row["price"], row["prev_close"])
        moved_up += above(row["price"], row["open"])
        pct, rvol = row["pct_from_open"], row["rvol"]
        topd += pct is not None and rvol is not None and pct >= topd_min_pct_from_open and rvol >= topd_min_rvol
    return {
        "universe_count": universe_count,
        "gapped_up": gapped_up,
        "positive_since_close": positive,
        "moved_up_since_open": moved_up,
        "topd_moved_up_since_open": topd,
    }


def sma(values: Sequence[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= length:
            running -= values[i - length]
        if i >= length - 1:
            out[i] = running / length
    return out


def ema(values: Sequence[float], length: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``length`` values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    alpha = 2 / (length + 1)
    current = sum(values[:length]) / length
    out[length - 1] = current
    for i in range(length, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        out[i] = current
    return out


def _line(times: list[str], values: list[float | None]) -> list[dict[str, Any]]:
    return [{"time": t, "value": round(v, 4)} for t, v in zip(times, values) if v is not None]


def merge_live_bar(candles: list[dict[str, Any]], row: dict[str, Any] | None, today: date) -> list[dict[str, Any]]:
    """Replace or append today's bar from the live quote so the chart shows the session.
    The daily bar uses the regular-session price, not pre/post-market trades."""
    price = row.get("regular_price", row.get("price")) if row else None
    if price is None or row.get("open") is None:
        return candles
    bar = {
        "date": today,
        "open": row["open"],
        "high": max(v for v in (row.get("high"), row["open"], price) if v is not None),
        "low": min(v for v in (row.get("low"), row["open"], price) if v is not None),
        "close": price,
        "volume": row.get("volume") or 0,
    }
    kept = [c for c in candles if c["date"] < today]
    return kept + [bar]


def chart_payload(symbol: str, candles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Lightweight Charts series: candlesticks, volume histogram, EMA 10/20, SMA 50/200."""
    times = [bar["date"].isoformat() for bar in candles]
    closes = [float(bar["close"]) for bar in candles]
    up_color, down_color = "#26a69a", "#ef5350"
    return {
        "symbol": symbol,
        "candles": [
            {"time": t, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]}
            for t, b in zip(times, candles)
        ],
        "volume": [
            {
                "time": t,
                "value": b.get("volume") or 0,
                "color": (up_color if b["close"] >= b["open"] else down_color) + "80",
            }
            for t, b in zip(times, candles)
        ],
        "ema10": _line(times, ema(closes, 10)),
        "ema20": _line(times, ema(closes, 20)),
        "sma50": _line(times, sma(closes, 50)),
        "sma200": _line(times, sma(closes, 200)),
    }


def normalize_schwab_candles(payload: dict[str, Any], tz) -> list[dict[str, Any]]:
    """Schwab pricehistory candles (epoch ms) to dicts keyed by the ET session date."""
    out = []
    for bar in payload.get("candles") or []:
        try:
            session = datetime.fromtimestamp(bar["datetime"] / 1000, tz).date()
            out.append(
                {
                    "date": session,
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": int(bar.get("volume") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda bar: bar["date"])
    return out
