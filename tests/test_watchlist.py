import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import watchlist  # noqa: E402

CURRENT = {
    "groups": {"Indexes": ["SPY", "QQQ"], "Focus": ["NVDA"]},
    "tickers": ["AMD"],
}


def test_flat_list_replaces_and_merge_keeps_existing_groups():
    replaced = watchlist.apply_watchlist(CURRENT, "aapl\n# skip\nmu, tsla\nSPY\n", "replace")
    assert replaced == {"groups": {}, "tickers": ["AAPL", "MU", "TSLA", "SPY"]}

    merged = watchlist.apply_watchlist(CURRENT, "mu\nNVDA\nBRK.B\n", "merge")
    assert merged["groups"] == CURRENT["groups"]
    assert merged["tickers"] == ["AMD", "MU", "BRK.B"]


def test_csv_group_column_is_kept_and_a_flat_csv_is_not():
    text = "ticker,group\nAMD,Liquid Leaders\nTSLA,Liquid Leaders\nAAPL,\n"
    document = watchlist.apply_watchlist({"groups": {}, "tickers": []}, text, "replace")
    assert document["groups"] == {"Liquid Leaders": ["AMD", "TSLA"]}
    assert document["tickers"] == ["AAPL"]

    flat = watchlist.apply_watchlist(CURRENT, "DIA,IWM\n", "replace")
    assert flat["groups"] == {}
    assert flat["tickers"] == ["DIA", "IWM"]


def test_add_symbol_appends_once_and_rejects_a_derivative():
    added = watchlist.add_symbol(CURRENT, " mu ")
    assert added["tickers"] == ["AMD", "MU"]
    again = watchlist.add_symbol(added, "NVDA")
    assert again["groups"]["Focus"] == ["NVDA"]
    assert "NVDA" not in again["tickers"]
    with pytest.raises(watchlist.WatchlistError):
        watchlist.add_symbol(CURRENT, "/ES")
    with pytest.raises(watchlist.WatchlistError):
        watchlist.apply_watchlist(CURRENT, "   \n# nothing\n", "merge")
    with pytest.raises(watchlist.WatchlistError):
        watchlist.apply_watchlist(CURRENT, "AAPL\n", "append")


def test_dump_round_trip_drops_empty_groups():
    text = watchlist.dump_document({"groups": {"Old": [], "Focus": ["NVDA"]}, "tickers": ["AMD"]})
    assert json.loads(text) == {"groups": {"Focus": ["NVDA"]}, "tickers": ["AMD"]}
    assert watchlist.read_document(text)["groups"] == {"Focus": ["NVDA"]}
