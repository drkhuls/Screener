import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend  # noqa: E402
from alerts import dispatch_order_alert  # noqa: E402
from orders import (  # noqa: E402
    OrderBook,
    OrderError,
    equity_order,
    normalize_ticket,
    read_account,
    tif_choices,
)


@pytest.fixture(autouse=True)
def _no_alert_destinations(monkeypatch):
    monkeypatch.delenv("ORDER_ALERT_EMAIL", raising=False)
    monkeypatch.delenv("ORDER_ALERT_PHONE", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)


def test_ticker_box_rejects_a_sentence_and_keeps_a_symbol():
    ticket = normalize_ticket({"symbol": "nvda", "shares": 100})
    assert ticket.symbol == "NVDA" and ticket.side == "buy"
    assert ticket.order_type == "market" and ticket.tif == "day"
    assert ticket.session == "NORMAL" and ticket.duration == "DAY"
    with pytest.raises(OrderError, match="ticker only"):
        normalize_ticket({"symbol": "buy NVDA", "shares": 100})


def test_market_and_stop_are_day_only_and_limit_has_the_full_menu():
    assert tif_choices("market") == ["day"]
    assert tif_choices("stop") == ["day"]
    assert tif_choices("limit") == ["day", "gtc", "ext", "gtc_ext", "exto", "gtc_exto", "am", "pm"]
    with pytest.raises(OrderError, match="Day only"):
        normalize_ticket({"symbol": "TSLA", "order_type": "stop", "tif": "exto", "shares": 100, "price": 200})
    with pytest.raises(OrderError, match="Day only"):
        normalize_ticket({"symbol": "TSLA", "order_type": "market", "tif": "gtc", "shares": 100})
    limit = normalize_ticket({"symbol": "TSLA", "order_type": "limit", "tif": "gtc_exto", "shares": 100, "price": 250})
    assert (limit.duration, limit.session) == ("GOOD_TILL_CANCEL", "EXTO")


def test_limit_and_stop_orders_carry_the_schwab_session():
    built = equity_order(normalize_ticket({
        "symbol": "NVDA", "side": "sell", "order_type": "limit", "tif": "ext", "shares": 110, "price": 140.5,
    })).build()
    assert built["orderType"] == "LIMIT"
    assert built["session"] == "SEAMLESS" and built["duration"] == "DAY"
    assert built["price"] == "140.50"
    assert built["orderLegCollection"][0]["instruction"] == "SELL"
    assert built["orderLegCollection"][0]["quantity"] == 110
    stop = equity_order(normalize_ticket({
        "symbol": "AMD", "order_type": "stop", "shares": 100, "price": 90,
    })).build()
    assert stop["orderType"] == "STOP" and stop["session"] == "NORMAL" and stop["stopPrice"] == "90.00"


def test_account_401_surfaces_the_schwab_error():
    class Response:
        status_code = 401
        text = '{"message":"insufficient_scope"}'

        def json(self):
            return {"message": "insufficient_scope"}

    class Client:
        def get_account_numbers(self):
            return Response()

    with pytest.raises(OrderError, match="401") as caught:
        read_account(Client())
    message = str(caught.value)
    assert "insufficient_scope" in message
    assert "Not a stock or ETF" not in message


def test_review_does_not_place_and_send_is_the_only_schwab_call(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "demo")
    calls = {"quotes": 0, "placed": 0}

    def quotes(_self, symbols):
        calls["quotes"] += 1
        return {symbol: {"lastPrice": 140, "lowPrice": 135} for symbol in symbols}

    def place(*_args, **_kwargs):
        calls["placed"] += 1
        raise AssertionError("demo must not place")

    monkeypatch.setattr("sources.DemoSource.get_quotes", quotes)
    monkeypatch.setattr("orders.OrderBook._place", place)
    with TestClient(backend.app) as client:
        html = client.get("/").text
        assert html.index('id="order-entry"') < html.index('id="chart"')
        assert 'id="order-send"' in html and 'id="order-cancel"' in html
        preview = client.post("/api/orders/preview", json={"symbol": "NVDA", "shares": 100})
        assert preview.status_code == 200
        body = preview.json()
        assert body["placed"] is False
        assert body["plan"]["session"] == "NORMAL" and body["plan"]["shares"] == 100
        assert "Send this order?" in body["summary"]
        assert calls["placed"] == 0
        sent = client.post("/api/orders/send", json={"preview_id": body["preview_id"]})
        assert sent.status_code == 200
        payload = sent.json()
        assert payload["placed"] is False and payload["mode"] == "demo"
        assert "Demo mode: order not sent" in payload["alert"]["message"]
        assert calls["placed"] == 0


def test_send_is_the_only_place_and_a_401_stays_a_schwab_error():
    class Response:
        status_code = 201
        headers = {"Location": "https://api.schwabapi.com/trader/v1/accounts/x/orders/42"}
        text = ""

        def json(self):
            return {}

    placed = []

    def account():
        raise OrderError("Schwab accounts returned 401. This token cannot read accounts. insufficient_scope")

    def place(account_hash, order):
        placed.append(order.build())
        return Response()

    book = OrderBook(
        mode=lambda: "schwab",
        quote=lambda _symbol: {"lastPrice": 11},
        account=account,
        place=place,
    )
    preview = book.preview({
        "symbol": "NVDA", "shares": 100, "order_type": "limit", "tif": "exto", "price": 10,
    })
    assert preview["plan"]["session"] == "EXTO" and preview["placed"] is False
    with pytest.raises(OrderError, match="insufficient_scope") as caught:
        book.send(preview["preview_id"])
    assert "Not a stock or ETF" not in str(caught.value)
    assert placed == []
    book.account = lambda: ("hash", "123")
    sent = book.send(preview["preview_id"])
    assert sent["placed"] is True and sent["order_id"] == 42
    assert placed[0]["session"] == "EXTO" and placed[0]["duration"] == "DAY"


def test_email_logs_when_smtp_is_unset_and_sms_is_a_placeholder(caplog, monkeypatch):
    monkeypatch.setenv("ORDER_ALERT_EMAIL", "trader@example.com")
    monkeypatch.setenv("ORDER_ALERT_PHONE", "555-0100")
    with caplog.at_level("INFO"):
        dispatch_order_alert("submit", "Buy 1 TEST at market")
    assert "SMTP unset" in caplog.text
    assert "SMS placeholder" in caplog.text
