import json
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
    bracket_order,
    build_quick,
    equity_order,
    normalize_ticket,
    quick_shares,
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
    assert tif_choices("limit") == ["day", "gtc", "am", "pm"]
    with pytest.raises(OrderError, match="not available"):
        normalize_ticket({"symbol": "TSLA", "order_type": "limit", "tif": "exto", "shares": 100, "price": 200})
    with pytest.raises(OrderError, match="not available"):
        normalize_ticket({"symbol": "TSLA", "order_type": "limit", "tif": "gtc_ext", "shares": 100, "price": 200})
    with pytest.raises(OrderError, match="Day only"):
        normalize_ticket({"symbol": "TSLA", "order_type": "market", "tif": "gtc", "shares": 100})
    limit = normalize_ticket({"symbol": "TSLA", "order_type": "limit", "tif": "gtc", "shares": 100, "price": 250})
    assert (limit.duration, limit.session) == ("GOOD_TILL_CANCEL", "NORMAL")


def test_limit_and_stop_orders_carry_the_schwab_session():
    built = equity_order(normalize_ticket({
        "symbol": "NVDA", "side": "sell", "order_type": "limit", "tif": "pm", "shares": 110, "price": 140.5,
    })).build()
    assert built["orderType"] == "LIMIT"
    assert built["session"] == "PM" and built["duration"] == "DAY"
    assert "EXTO" not in json.dumps(built)
    assert built["price"] == "140.50"
    assert built["orderLegCollection"][0]["instruction"] == "SELL"
    assert built["orderLegCollection"][0]["quantity"] == 110
    stop = equity_order(normalize_ticket({
        "symbol": "AMD", "order_type": "stop", "shares": 100, "price": 90,
    })).build()
    assert stop["orderType"] == "STOP" and stop["session"] == "NORMAL" and stop["stopPrice"] == "90.00"


def test_removed_tif_is_not_posted():
    for tif in ("ext", "gtc_ext", "exto", "gtc_exto"):
        with pytest.raises(OrderError, match="not available"):
            normalize_ticket({
                "symbol": "NVDA", "side": "buy", "order_type": "limit", "tif": tif, "shares": 100, "price": 180,
            })
    for tif, duration, session in (("day", "DAY", "NORMAL"), ("gtc", "GOOD_TILL_CANCEL", "NORMAL"), ("am", "DAY", "AM"), ("pm", "DAY", "PM")):
        built = equity_order(normalize_ticket({
            "symbol": "NVDA", "side": "buy", "order_type": "limit", "tif": tif, "shares": 100, "price": 180,
        })).build()
        assert built["duration"] == duration and built["session"] == session
        assert "EXTO" not in json.dumps(built) and built["session"] != "SEAMLESS"


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
    assert "Accounts and Trading Production" in message
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
        assert html.index('id="order-entry"') < html.index('id="quick-order"') < html.index('id="open-orders"')
        assert ">Quick order<" in html and 'id="quick-send"' in html
        assert html.index('id="order-entry"') < html.index('id="chart"')
        assert 'id="order-send"' in html and 'id="order-cancel"' in html
        assert 'id="open-orders"' in html and 'id="chart-exit"' in html and "chart-btn" in html
        assert "EXTO" not in html and "GTC EXT" not in html and '["ext"' not in html
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
        "symbol": "NVDA", "shares": 100, "order_type": "limit", "tif": "am", "price": 10,
    })
    assert preview["plan"]["session"] == "AM" and preview["placed"] is False
    with pytest.raises(OrderError, match="insufficient_scope") as caught:
        book.send(preview["preview_id"])
    assert "Not a stock or ETF" not in str(caught.value)
    assert placed == []
    book.account = lambda: ("hash", "123")
    sent = book.send(preview["preview_id"])
    assert sent["placed"] is True and sent["order_id"] == 42
    assert placed[0]["session"] == "AM" and placed[0]["duration"] == "DAY"
    assert "EXTO" not in json.dumps(placed[0])


def _schwab_response(status, body):
    class Response:
        status_code = status
        text = json.dumps(body)

        def json(self):
            return body

    return Response()


def test_cancel_payload_uses_the_trade_client_and_skips_filled_orders():
    calls = []

    class Client:
        def get_orders_for_account(self, account_hash, **_kwargs):
            calls.append(("orders", account_hash))
            return _schwab_response(200, [
                {
                    "orderId": 9,
                    "status": "WORKING",
                    "orderType": "LIMIT",
                    "price": 180.5,
                    "orderLegCollection": [{
                        "instruction": "BUY",
                        "quantity": 100,
                        "instrument": {"symbol": "NVDA", "assetType": "EQUITY"},
                    }],
                },
                {
                    "orderId": 10,
                    "status": "FILLED",
                    "orderType": "MARKET",
                    "orderLegCollection": [{
                        "instruction": "SELL",
                        "quantity": 5,
                        "instrument": {"symbol": "AMD", "assetType": "EQUITY"},
                    }],
                },
            ])

        def get_order(self, order_id, account_hash):
            calls.append(("get", order_id, account_hash))
            status = "FILLED" if order_id == 10 else "WORKING"
            return _schwab_response(200, {"orderId": order_id, "status": status})

        def cancel_order(self, order_id, account_hash):
            calls.append(("cancel", order_id, account_hash))
            return _schwab_response(200, {})

        def get_quotes(self, *_args, **_kwargs):
            raise AssertionError("pending orders must not use quotes")

    book = OrderBook(
        mode=lambda: "schwab",
        quote=lambda _symbol: {"lastPrice": 1},
        account=lambda: ("HASH", "1"),
        place=lambda *_args, **_kwargs: None,
        client=lambda: Client(),
    )
    listed = book.working_orders()
    assert listed["orders"] == [{
        "order_id": 9,
        "symbol": "NVDA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 100,
        "price": 180.5,
        "status": "WORKING",
    }]
    assert book.cancel(9) == {"canceled": True, "order_id": 9}
    assert ("cancel", 9, "HASH") in calls
    with pytest.raises(OrderError, match="filled"):
        book.cancel(10)
    assert ("cancel", 10, "HASH") not in calls


def test_missing_trade_token_and_demo_do_not_cancel_live_orders():
    from sources import TRADE_LOGIN_HINT

    missing = OrderBook(
        mode=lambda: "schwab",
        quote=lambda _symbol: {"lastPrice": 1},
        account=lambda: (_ for _ in ()).throw(AssertionError("no account call")),
        place=lambda *_args, **_kwargs: None,
        client=lambda: None,
    )
    with pytest.raises(OrderError, match="login-trade") as caught:
        missing.working_orders()
    assert "401" not in str(caught.value)
    assert TRADE_LOGIN_HINT in str(caught.value)

    def forbidden():
        raise AssertionError("demo must not call Schwab")

    demo = OrderBook(
        mode=lambda: "demo",
        quote=lambda _symbol: {"lastPrice": 1},
        account=forbidden,
        place=forbidden,
        client=forbidden,
    )
    assert demo.working_orders() == {"orders": [], "mode": "demo"}
    with pytest.raises(OrderError, match="does not cancel"):
        demo.cancel(9)


QUICK_QUOTE = {"lastPrice": "100", "lowPrice": "90", "highPrice": "110"}


def test_quick_share_math_and_dollar_risk_replaces_percent():
    from decimal import Decimal

    assert quick_shares(Decimal("100"), Decimal("100"), Decimal("89.90")) == 9
    assert quick_shares(Decimal("50"), Decimal("100"), Decimal("89.90")) == 4
    assert quick_shares(Decimal("33"), Decimal("100"), Decimal("89.90")) == 3
    assert quick_shares(Decimal("25"), Decimal("100"), Decimal("89.90")) == 2
    with pytest.raises(OrderError, match="Share size is 0"):
        quick_shares(Decimal("5"), Decimal("100"), Decimal("89.90"))
    percent = build_quick({"symbol": "NVDA", "side": "buy", "risk_percent": "1"}, QUICK_QUOTE, 10000)
    assert percent.shares == 9 and percent.risk == 100
    dollars = build_quick(
        {"symbol": "NVDA", "side": "buy", "risk_percent": "1", "risk_dollars": "20"},
        QUICK_QUOTE,
        10000,
    )
    assert dollars.shares == 1 and dollars.risk == 20
    half = build_quick({"symbol": "NVDA", "side": "buy", "risk_percent": "0.5"}, QUICK_QUOTE, 10000)
    assert half.shares == 4 and half.risk == 50


def test_default_stops_and_custom_stop_override():
    buy = build_quick({"symbol": "NVDA", "side": "buy", "risk_percent": "1"}, QUICK_QUOTE, 10000)
    sell = build_quick({"symbol": "NVDA", "side": "sell", "risk_percent": "1"}, QUICK_QUOTE, 10000)
    custom = build_quick(
        {"symbol": "NVDA", "side": "buy", "risk_percent": "1", "stop": "95"},
        QUICK_QUOTE,
        10000,
    )
    assert buy.stop == 89.9 and buy.entry == 100
    assert sell.stop == 110.1 and sell.entry == 100
    assert custom.stop == 95 and custom.shares == 20
    built = bracket_order(buy).build()
    assert built == {
        "session": "NORMAL",
        "duration": "DAY",
        "orderType": "MARKET",
        "orderLegCollection": [{
            "instruction": "BUY",
            "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
            "quantity": 9,
        }],
        "orderStrategyType": "TRIGGER",
        "childOrderStrategies": [{
            "session": "NORMAL",
            "duration": "DAY",
            "orderType": "STOP",
            "stopPrice": "89.90",
            "orderLegCollection": [{
                "instruction": "SELL",
                "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
                "quantity": 9,
            }],
            "orderStrategyType": "SINGLE",
        }],
    }
    assert "EXTO" not in json.dumps(built)
    sell_built = bracket_order(sell).build()
    assert sell_built["childOrderStrategies"][0]["orderLegCollection"][0]["instruction"] == "BUY"
    assert sell_built["childOrderStrategies"][0]["stopPrice"] == "110.10"
    gtc = build_quick({"symbol": "NVDA", "side": "buy", "tif": "gtc", "risk_percent": "1"}, QUICK_QUOTE, 10000)
    gtc_built = bracket_order(gtc).build()
    assert gtc_built["duration"] == "GOOD_TILL_CANCEL"
    assert gtc_built["childOrderStrategies"][0]["duration"] == "GOOD_TILL_CANCEL"
    assert gtc_built["session"] == "NORMAL"


def test_quick_send_refuses_when_last_is_through_the_stop_and_places_nothing():
    placed = []

    def place(*_args, **_kwargs):
        placed.append(True)
        raise AssertionError("refused order must not be placed")

    book = OrderBook(
        mode=lambda: "schwab",
        quote=lambda _symbol: {"lastPrice": "80", "lowPrice": "90", "highPrice": "110"},
        account=lambda: ("HASH", "1"),
        place=place,
        equity=lambda: 10000,
    )
    with pytest.raises(OrderError, match="through the stop"):
        book.quick_send({"symbol": "NVDA", "side": "buy", "risk_percent": "1"})
    with pytest.raises(OrderError, match="through the stop"):
        book.quick_send({"symbol": "NVDA", "side": "sell", "risk_percent": "1", "stop": "70"})
    with pytest.raises(OrderError, match="limit price"):
        book.quick_send({"symbol": "NVDA", "side": "buy", "order_type": "limit", "risk_percent": "1"})
    with pytest.raises(OrderError, match="ticker"):
        book.quick_send({"symbol": "", "risk_percent": "1"})
    assert placed == []

    def capture(account_hash, order):
        placed.append((account_hash, order.build()))

        class Response:
            status_code = 201
            headers = {"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH/orders/55"}

        return Response()

    live = OrderBook(
        mode=lambda: "schwab",
        quote=lambda _symbol: QUICK_QUOTE,
        account=lambda: ("HASH", "1"),
        place=capture,
        equity=lambda: 10000,
    )
    sent = live.quick_send({"symbol": "nvda", "side": "buy", "risk_percent": "1"})
    assert sent["placed"] is True and sent["plan"]["shares"] == 9 and sent["plan"]["stop"] == 89.9
    assert placed[0][0] == "HASH"
    assert placed[0][1]["orderStrategyType"] == "TRIGGER"
    assert placed[0][1]["childOrderStrategies"][0]["stopPrice"] == "89.90"
    assert "EXTO" not in json.dumps(placed[0][1])


def test_demo_quick_order_does_not_call_schwab():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("demo must not call Schwab")

    book = OrderBook(
        mode=lambda: "demo",
        quote=lambda _symbol: QUICK_QUOTE,
        account=forbidden,
        place=forbidden,
        client=forbidden,
        equity=forbidden,
    )
    result = book.quick_send({"symbol": "NVDA", "side": "buy", "risk_percent": "1"})
    assert result["placed"] is False and result["mode"] == "demo"
    assert "89.90" in result["summary"]


def test_email_logs_when_smtp_is_unset_and_sms_is_a_placeholder(caplog, monkeypatch):
    monkeypatch.setenv("ORDER_ALERT_EMAIL", "trader@example.com")
    monkeypatch.setenv("ORDER_ALERT_PHONE", "555-0100")
    with caplog.at_level("INFO"):
        dispatch_order_alert("submit", "Buy 1 TEST at market")
    assert "SMTP unset" in caplog.text
    assert "SMS placeholder" in caplog.text
