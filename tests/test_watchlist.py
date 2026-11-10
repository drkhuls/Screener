import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend  # noqa: E402
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


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "demo")
    with TestClient(backend.app) as client:
        yield client


def test_page_has_upload_scan_filter_and_chart_controls(demo_client):
    html = demo_client.get("/").text
    assert 'id="watchlist-file"' in html and 'id="watchlist-replace"' in html and 'id="watchlist-merge"' in html
    assert 'id="add-symbol"' in html and 'id="add-ticker"' in html
    assert 'id="scan-clear"' in html and "Show all" in html
    assert 'id="bw"' in html and ">B&amp;W<" in html
    assert ">Dark mode<" in html and "Dark chart" not in html
    assert 'class="ma-type"' in html and "Show or hide" in html
    assert 'id="order-entry"' in html and 'id="order-confirm"' in html and 'id="order-mic"' in html
    assert 'id="pager"' in html and "PAGE_SIZE = 20" in html
    assert "Paced RVOL" not in html and "rvol_pace" not in html


def test_upload_replaces_or_merges_and_add_keeps_groups(demo_client, tmp_path, monkeypatch):
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"groups": {"Indexes": ["SPY"]}, "tickers": []}))
    original = backend.UNIVERSE_PATH
    screener = backend.screener
    monkeypatch.setattr(backend, "UNIVERSE_PATH", path)
    screener.universe_mtime = 0
    screener.reload_universe()
    try:
        replaced = demo_client.post(
            "/api/universe",
            json={"mode": "replace", "text": "ticker,group\nAMD,Focus\nTSLA,Focus\nAAPL\n"},
        )
        assert replaced.status_code == 200
        assert replaced.json()["symbols"] == ["AMD", "TSLA", "AAPL"]
        saved = json.loads(path.read_text())
        assert saved["groups"] == {"Focus": ["AMD", "TSLA"]}
        assert saved["tickers"] == ["AAPL"]

        merged = demo_client.post("/api/universe", json={"mode": "merge", "text": "NVDA\nAMD\n"})
        assert merged.status_code == 200
        saved = json.loads(path.read_text())
        assert saved["groups"]["Focus"] == ["AMD", "TSLA"]
        assert saved["tickers"] == ["AAPL", "NVDA"]

        added = demo_client.post("/api/universe/symbol", json={"symbol": "mu"})
        assert added.status_code == 200 and "MU" in added.json()["symbols"]
        assert json.loads(path.read_text())["groups"]["Focus"] == ["AMD", "TSLA"]
        bad = demo_client.post("/api/universe/symbol", json={"symbol": "/ES"})
        assert bad.status_code == 400 and "ticker" in bad.json()["detail"].lower()
        assert "/ES" not in path.read_text()
    finally:
        backend.UNIVERSE_PATH = original
        screener.universe_mtime = 0
        screener.reload_universe()
