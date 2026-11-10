import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend  # noqa: E402


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_basic_auth_check():
    assert backend.basic_auth_ok(_basic("pardeep", "s3cret:x"), "pardeep", "s3cret:x")
    assert not backend.basic_auth_ok(_basic("pardeep", "wrong"), "pardeep", "s3cret:x")
    assert not backend.basic_auth_ok("Basic !!!", "pardeep", "s3cret:x")
    assert not backend.basic_auth_ok(None, "pardeep", "s3cret:x")


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "demo")
    with TestClient(backend.app) as client:
        yield client


def test_auth_enforced_only_when_configured(demo_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert demo_client.get("/api/screener-data").status_code == 200
    monkeypatch.setenv("DASHBOARD_USER", "pardeep")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    response = demo_client.get("/")
    assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Basic")
    assert demo_client.get("/api/screener-data", headers={"Authorization": _basic("pardeep", "nope")}).status_code == 401
    assert demo_client.get("/api/screener-data", headers={"Authorization": _basic("pardeep", "s3cret")}).status_code == 200


def test_schwab_mode_starts_without_token_and_loads_new_login(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(token))
    created = []

    def fake_source(mode, *_):
        created.append(mode)
        return object()

    monkeypatch.setattr(backend, "make_source", fake_source)
    screener = backend.Screener(backend.load_config(), "schwab")
    assert screener.source is None and "No Schwab token" in screener.errors["schwab"]
    assert screener.snapshot()["meta"]["token"]["level"] == "missing"

    token.write_text(json.dumps({"creation_timestamp": time.time() - 3600, "token": {}}))
    screener.ensure_source()
    first = screener.source
    assert first is not None and "schwab" not in screener.errors
    screener.ensure_source()
    assert screener.source is first and len(created) == 1

    token.write_text(json.dumps({"creation_timestamp": time.time(), "token": {}}))
    screener.ensure_source()
    assert screener.source is not first and len(created) == 2
    assert screener.last_poll is None


def _stable_quotes(self, symbols):
    return {
        symbol: {"lastPrice": 140, "lowPrice": 135, "highPrice": 142, "openPrice": 136}
        for symbol in symbols
    }


def test_order_panel_is_under_the_table(demo_client):
    html = demo_client.get("/").text
    table = html.index('<section class="box table-wrap">')
    order = html.index('<section class="box" id="order-entry">')
    chart = html.index('<section class="box chart-box">')
    assert table < order < chart
    for label in ("0.25%", "0.33%", "0.5%", "1%"):
        assert label in html
    assert 'id="order-mic"' in html
    assert "webkitSpeechRecognition" in html
    assert 'id="order-confirm"' in html and 'id="order-cancel"' in html
    assert ">Audio<" not in html


def test_demo_buy_nvda_shows_the_modal_math_and_does_not_place(demo_client, monkeypatch):
    monkeypatch.delenv("ORDER_ALERT_EMAIL", raising=False)
    monkeypatch.delenv("ORDER_ALERT_PHONE", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setattr("sources.DemoSource.get_quotes", _stable_quotes)

    preview = demo_client.post("/api/orders/preview", json={"instruction": "buy NVDA", "risk_pct": 0.5})
    assert preview.status_code == 200
    body = preview.json()
    assert body["placed"] is False
    assert body["plan"]["shares"] == 250
    assert body["plan"]["stop_source"] == "low_of_day"
    assert body["summary"] == (
        "Buy 250 NVDA at market. Account equity: $250,000. Risk: 0.5% = $1,250. "
        "Stop-loss: stop-market at $135.00. Send this order?"
    )

    confirmed = demo_client.post("/api/orders/confirm", json={"preview_id": body["preview_id"]})
    assert confirmed.status_code == 200
    payload = confirmed.json()
    assert payload["placed"] is False and payload["mode"] == "demo"
    assert "Demo mode: order not sent" in payload["alert"]["message"]
    assert "Buy 250 NVDA" in payload["alert"]["message"]

    again = demo_client.post("/api/orders/confirm", json={"preview_id": body["preview_id"]})
    assert again.status_code == 400
    assert "No order was sent" in again.json()["detail"]


def test_demo_rejects_a_zero_stop_distance_and_derivatives(demo_client, monkeypatch):
    monkeypatch.setattr(
        "sources.DemoSource.get_quotes",
        lambda self, symbols: {symbol: {"lastPrice": 100, "lowPrice": 100, "highPrice": 101} for symbol in symbols},
    )
    blocked = demo_client.post("/api/orders/preview", json={"instruction": "NVDA", "risk_pct": 0.5})
    assert blocked.status_code == 400
    assert "at or below" in blocked.json()["detail"]

    option = demo_client.post("/api/orders/preview", json={"instruction": "buy 1 AAPL 190 call", "risk_pct": 0.5})
    assert option.status_code == 400
    assert "Options" in option.json()["detail"]
    future = demo_client.post("/api/orders/preview", json={"instruction": "buy /ES", "risk_pct": 0.5})
    assert future.status_code == 400
    assert "Futures" in future.json()["detail"]
