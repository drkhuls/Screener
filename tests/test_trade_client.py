import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import backend  # noqa: E402
import sources  # noqa: E402
from orders import OrderError  # noqa: E402


def _quote_source():
    class Quote:
        def get_quotes(self, symbols):
            return {symbol: {"lastPrice": 10, "lowPrice": 9} for symbol in symbols}

        def get_daily_history(self, _symbol, _days):
            return []

        def get_account_numbers(self):
            raise AssertionError("quote client must not read accounts")

        def place_order(self, *_args, **_kwargs):
            raise AssertionError("quote client must not place orders")

    return Quote()


def _response(status, body, headers=None):
    class Response:
        def __init__(self):
            self.status_code = status
            self.headers = headers or {}
            self.text = json.dumps(body)

        def json(self):
            return body

    return Response()


def _quote_token(tmp_path, monkeypatch):
    quote = tmp_path / "token.json"
    quote.write_text(json.dumps({"creation_timestamp": 1_700_000_000, "token": {"refresh_token": "qr"}}))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(quote))
    return quote


def test_orders_use_the_trade_client_and_quotes_do_not(monkeypatch, tmp_path):
    trade_calls = []

    class Trade:
        def get_quotes(self, symbols):
            trade_calls.append(("quotes", list(symbols)))
            raise AssertionError("trade client must not quote")

        def get_price_history_every_day(self, *_args, **_kwargs):
            trade_calls.append("history")
            raise AssertionError("trade client must not load history")

        def get_account_numbers(self):
            trade_calls.append("accounts")
            return _response(200, [{"hashValue": "HASH", "accountNumber": "1"}])

        def place_order(self, account_hash, _order):
            trade_calls.append(("place", account_hash))
            return _response(
                201, {}, {"Location": "https://api.schwabapi.com/trader/v1/accounts/x/orders/7"}
            )

    _quote_token(tmp_path, monkeypatch)
    trade_file = tmp_path / "token-trade.json"
    trade_file.write_text(json.dumps({"creation_timestamp": 1_700_000_000, "token": {"refresh_token": "keep"}}))
    monkeypatch.setenv("SCHWAB_TRADE_TOKEN_PATH", str(trade_file))
    monkeypatch.setenv("SCHWAB_TRADE_APP_KEY", "trade-key")
    monkeypatch.setenv("SCHWAB_TRADE_APP_SECRET", "trade-secret")
    monkeypatch.setattr(backend, "make_source", lambda *_args, **_kwargs: _quote_source())
    monkeypatch.setattr(backend, "load_trade_client", lambda: Trade())

    screener = backend.Screener(backend.load_config(), "schwab")
    screener.poll_quotes()
    preview = screener.orders.preview({"symbol": "NVDA", "shares": 100})
    assert preview["placed"] is False
    assert trade_calls == []
    sent = screener.orders.send(preview["preview_id"])
    assert sent["placed"] is True and sent["order_id"] == 7
    assert trade_calls == ["accounts", ("place", "HASH")]


def test_quote_source_opens_only_the_quote_token(monkeypatch, tmp_path):
    quote = tmp_path / "token.json"
    trade = tmp_path / "token-trade.json"
    quote.write_text(json.dumps({"creation_timestamp": 10, "token": {"refresh_token": "qr"}}))
    trade.write_text(json.dumps({"creation_timestamp": 20, "token": {"refresh_token": "tr"}}))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(quote))
    monkeypatch.setenv("SCHWAB_TRADE_TOKEN_PATH", str(trade))
    monkeypatch.setenv("SCHWAB_APP_KEY", "quote-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "quote-secret")
    monkeypatch.setenv("SCHWAB_TRADE_APP_KEY", "trade-key")
    opened = []

    class Client:
        def get_quotes(self, _symbols):
            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {}

            return Response()

    def open_client(path, key, _secret):
        opened.append((str(path), key))
        return Client()

    monkeypatch.setattr(sources, "open_client", open_client)
    source = sources.SchwabSource()
    assert source.get_quotes(["NVDA"]) == {}
    assert opened == [(str(quote), "quote-key")]


def test_missing_trade_token_message_and_quote_poll_still_runs(monkeypatch, tmp_path):
    _quote_token(tmp_path, monkeypatch)
    trade = tmp_path / "token-trade.json"
    trade.write_text(json.dumps({"creation_timestamp": 20, "token": {"access_token": "ta"}}))
    monkeypatch.setenv("SCHWAB_TRADE_TOKEN_PATH", str(trade))
    monkeypatch.setenv("SCHWAB_TRADE_APP_KEY", "trade-key")
    monkeypatch.setenv("SCHWAB_TRADE_APP_SECRET", "trade-secret")
    monkeypatch.setattr(backend, "make_source", lambda *_args, **_kwargs: _quote_source())

    screener = backend.Screener(backend.load_config(), "schwab")
    assert screener.trade_client is None
    preview = screener.orders.preview({"symbol": "NVDA", "shares": 100})
    with pytest.raises(OrderError, match="login-trade") as caught:
        screener.orders.send(preview["preview_id"])
    assert "401" not in str(caught.value)
    screener.poll_quotes()
    assert screener.quotes["NVDA"]["lastPrice"] == 10


def test_trade_token_refresh_does_not_clobber_the_quote_token(tmp_path, monkeypatch):
    quote = tmp_path / "token.json"
    trade = tmp_path / "token-trade.json"
    quote.write_text(json.dumps({"creation_timestamp": 10, "token": {"access_token": "qa", "refresh_token": "qr"}}))
    trade.write_text(json.dumps({"creation_timestamp": 20, "token": {"access_token": "ta", "refresh_token": "tr"}}))
    quote_bytes = quote.read_bytes()
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(quote))
    monkeypatch.setenv("SCHWAB_TRADE_TOKEN_PATH", str(trade))

    sources.token_writer(sources.trade_token_path())(
        {"creation_timestamp": 20, "token": {"access_token": "ta-new", "token_type": "Bearer"}}
    )

    assert quote.read_bytes() == quote_bytes
    saved = json.loads(trade.read_text())
    assert saved["creation_timestamp"] == 20
    assert saved["token"]["access_token"] == "ta-new"
    assert saved["token"]["refresh_token"] == "tr"
    assert stat.S_IMODE(trade.stat().st_mode) == 0o600


def test_new_trade_token_reloads_and_leaves_the_quote_client(monkeypatch, tmp_path):
    trade = tmp_path / "token-trade.json"

    def write(stamp):
        trade.write_text(json.dumps({"creation_timestamp": stamp, "token": {"refresh_token": "tr"}}))

    write(100)
    loaded = []

    def load():
        client = object()
        loaded.append(client)
        return client

    _quote_token(tmp_path, monkeypatch)
    monkeypatch.setenv("SCHWAB_TRADE_TOKEN_PATH", str(trade))
    monkeypatch.setenv("SCHWAB_TRADE_APP_KEY", "trade-key")
    monkeypatch.setenv("SCHWAB_TRADE_APP_SECRET", "trade-secret")
    monkeypatch.setattr(backend, "make_source", lambda *_args, **_kwargs: _quote_source())
    monkeypatch.setattr(backend, "load_trade_client", load)

    screener = backend.Screener(backend.load_config(), "schwab")
    quote_source = screener.source
    first = screener.trade_client
    screener.ensure_trade()
    assert screener.trade_client is first and len(loaded) == 1
    write(200)
    screener.ensure_trade()
    assert screener.trade_client is loaded[1]
    assert screener.source is quote_source


def test_login_trade_refuses_without_keys_and_leaves_the_quote_token(monkeypatch, tmp_path):
    quote = tmp_path / "token.json"
    quote.write_text("quote-token")
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(quote))
    monkeypatch.delenv("SCHWAB_TRADE_APP_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_TRADE_APP_SECRET", raising=False)
    monkeypatch.delenv("SCHWAB_TRADE_TOKEN_PATH", raising=False)
    with pytest.raises(SystemExit, match="SCHWAB_TRADE_APP_KEY"):
        backend.login_trade(manual=True)
    assert quote.read_text() == "quote-token"
    assert not (tmp_path / "token-trade.json").exists()


def test_trade_token_defaults_beside_the_quote_token(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.delenv("SCHWAB_TRADE_TOKEN_PATH", raising=False)
    monkeypatch.delenv("SCHWAB_TRADE_CALLBACK_URL", raising=False)
    assert sources.trade_token_path() == tmp_path / "token-trade.json"
    assert sources.trade_callback_url() == "https://127.0.0.1:8182"


def test_env_example_is_blank():
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert "SCHWAB_TRADE_APP_KEY=" in text
    assert "SCHWAB_TRADE_APP_SECRET=" in text
    assert "SCHWAB_TRADE_CALLBACK_URL=" in text
    assert "SCHWAB_TRADE_TOKEN_PATH=" in text
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            assert line.split("=", 1)[1].strip() == ""
