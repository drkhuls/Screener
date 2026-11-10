import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from orders import (  # noqa: E402
    OrderError,
    OrderParseError,
    ShareSizeError,
    OrderBook,
    build_plan,
    linked_order,
    parse_instruction,
    read_equity,
    share_size,
    stop_event,
    summary,
)
from alerts import dispatch_order_alert  # noqa: E402


@pytest.fixture(autouse=True)
def _no_alert_destinations(monkeypatch):
    monkeypatch.delenv("ORDER_ALERT_EMAIL", raising=False)
    monkeypatch.delenv("ORDER_ALERT_PHONE", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)


NVDA = {"lastPrice": 140, "lowPrice": 135, "highPrice": 142}


def book_with(quote, equity=250_000, mode="schwab"):
    queued = list(quote) if isinstance(quote, list) else None

    def get_quote(_symbol):
        if queued is not None:
            return queued.pop(0)
        return quote

    placed = []

    def place(account_hash, order):
        placed.append((account_hash, order.build()))

        class Response:
            status_code = 201
            headers = {"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH/orders/42"}
            text = ""

        return Response()

    book = OrderBook(
        mode=lambda: mode,
        quote=get_quote,
        equity=lambda: ("HASH", "999", equity),
        place=place,
        client=lambda: None,
    )
    book.placed = placed
    return book


def test_default_ticker_and_buy_without_size_parse_as_market_buys():
    for text in ("NVDA", "buy NVDA", "  buy   nvda  "):
        parsed = parse_instruction(text)
        assert parsed.side == "buy"
        assert parsed.symbol == "NVDA"
        assert parsed.quantity is None
        assert parsed.entry == "market"
        assert parsed.limit_price is None
        assert parsed.risk_pct is None
        assert parsed.stop_price is None


def test_examples_parse_side_size_limit_and_risk():
    buy = parse_instruction("buy 100 TSLA at market with 0.5% equity risk stop")
    assert (buy.side, buy.quantity, buy.symbol, buy.entry, buy.risk_pct, buy.stop_price) == (
        "buy", 100, "TSLA", "market", 0.5, None,
    )
    sell = parse_instruction("sell 50 AAPL limit 185 with 1% stop")
    assert (sell.side, sell.quantity, sell.symbol, sell.entry, sell.limit_price, sell.risk_pct) == (
        "sell", 50, "AAPL", "limit", 185.0, 1.0,
    )
    assert sell.stop_price is None


def test_explicit_stop_and_word_tickers():
    assert parse_instruction("buy 10 SPY stop 500").stop_price == 500
    assert parse_instruction("buy LOW").symbol == "LOW"
    assert parse_instruction("buy NVDA low of day").symbol == "NVDA"
    assert parse_instruction("buy 100 shares of BRK.B at market").symbol == "BRK.B"


@pytest.mark.parametrize("text", [
    "buy 1 AAPL 190 call",
    "buy AAPL 190C",
    "buy /ES",
    "sell NQ futures",
    "buy AAPL240119C00190000",
    "buy 1 put",
])
def test_rejects_options_and_futures(text):
    with pytest.raises(OrderParseError, match="Stocks and ETFs only"):
        parse_instruction(text)


def test_share_size_uses_equity_times_risk_over_last_minus_low():
    # 250_000 * 0.5% = 1_250; 140 - 135 = 5; 1250 / 5 = 250
    assert share_size(250_000, 0.5, 140, 135) == 250
    # 1250 / 3.5 = 357.142... floors to 357 so the order does not exceed the budget
    assert share_size(250_000, 0.5, 180, 176.5) == 357


def test_share_size_rejects_last_at_or_below_the_low():
    with pytest.raises(ShareSizeError, match="at or below"):
        share_size(250_000, 0.5, 100, 100)
    with pytest.raises(ShareSizeError, match="at or below"):
        share_size(250_000, 0.5, 99.5, 100)


def test_share_size_rejects_a_budget_under_one_share():
    with pytest.raises(ShareSizeError, match="less than 1 share"):
        share_size(100, 0.5, 50, 40)


def test_default_buy_nvda_is_sized_from_equity_risk_and_the_low():
    preview = book_with(NVDA).preview("buy NVDA", None)
    assert preview["plan"]["shares"] == 250
    assert preview["plan"]["sized_from_risk"] is True
    assert preview["plan"]["entry"] == "market"
    assert preview["plan"]["stop_source"] == "low_of_day"
    assert preview["plan"]["stop_price"] == 135
    assert preview["placed"] is False
    assert preview["summary"] == (
        "Buy 250 NVDA at market. Account equity: $250,000. Risk: 0.5% = $1,250. "
        "Stop-loss: stop-market at $135.00. Send this order?"
    )


def test_explicit_size_keeps_the_share_count_and_the_confirmation_sentence():
    quote = {"lastPrice": 250, "lowPrice": 242.5, "highPrice": 255}
    preview = book_with(quote).preview("buy 100 TSLA at market with 0.5% equity risk stop", 1)
    assert preview["plan"]["shares"] == 100
    assert preview["plan"]["sized_from_risk"] is False
    assert preview["plan"]["risk_pct"] == 0.5  # the instruction overrides the 1% button
    assert preview["summary"] == (
        "Buy 100 TSLA at market. Account equity: $250,000. Risk: 0.5% = $1,250. "
        "Stop-loss: stop-market at $242.50. Send this order?"
    )


def test_preview_does_not_place_and_confirm_without_a_preview_places_nothing():
    book = book_with(NVDA)
    book.preview("buy NVDA", 0.5)
    assert book.placed == []
    with pytest.raises(OrderError, match="No order was sent"):
        book.confirm("missing")
    assert book.placed == []


def test_confirm_sizes_from_the_last_at_confirm_time():
    first = {"lastPrice": 140, "lowPrice": 135, "highPrice": 141}
    later = {"lastPrice": 150, "lowPrice": 135, "highPrice": 151}
    book = book_with([first, later, dict(later)])
    preview = book.preview("NVDA", 0.5)
    assert preview["plan"]["shares"] == 250  # 1250 / (140 - 135)

    moved = book.confirm(preview["preview_id"])
    assert moved["placed"] is False and moved["repriced"] is True
    assert moved["plan"]["shares"] == 83  # floor(1250 / (150 - 135)) = floor(83.333)
    assert book.placed == []

    sent = book.confirm(moved["preview_id"])
    assert sent["placed"] is True
    assert sent["order_id"] == 42
    assert sent["route"] == "trigger"
    spec = book.placed[0][1]
    assert spec["orderStrategyType"] == "TRIGGER"
    assert spec["orderType"] == "MARKET"
    assert spec["session"] == "NORMAL"
    assert spec["duration"] == "DAY"
    assert spec["orderLegCollection"][0]["instruction"] == "BUY"
    assert spec["orderLegCollection"][0]["quantity"] == 83
    assert spec["orderLegCollection"][0]["instrument"]["assetType"] == "EQUITY"
    child = spec["childOrderStrategies"][0]
    assert child["orderType"] == "STOP"
    assert child["stopPrice"] == "135.00"
    assert child["orderLegCollection"][0]["instruction"] == "SELL"
    assert child["orderLegCollection"][0]["quantity"] == 83


def test_demo_confirm_shows_the_order_and_does_not_place():
    book = book_with(NVDA, mode="demo")
    preview = book.preview("buy NVDA", 0.5)
    result = book.confirm(preview["preview_id"])
    assert result["placed"] is False
    assert result["mode"] == "demo"
    assert result["alert"]["kind"] == "submit"
    assert "not sent" in result["alert"]["message"]
    assert "Buy 250 NVDA" in result["alert"]["message"]
    assert book.placed == []


def test_sell_limit_is_a_trigger_with_a_buy_to_cover_stop():
    parsed = parse_instruction("sell 50 AAPL limit 185 with 1% stop")
    plan = build_plan(
        parsed,
        risk_pct=parsed.risk_pct,
        equity=250_000,
        quote={"lastPrice": 180, "lowPrice": 170, "highPrice": 190},
    )
    assert plan.shares == 50
    assert plan.stop_source == "high_of_day"
    assert plan.stop_price == 190
    assert summary(plan).startswith("Sell short 50 AAPL at limit $185.00.")
    spec = linked_order(plan).build()
    assert spec["orderStrategyType"] == "TRIGGER"
    assert spec["orderType"] == "LIMIT"
    assert spec["price"] == "185.00"
    assert spec["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    child = spec["childOrderStrategies"][0]
    assert child["orderType"] == "STOP"
    assert child["stopPrice"] == "190.00"
    assert child["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"


def test_unsized_sell_uses_the_high_of_day():
    # 1250 / (150 - 140) = 125
    preview = book_with({"lastPrice": 140, "lowPrice": 130, "highPrice": 150}).preview("sell NVDA", 0.5)
    assert preview["plan"]["shares"] == 125
    assert preview["plan"]["stop_price"] == 150
    assert preview["summary"].startswith("Sell short 125 NVDA at market.")


def test_read_equity_prefers_equity_then_liquidation_value():
    class Response:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def json(self):
            return self._payload

    class Client:
        def get_account_numbers(self):
            return Response([{"accountNumber": "12345678", "hashValue": "abc"}])

        def get_account(self, account_hash):
            assert account_hash == "abc"
            return Response({"securitiesAccount": {"currentBalances": {"equity": 0, "liquidationValue": 80_000}}})

    account_hash, number, equity = read_equity(Client())
    assert (account_hash, number, equity) == ("abc", "12345678", 80_000)

    class EquityClient(Client):
        def get_account(self, account_hash):
            return Response({"securitiesAccount": {"currentBalances": {"equity": 250_000, "liquidationValue": 1}}})

    assert read_equity(EquityClient())[2] == 250_000


def test_stop_event_waits_until_the_stop_fills_and_alerts_once():
    assert stop_event({"status": "WORKING", "childOrderStrategies": [{"orderType": "STOP", "status": "WORKING"}]}) is None
    assert stop_event({"status": "FILLED", "childOrderStrategies": [{"orderType": "STOP", "status": "WORKING"}]}) is None
    filled = {"status": "FILLED", "childOrderStrategies": [{"orderType": "STOP", "status": "FILLED"}]}
    assert stop_event(filled) == "filled"
    assert stop_event({"status": "REJECTED"}) == "rejected"

    state = {"body": {"status": "FILLED", "childOrderStrategies": [{"orderType": "STOP", "status": "WORKING"}]}}

    class Client:
        def get_order(self, order_id, account_hash):
            assert (order_id, account_hash) == (42, "HASH")

            class Response:
                status_code = 200

                def json(_self):
                    return state["body"]

            return Response()

    book = book_with(NVDA)
    book.client = lambda: Client()
    book.watched.append({
        "order_id": 42,
        "account_hash": "HASH",
        "done": False,
        "fill_message": "Stop filled: NVDA stop-market at $135.00 (250 shares).",
        "reject_message": "Schwab rejected the NVDA order.",
    })
    book.poll_stops()
    assert book.drain_alerts() == []
    state["body"] = filled
    book.poll_stops()
    alerts = book.drain_alerts()
    assert alerts == [{"kind": "stop_fill", "message": "Stop filled: NVDA stop-market at $135.00 (250 shares)."}]
    book.poll_stops()
    assert book.drain_alerts() == []


def test_demo_confirm_shows_the_panel_and_does_not_place(monkeypatch):
    from fastapi.testclient import TestClient

    import backend

    monkeypatch.setenv("SCREENER_MODE", "demo")

    def stable_quotes(_self, symbols):
        return {
            symbol: {"lastPrice": 140, "lowPrice": 135, "highPrice": 142, "openPrice": 136}
            for symbol in symbols
        }

    monkeypatch.setattr("sources.DemoSource.get_quotes", stable_quotes)
    with TestClient(backend.app) as client:
        html = client.get("/").text
        table = html.index('<section class="box" id="table-panel">')
        order = html.index('<section class="box" id="order-entry">')
        chart = html.index('<section class="box chart-box">')
        assert table < order < chart
        preview = client.post("/api/orders/preview", json={"instruction": "buy NVDA", "risk_pct": 0.5})
        assert preview.status_code == 200
        body = preview.json()
        assert body["placed"] is False
        assert body["plan"]["shares"] == 250
        confirmed = client.post("/api/orders/confirm", json={"preview_id": body["preview_id"]})
        assert confirmed.status_code == 200
        payload = confirmed.json()
        assert payload["placed"] is False and payload["mode"] == "demo"
        assert "Demo mode: order not sent" in payload["alert"]["message"]


def test_email_logs_when_smtp_is_unset_and_sms_is_a_placeholder(caplog, monkeypatch):
    monkeypatch.setenv("ORDER_ALERT_EMAIL", "trader@example.com")
    monkeypatch.setenv("ORDER_ALERT_PHONE", "555-0100")
    with caplog.at_level("INFO"):
        dispatch_order_alert("submit", "Buy 1 TEST at market")
    assert "SMTP unset" in caplog.text
    assert "trader@example.com" in caplog.text
    assert "SMS placeholder to 555-0100" in caplog.text
    assert "Buy 1 TEST at market" in caplog.text
