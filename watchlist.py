"""Turn a txt or csv watchlist into the universe.json document the screener already reloads."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

# Stocks and ETFs. One leading letter, then letters, digits, dots, or hyphens (BRK.B, BF-B).
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MAX_SYMBOLS = 2000
MAX_TEXT = 500_000
_SYMBOL_HEADERS = {"symbol", "ticker", "tickers", "symbols"}
_GROUP_HEADERS = {"group", "groups", "list", "watchlist"}


class WatchlistError(ValueError):
    """The upload or the typed ticker cannot be applied. The message is safe to show."""


def read_document(raw: Any) -> dict[str, Any]:
    """Normalize a universe.json value, a JSON string, or a missing file's stand-in."""
    if raw is None:
        return {"groups": {}, "tickers": []}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {"groups": {}, "tickers": []}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WatchlistError("universe.json is not valid JSON") from exc
    if isinstance(raw, list):
        return {"groups": {}, "tickers": [_clean_symbol(item) for item in raw if _clean_symbol(item)]}
    if not isinstance(raw, dict):
        raise WatchlistError("universe.json must be a list or an object")
    groups: dict[str, list[str]] = {}
    for name, symbols in (raw.get("groups") or {}).items():
        kept = []
        for item in symbols or []:
            symbol = _clean_symbol(item)
            if symbol and symbol not in kept:
                kept.append(symbol)
        if str(name).strip():
            groups[str(name).strip()] = kept
    tickers = []
    for item in raw.get("tickers") or []:
        symbol = _clean_symbol(item)
        if symbol and symbol not in tickers:
            tickers.append(symbol)
    return {"groups": groups, "tickers": tickers}


def dump_document(document: dict[str, Any]) -> str:
    """Pretty JSON. Empty groups are dropped. Order of groups and tickers is kept."""
    groups = {name: list(symbols) for name, symbols in document.get("groups", {}).items() if symbols}
    tickers = list(document.get("tickers") or [])
    return json.dumps({"groups": groups, "tickers": tickers}, indent=2) + "\n"


def parse_watchlist(text: str) -> list[tuple[str, str]]:
    """Symbols in file order, with a group name when the file has one.

    A header row ``ticker,group`` keeps groups. Anything else is a flat list:
    commas, tabs, and whitespace are separators. Blank lines and ``#`` comments are skipped.
    """
    if len(text) > MAX_TEXT:
        raise WatchlistError("Watchlist file is too large")
    rows = _rows(text)
    header = [cell.strip().lower() for cell in rows[0]] if rows else []
    if any(cell in _SYMBOL_HEADERS for cell in header):
        pairs = _from_header(rows, header)
    else:
        pairs = _flat_tokens(text)
    if not pairs:
        raise WatchlistError("No tickers found")
    if len(pairs) > MAX_SYMBOLS:
        raise WatchlistError("Watchlist is too long")
    return pairs


def apply_watchlist(document: dict[str, Any], text: str, mode: str) -> dict[str, Any]:
    """``replace`` rebuilds the universe from the file. ``merge`` adds to it.

    A flat file does not invent groups. On merge, a symbol that already has a group stays there.
    On replace, groups are kept only when the file itself names them.
    """
    action = (mode or "").strip().lower()
    if action not in {"replace", "merge"}:
        raise WatchlistError("Choose replace or merge")
    pairs = parse_watchlist(text)
    base = {"groups": {}, "tickers": []} if action == "replace" else read_document(document)
    named = any(group for _, group in pairs)
    if action == "merge" and not named:
        existing = _members(base)
        for symbol, _group in pairs:
            if symbol not in existing:
                base["tickers"].append(symbol)
                existing.add(symbol)
        return base
    for symbol, group in pairs:
        _place(base, symbol, group if named else "")
    return base


def add_symbol(document: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Append one ticker. A symbol that is already in the universe stays where it is."""
    cleaned = _clean_symbol(symbol)
    if not cleaned or not SYMBOL.fullmatch(cleaned):
        raise WatchlistError("Enter a stock or ETF ticker")
    base = read_document(document)
    if cleaned not in _members(base):
        base["tickers"].append(cleaned)
    return base


def _clean_symbol(value: Any) -> str:
    text = str(value or "").strip().upper().lstrip("$")
    return text if SYMBOL.fullmatch(text) else ""


def _members(document: dict[str, Any]) -> set[str]:
    found = set(document.get("tickers") or [])
    for symbols in (document.get("groups") or {}).values():
        found.update(symbols)
    return found


def _place(document: dict[str, Any], symbol: str, group: str) -> None:
    for name, symbols in document["groups"].items():
        document["groups"][name] = [item for item in symbols if item != symbol]
    document["tickers"] = [item for item in document["tickers"] if item != symbol]
    if group:
        document["groups"].setdefault(group, [])
        if symbol not in document["groups"][group]:
            document["groups"][group].append(symbol)
    elif symbol not in document["tickers"]:
        document["tickers"].append(symbol)


def _rows(text: str) -> list[list[str]]:
    stripped = text.lstrip("\ufeff")
    sample = next((line for line in stripped.splitlines() if line.strip() and not line.strip().startswith("#")), "")
    delimiter = "," if "," in sample else ("\t" if "\t" in sample else None)
    rows: list[list[str]] = []
    if delimiter:
        for row in csv.reader(io.StringIO(stripped), delimiter=delimiter):
            cells = [_uncomment(cell) for cell in row]
            if any(cells) and not (cells[0].startswith("#")):
                rows.append(cells)
        return rows
    for line in stripped.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        rows.append(line.split())
    return rows


def _uncomment(cell: str) -> str:
    return cell.split("#", 1)[0].strip()


def _from_header(rows: list[list[str]], header: list[str]) -> list[tuple[str, str]]:
    symbol_at = next(index for index, cell in enumerate(header) if cell in _SYMBOL_HEADERS)
    group_at = next((index for index, cell in enumerate(header) if cell in _GROUP_HEADERS), None)
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if symbol_at >= len(row):
            continue
        group = ""
        if group_at is not None and group_at < len(row):
            group = row[group_at].strip()
        for part in re.split(r"[\s;]+", row[symbol_at]):
            symbol = _clean_symbol(part)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            pairs.append((symbol, group))
    return pairs


def _flat_tokens(text: str) -> list[tuple[str, str]]:
    """Every ticker-shaped word. Commas, tabs, and spaces are all separators."""
    kept: list[str] = []
    for line in text.lstrip("\ufeff").splitlines():
        if line.strip().startswith("#"):
            continue
        kept.append(line.split("#", 1)[0])
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"\$?[A-Za-z][A-Za-z0-9.\-]*", "\n".join(kept)):
        symbol = _clean_symbol(match.group(0))
        if symbol and symbol not in seen:
            seen.add(symbol)
            pairs.append((symbol, ""))
    return pairs
