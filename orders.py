"""Order text, share sizing, and a confirm-then-send Schwab trigger order.

A ticker, or "buy NVDA" with no share count, is a market buy with a stop-market
at the low of day. Shares are::

    floor(account_equity * risk% / (last - low_of_day))

The last price used for that division is the quote read when Confirm is accepted.
If that quote changes the share count, the stop, or the equity shown in the
dialog, the dialog is updated and nothing is sent.

Schwab path: one TRIGGER order from ``schwab.orders.common.first_triggers_second``.
The entry (market or limit) is the parent. The child is ``OrderType.STOP``, which
schwab-py defines as stop-market: when the stop is touched, Schwab submits a
market order. OCO is the wrong shape here — an OCO rests both orders at once, and
the protective stop must not rest before the entry fills. A failed trigger is
surfaced as an error. The entry is not sent on its own.

Sells are sell-short, with a buy-to-cover stop-market. With no share count the
stop is the high of day and the distance is ``high - last``.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable

from alerts import dispatch_order_alert

log = logging.getLogger("screener.orders")

DEMO_EQUITY = 250_000.0
PREVIEW_TTL = 15 * 60
ORDER_ROUTE = "trigger"
ORDER_ROUTE_NOTE = (
    "One Schwab TRIGGER order (schwab.orders.common.first_triggers_second): "
    "the entry is the parent and a stop-market child (OrderType.STOP) is held "
    "until the entry fills. OCO is not used, because an OCO would rest the stop "
    "before the entry exists. There is no second unlinked order."
)

# Instruction words. A few of these are also real tickers (A, ON, LOW, FOR, AT);
# those are dropped only when another symbol is present.
_ALWAYS = {
    "buy", "sell", "short", "long", "market", "mkt", "limit", "lmt", "stop",
    "with", "equity", "risk", "shares", "share", "percent", "pct", "lod", "hod",
    "stoploss", "stopmarket", "cover",
}
_CONTEXTUAL = {
    "a", "an", "the", "of", "day", "low", "high", "for", "and", "on", "to",
    "at", "price", "last", "order", "in", "from", "by",
}
_EQUITY = re.compile(r"^[A-Z]{1,5}(?:[./][A-Z]+)?$")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)")
_LIMIT_PRICE = re.compile(r"\b(?:limit|lmt)\s+\$?\s*(\d+(?:\.\d+)?)")
_AT_PRICE = re.compile(r"\bat\s+\$?\s*(\d+(?:\.\d+)?)")
_STOP_PRICE = re.compile(
    r"\bstop(?:\s*-\s*market|\s+market)?(?:\s+loss)?(?:\s+at)?\s+\$?\s*(\d+(?:\.\d+)?)\b"
)


class OrderError(Exception):
    pass


class OrderParseError(OrderError):
    pass


class ShareSizeError(OrderError):
    pass


@dataclass(frozen=True)
class ParsedOrder:
    side: str
    symbol: str
    quantity: int | None
    entry: str
    limit_price: float | None
    risk_pct: float | None
    stop_price: float | None


@dataclass
class OrderPlan:
    side: str
    symbol: str
    shares: int
    sized_from_risk: bool
    entry: str
    limit_price: float | None
    stop_price: float
    stop_source: str
    last: float
    equity: float
    risk_pct: float
    risk_dollars: float
    account_hash: str = ""
    account_number: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "symbol": self.symbol,
            "shares": self.shares,
            "sized_from_risk": self.sized_from_risk,
            "entry": self.entry,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "stop_source": self.stop_source,
            "last": self.last,
            "equity": self.equity,
            "risk_pct": self.risk_pct,
            "risk_dollars": self.risk_dollars,
        }


def cents(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise OrderError("Price is not a number") from exc
    if not number.is_finite():
        raise OrderError("Price is not a number")
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def risk_budget(equity: float, risk_pct: float) -> Decimal:
    return Decimal(str(equity)) * Decimal(str(risk_pct)) / Decimal(100)


def coerce_risk(value: Any) -> float:
    """Preset buttons send 0.25, 0.33, 0.5, or 1. Missing means 0.5%."""
    if isinstance(value, bool):
        raise OrderError("Risk percent is not a number")
    if value is None or value == "":
        return 0.5
    try:
        risk = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderError("Risk percent is not a number") from exc
    if not (0 < risk < 100):
        raise OrderError("Risk percent must be greater than 0 and less than 100")
    return float(Decimal(str(risk)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def share_size(equity: float, risk_pct: float, last: float, low: float, *, side: str = "buy") -> int:
    """Whole shares: floor(equity * risk% / distance).

    For a buy, ``low`` is the stop (the low of day by default) and the distance
    is ``last - low``. Distance of zero or less is rejected before dividing.
    """
    if equity <= 0:
        raise ShareSizeError("Account equity must be positive")
    if risk_pct <= 0:
        raise ShareSizeError("Risk percent must be positive")
    last_c = cents(last)
    stop_c = cents(low)
    distance = last_c - stop_c if side == "buy" else stop_c - last_c
    if distance <= 0:
        if side == "buy":
            raise ShareSizeError("Cannot size shares: last price is at or below the low of day")
        raise ShareSizeError("Cannot size shares: last price is at or above the stop")
    shares = int((risk_budget(equity, risk_pct) / distance).to_integral_value(rounding=ROUND_DOWN))
    if shares < 1:
        raise ShareSizeError("Risk budget covers less than 1 share at this stop distance")
    return shares


def fmt_pct(risk: float) -> str:
    return f"{risk:.2f}".rstrip("0").rstrip(".")


def fmt_money(amount: Decimal | float) -> str:
    quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return f"${quantized:,.0f}"
    return f"${quantized:,.2f}"


def summary(plan: OrderPlan) -> str:
    verb = "Buy" if plan.side == "buy" else "Sell short"
    how = "at market" if plan.entry == "market" else f"at limit ${plan.limit_price:,.2f}"
    return (
        f"{verb} {plan.shares} {plan.symbol} {how}. "
        f"Account equity: {fmt_money(plan.equity)}. "
        f"Risk: {fmt_pct(plan.risk_pct)}% = {fmt_money(plan.risk_dollars)}. "
        f"Stop-loss: stop-market at ${plan.stop_price:,.2f}. "
        "Send this order?"
    )


def sentence(plan: OrderPlan) -> str:
    return summary(plan).removesuffix(" Send this order?")


def comparable(plan: OrderPlan) -> tuple:
    limit = None if plan.limit_price is None else f"{plan.limit_price:.2f}"
    return (
        plan.side,
        plan.symbol,
        plan.shares,
        plan.entry,
        limit,
        f"{plan.stop_price:.2f}",
        f"{plan.equity:.2f}",
        f"{plan.risk_pct:.2f}",
        f"{plan.risk_dollars:.2f}",
    )


def _reject_derivatives(raw: str) -> None:
    if re.search(r"\b(options?|calls?|puts?|futures?|strikes?)\b", raw, re.I):
        raise OrderParseError("Stocks and ETFs only. Options and futures are rejected.")
    upper = raw.upper()
    if re.search(r"(^|\s)/[A-Z0-9]{1,6}\b", upper):
        raise OrderParseError("Stocks and ETFs only. Futures are rejected.")
    if re.search(r"\b[A-Z]{1,6}\d{6}[CP]\d{8}\b", upper):
        raise OrderParseError("Stocks and ETFs only. Options are rejected.")
    if re.search(r"\b\d+(?:\.\d+)?[CP]\b", upper):
        raise OrderParseError("Stocks and ETFs only. Options are rejected.")


def _percent(lower: str) -> float | None:
    found = _PERCENT.findall(lower)
    if not found:
        return None
    if len(found) > 1:
        raise OrderParseError("Give one risk percent")
    return coerce_risk(found[0])


def _side(lower: str) -> str:
    buy = re.search(r"\b(buy|long)\b", lower)
    sell = re.search(r"\b(sell|short)\b", lower)
    if buy and sell:
        raise OrderParseError("Say either buy or sell")
    return "sell" if sell else "buy"


def _entry(lower: str) -> tuple[str, float | None]:
    without_stop = _STOP_PRICE.sub(" ", lower)
    limit = _LIMIT_PRICE.search(without_stop)
    at_price = _AT_PRICE.search(without_stop)
    says_market = re.search(r"\b(?:market|mkt)\b", without_stop)
    if limit and (at_price or says_market):
        raise OrderParseError("Say either market or a limit price")
    if at_price and says_market:
        raise OrderParseError("Say either market or a limit price")
    if limit:
        return "limit", float(limit.group(1))
    if at_price:
        return "limit", float(at_price.group(1))
    return "market", None


def _stop_price(lower: str) -> float | None:
    found = _STOP_PRICE.findall(lower)
    if len(found) > 1:
        raise OrderParseError("Give one stop price")
    return float(found[0]) if found else None


def _scrub(lower: str) -> str:
    text = _PERCENT.sub(" ", lower)
    text = _LIMIT_PRICE.sub(" ", text)
    text = _STOP_PRICE.sub(" ", text)
    text = _AT_PRICE.sub(" ", text)
    return text


def _symbol_and_shares(scrubbed: str) -> tuple[str, int | None]:
    if re.search(r"\d+\.\d+", scrubbed):
        raise OrderParseError("Share count must be a whole number")
    words: list[str] = []
    shares: list[int] = []
    for tok in re.findall(r"[a-z]+(?:[./][a-z]+)?|\d+", scrubbed):
        if tok.isdigit():
            shares.append(int(tok))
            continue
        if tok in _ALWAYS:
            continue
        symbol = tok.upper()
        if not _EQUITY.fullmatch(symbol):
            raise OrderParseError(f"Not a stock or ETF: {symbol}")
        words.append(symbol)
    if len(shares) > 1:
        raise OrderParseError("Give one share count")
    quantity = shares[0] if shares else None
    if quantity == 0:
        raise OrderParseError("Share count must be at least 1")
    real = [word for word in words if word.lower() not in _CONTEXTUAL]
    symbols = real or words
    if not symbols:
        raise OrderParseError("Name a stock or ETF. For example: buy NVDA")
    if len(symbols) > 1:
        raise OrderParseError("Name one stock or ETF")
    return symbols[0], quantity


def parse_instruction(text: str) -> ParsedOrder:
    raw = " ".join(str(text or "").strip().split())
    if not raw:
        raise OrderParseError("Enter an order. For example: buy NVDA")
    if len(raw) > 500:
        raise OrderParseError("Order text is too long")
    _reject_derivatives(raw)
    lower = raw.lower()
    risk = _percent(lower)
    side = _side(lower)
    entry, limit_price = _entry(lower)
    stop = _stop_price(lower)
    symbol, quantity = _symbol_and_shares(_scrub(lower))
    if limit_price is not None and limit_price <= 0:
        raise OrderParseError("Limit price must be positive")
    if stop is not None and stop <= 0:
        raise OrderParseError("Stop price must be positive")
    return ParsedOrder(side, symbol, quantity, entry, limit_price, risk, stop)


def build_plan(parsed: ParsedOrder, *, risk_pct: float, equity: float, quote: dict[str, Any]) -> OrderPlan:
    if equity <= 0:
        raise OrderError("Account equity must be positive")
    if quote.get("lastPrice") is None:
        raise OrderError(f"No last price for {parsed.symbol}")
    last = cents(quote["lastPrice"])
    low = cents(quote["lowPrice"]) if quote.get("lowPrice") not in (None, 0, 0.0) else None
    high = cents(quote["highPrice"]) if quote.get("highPrice") not in (None, 0, 0.0) else None
    if parsed.stop_price is not None:
        stop = cents(parsed.stop_price)
        source = "explicit"
    elif parsed.side == "buy":
        if low is None:
            raise OrderError(f"No low of day for {parsed.symbol}")
        stop, source = low, "low_of_day"
    else:
        if high is None:
            raise OrderError(f"No high of day for {parsed.symbol}")
        stop, source = high, "high_of_day"
    if parsed.side == "buy" and last <= stop:
        if source == "low_of_day":
            raise ShareSizeError("Cannot size shares: last price is at or below the low of day")
        raise OrderError("Stop must be below the last price")
    if parsed.side == "sell" and last >= stop:
        if source == "high_of_day":
            raise ShareSizeError("Cannot size shares: last price is at or above the high of day")
        raise OrderError("Stop must be above the last price")
    budget = risk_budget(equity, risk_pct)
    if parsed.quantity is None:
        shares = share_size(equity, risk_pct, float(last), float(stop), side=parsed.side)
        sized = True
    else:
        shares = parsed.quantity
        sized = False
    limit = float(cents(parsed.limit_price)) if parsed.limit_price is not None else None
    return OrderPlan(
        side=parsed.side,
        symbol=parsed.symbol,
        shares=shares,
        sized_from_risk=sized,
        entry=parsed.entry,
        limit_price=limit,
        stop_price=float(stop),
        stop_source=source,
        last=float(last),
        equity=float(equity),
        risk_pct=float(risk_pct),
        risk_dollars=float(budget.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
    )


def linked_order(plan: OrderPlan):
    """Build the trigger order with schwab-py. Caller passes it to ``place_order``."""
    from schwab.orders.common import (
        Duration,
        EquityInstruction,
        OrderStrategyType,
        OrderType,
        Session,
        first_triggers_second,
    )
    from schwab.orders.equities import (
        equity_buy_limit,
        equity_buy_market,
        equity_sell_short_limit,
        equity_sell_short_market,
    )
    from schwab.orders.generic import OrderBuilder

    symbol = plan.symbol
    shares = int(plan.shares)
    if plan.side == "buy":
        entry = (
            equity_buy_limit(symbol, shares, f"{plan.limit_price:.2f}")
            if plan.entry == "limit"
            else equity_buy_market(symbol, shares)
        )
        cover = EquityInstruction.SELL
    else:
        entry = (
            equity_sell_short_limit(symbol, shares, f"{plan.limit_price:.2f}")
            if plan.entry == "limit"
            else equity_sell_short_market(symbol, shares)
        )
        cover = EquityInstruction.BUY_TO_COVER
    stop = (
        OrderBuilder()
        .set_order_type(OrderType.STOP)
        .set_stop_price(f"{plan.stop_price:.2f}")
        .set_session(Session.NORMAL)
        .set_duration(Duration.DAY)
        .set_order_strategy_type(OrderStrategyType.SINGLE)
        .add_equity_leg(cover, symbol, shares)
    )
    return first_triggers_second(entry, stop)


def order_id_from_location(headers: Any) -> int | None:
    if not headers:
        return None
    location = headers.get("Location") or headers.get("location") or ""
    match = re.search(r"/orders/(\d+)", str(location))
    return int(match.group(1)) if match else None


def read_equity(client: Any) -> tuple[str, str, float]:
    """First linked Schwab account. Prefer currentBalances.equity, else liquidation value."""
    numbers = client.get_account_numbers()
    _check(numbers, "Schwab accounts")
    accounts = numbers.json()
    if not isinstance(accounts, list) or not accounts:
        raise OrderError("No Schwab account on this login")
    chosen = accounts[0]
    account_hash = chosen.get("hashValue")
    account_number = str(chosen.get("accountNumber") or "")
    if not account_hash:
        raise OrderError("Schwab account hash is missing")
    acct = client.get_account(account_hash)
    _check(acct, "Schwab account")
    equity = _equity_value(_balances(acct.json()))
    return str(account_hash), account_number, equity


def _balances(body: Any) -> dict[str, Any]:
    sec = body.get("securitiesAccount") if isinstance(body, dict) else None
    if isinstance(sec, dict) and isinstance(sec.get("currentBalances"), dict):
        return sec["currentBalances"]
    if isinstance(body, dict) and isinstance(body.get("currentBalances"), dict):
        return body["currentBalances"]
    raise OrderError("Schwab account did not include balances")


def _equity_value(balances: dict[str, Any]) -> float:
    for key in ("equity", "liquidationValue"):
        raw = balances.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    raise OrderError("Schwab account equity is not available")


def _check(response: Any, what: str) -> None:
    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise OrderError(f"{what} failed ({status})")


def stop_event(order: dict[str, Any]) -> str | None:
    """``filled`` when the stop-market child fills, ``rejected`` on a reject, else wait."""
    if not isinstance(order, dict):
        return None
    stops = [item for item in _walk(order) if item.get("orderType") == "STOP"]
    if any(item.get("status") == "FILLED" for item in stops):
        return "filled"
    if any(item.get("status") == "REJECTED" for item in stops) or order.get("status") == "REJECTED":
        return "rejected"
    return None


def _walk(order: dict[str, Any]):
    yield order
    for child in order.get("childOrderStrategies") or []:
        if isinstance(child, dict):
            yield from _walk(child)


class OrderBook:
    def __init__(
        self,
        *,
        mode: Callable[[], str],
        quote: Callable[[str], dict[str, Any]],
        equity: Callable[[], tuple[str, str, float]],
        place: Callable[[str, Any], Any],
        client: Callable[[], Any] | None = None,
    ):
        self.mode = mode
        self.quote = quote
        self.equity = equity
        self.place = place
        self.client = client or (lambda: None)
        self.lock = threading.Lock()
        self.previews: dict[str, dict[str, Any]] = {}
        self.pending: list[dict[str, str]] = []
        self.watched: list[dict[str, Any]] = []

    @classmethod
    def for_screener(cls, screener: Any) -> "OrderBook":
        def quote(symbol: str) -> dict[str, Any]:
            source = screener.source
            if source is None:
                raise OrderError(screener.errors.get("schwab") or "Schwab is not connected")
            try:
                found = source.get_quotes([symbol])
            except Exception as exc:
                raise OrderError(f"Quote for {symbol} failed: {exc}") from exc
            row = found.get(symbol) if isinstance(found, dict) else None
            if not row:
                raise OrderError(f"No quote for {symbol}")
            return row

        def equity() -> tuple[str, str, float]:
            if screener.mode == "demo":
                return "demo", "demo", DEMO_EQUITY
            source = screener.source
            if source is None or not hasattr(source, "client"):
                raise OrderError(screener.errors.get("schwab") or "Schwab is not connected")
            return read_equity(source.client)

        def place(account_hash: str, order: Any):
            if screener.mode == "demo":
                raise OrderError("Demo mode does not place orders")
            return screener.source.client.place_order(account_hash, order)

        return cls(
            mode=lambda: screener.mode,
            quote=quote,
            equity=equity,
            place=place,
            client=lambda: getattr(screener.source, "client", None),
        )

    def preview(self, instruction: str, risk_pct: Any) -> dict[str, Any]:
        button_risk = coerce_risk(risk_pct)
        plan = self._plan(instruction, button_risk)
        preview_id = self._store(instruction, button_risk, plan)
        return {
            "preview_id": preview_id,
            "placed": False,
            "repriced": False,
            "summary": summary(plan),
            "plan": plan.public(),
        }

    def confirm(self, preview_id: str) -> dict[str, Any]:
        with self.lock:
            item = self.previews.pop(str(preview_id), None)
        if item is None or time.monotonic() - item["at"] > PREVIEW_TTL:
            raise OrderError("Confirm a preview first. No order was sent.")
        if self.mode() == "demo":
            plan = item["plan"]
            message = "Demo mode: order not sent. " + sentence(plan)
            return self._view(plan, placed=False, mode="demo", alert=self.notify("submit", message))
        try:
            plan = self._plan(item["instruction"], item["button_risk"])
        except Exception:
            self._restore(preview_id, item)
            raise
        if comparable(plan) != comparable(item["plan"]):
            new_id = self._store(item["instruction"], item["button_risk"], plan)
            body = self._view(plan, placed=False, mode=self.mode())
            body["repriced"] = True
            body["preview_id"] = new_id
            return body
        try:
            return self._place(plan)
        except OrderError as exc:
            # A reject means Schwab did not accept the order, so Confirm can be retried.
            if "rejected the order" in str(exc):
                self._restore(preview_id, item)
            raise

    def notify(self, kind: str, message: str, *, enqueue: bool = False) -> dict[str, str]:
        dispatch_order_alert(kind, message)
        alert = {"kind": kind, "message": message}
        if enqueue:
            with self.lock:
                self.pending.append(alert)
        return alert

    def drain_alerts(self) -> list[dict[str, str]]:
        with self.lock:
            items = list(self.pending)
            self.pending.clear()
        return items

    def watch_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.mode() == "schwab":
                    self.poll_stops()
            except Exception as exc:
                log.warning("Order status: %s", exc)
            stop.wait(5)

    def poll_stops(self) -> None:
        client = self.client()
        if client is None:
            return
        with self.lock:
            open_orders = [item for item in self.watched if not item["done"]]
        for item in open_orders:
            try:
                response = client.get_order(item["order_id"], item["account_hash"])
            except Exception as exc:
                log.warning("Order %s status: %s", item["order_id"], exc)
                continue
            if getattr(response, "status_code", 200) >= 400:
                continue
            event = stop_event(response.json())
            if event is None:
                continue
            with self.lock:
                if item["done"]:
                    continue
                item["done"] = True
            if event == "filled":
                self.notify("stop_fill", item["fill_message"], enqueue=True)
            else:
                self.notify("error", item["reject_message"], enqueue=True)

    def _plan(self, instruction: str, button_risk: float) -> OrderPlan:
        parsed = parse_instruction(instruction)
        risk = parsed.risk_pct if parsed.risk_pct is not None else button_risk
        account_hash, account_number, equity = self.equity()
        plan = build_plan(parsed, risk_pct=risk, equity=equity, quote=self.quote(parsed.symbol))
        plan.account_hash = account_hash
        plan.account_number = account_number
        return plan

    def _store(self, instruction: str, button_risk: float, plan: OrderPlan) -> str:
        preview_id = secrets.token_hex(8)
        with self.lock:
            self.previews[preview_id] = {
                "instruction": instruction,
                "button_risk": button_risk,
                "plan": plan,
                "at": time.monotonic(),
            }
        return preview_id

    def _restore(self, preview_id: str, item: dict[str, Any]) -> None:
        with self.lock:
            self.previews[str(preview_id)] = item

    def _view(self, plan: OrderPlan, *, placed: bool, mode: str, alert: dict | None = None) -> dict[str, Any]:
        return {
            "placed": placed,
            "repriced": False,
            "mode": mode,
            "route": ORDER_ROUTE,
            "order_id": None,
            "summary": summary(plan),
            "plan": plan.public(),
            "alert": alert,
        }

    def _place(self, plan: OrderPlan) -> dict[str, Any]:
        order = linked_order(plan)
        try:
            response = self.place(plan.account_hash, order)
        except OrderError:
            raise
        except Exception as exc:
            raise OrderError(f"Schwab order failed: {exc}") from exc
        status = getattr(response, "status_code", 0)
        if status >= 400:
            detail = str(getattr(response, "text", "") or "")[:300]
            raise OrderError(f"Schwab rejected the order ({status}): {detail}")
        order_id = order_id_from_location(getattr(response, "headers", {}) or {})
        if order_id is not None:
            self._watch(plan, order_id)
        message = "Order sent. " + sentence(plan)
        if order_id is None:
            message += " Schwab did not return an order id, so a stop-fill alert will not fire for this order."
        else:
            message += f" Order id {order_id}."
        body = self._view(plan, placed=True, mode="schwab", alert=self.notify("submit", message))
        body["order_id"] = order_id
        return body

    def _watch(self, plan: OrderPlan, order_id: int) -> None:
        with self.lock:
            self.watched.append(
                {
                    "order_id": order_id,
                    "account_hash": plan.account_hash,
                    "done": False,
                    "fill_message": (
                        f"Stop filled: {plan.symbol} stop-market at ${plan.stop_price:,.2f} ({plan.shares} shares)."
                    ),
                    "reject_message": f"Schwab rejected the {plan.symbol} order.",
                }
            )
