"""Equity order ticket. Review does not call Schwab. Send is the only place_order.

The ticket is a side, an order type, a time in force, a share count, and a ticker.
Limit and stop also carry a price. Time in force is two Schwab fields: duration
and session. The public orders API has no overnight session, so EXT, GTC EXT,
EXTO, and GTC EXTO are not offered and are never posted.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Callable

from alerts import dispatch_order_alert
from sources import TRADE_LOGIN_HINT

log = logging.getLogger("screener.orders")

PREVIEW_TTL = 15 * 60
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# duration, session, label shown on the details page
TIF = {
    "day": ("DAY", "NORMAL", "Day"),
    "gtc": ("GOOD_TILL_CANCEL", "NORMAL", "GTC"),
    "am": ("DAY", "AM", "AM"),
    "pm": ("DAY", "PM", "PM"),
}
REMOVED_TIF = {"ext", "gtc_ext", "exto", "gtc_exto"}
OPEN_STATUSES = {
    "WORKING",
    "QUEUED",
    "ACCEPTED",
    "PENDING_ACTIVATION",
    "NEW",
    "AWAITING_PARENT_ORDER",
    "AWAITING_CONDITION",
    "AWAITING_STOP_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "AWAITING_UR_OUT",
    "AWAITING_RELEASE_TIME",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_REPLACE",
}
LIMIT_TIF = list(TIF)
DAY_ONLY = ["day"]
ORDER_TYPES = {"market", "limit", "stop"}


class OrderError(Exception):
    """The message is safe to show. It is not a token and not a stack trace."""


@dataclass
class OrderTicket:
    side: str
    order_type: str
    tif: str
    shares: int
    symbol: str
    price: float | None
    duration: str
    session: str
    tif_label: str
    last: float | None = None
    account_hash: str = ""
    account_number: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "order_type": self.order_type,
            "tif": self.tif,
            "tif_label": self.tif_label,
            "shares": self.shares,
            "symbol": self.symbol,
            "price": self.price,
            "duration": self.duration,
            "session": self.session,
            "last": self.last,
        }


def tif_choices(order_type: str) -> list[str]:
    """Market and stop are Day. Limit is Day, GTC, AM, and PM."""
    if order_type == "limit":
        return list(LIMIT_TIF)
    return list(DAY_ONLY)


def normalize_ticket(raw: dict[str, Any]) -> OrderTicket:
    """Validate a ticket. This never reads an account and never places an order."""
    symbol = _ticker(raw.get("symbol") if raw else "")

    side = str(raw.get("side") or "buy").strip().lower()
    if side not in {"buy", "sell"}:
        raise OrderError("Side must be buy or sell")
    order_type = str(raw.get("order_type") or "market").strip().lower()
    if order_type not in ORDER_TYPES:
        raise OrderError("Order type must be market, limit, or stop")
    tif = str(raw.get("tif") or "day").strip().lower()
    if tif in REMOVED_TIF:
        raise OrderError("Extended and overnight time in force are not available")
    if tif not in tif_choices(order_type):
        if order_type == "market":
            raise OrderError("Market orders are Day only")
        if order_type == "stop":
            raise OrderError("Stop orders are Day only")
        raise OrderError("That time in force is only available for limit orders")

    try:
        shares = int(raw.get("shares"))
    except (TypeError, ValueError):
        raise OrderError("Shares must be a whole number") from None
    if shares < 1:
        raise OrderError("Shares must be at least 1")
    if isinstance(raw.get("shares"), float) and not raw["shares"].is_integer():
        raise OrderError("Shares must be a whole number")

    price = None
    if order_type in {"limit", "stop"}:
        try:
            price = round(float(raw.get("price")), 2)
        except (TypeError, ValueError):
            raise OrderError("Enter a price") from None
        if price <= 0:
            raise OrderError("Price must be positive")
    duration, session, label = TIF[tif]
    return OrderTicket(side, order_type, tif, shares, symbol, price, duration, session, label)


def summary(ticket: OrderTicket) -> str:
    side = "Buy" if ticket.side == "buy" else "Sell"
    kind = {"market": "market", "limit": "limit", "stop": "stop"}[ticket.order_type]
    price = ""
    if ticket.order_type == "limit":
        price = f" at ${ticket.price:.2f}"
    elif ticket.order_type == "stop":
        price = f", stop ${ticket.price:.2f}"
    last = f" Last ${ticket.last:.2f}." if ticket.last is not None else ""
    return (
        f"{side} {ticket.shares} {ticket.symbol} {kind}{price}. "
        f"Time in force {ticket.tif_label}. "
        f"Schwab session {ticket.session}, duration {ticket.duration}.{last} "
        "Send this order?"
    )


QUICK_PERCENTS = {
    "1": Decimal("1"),
    "0.5": Decimal("0.5"),
    "0.33": Decimal("0.33"),
    "0.25": Decimal("0.25"),
}
STOP_OFFSET = Decimal("0.10")


@dataclass
class QuickOrder:
    side: str
    order_type: str
    tif: str
    shares: int
    symbol: str
    entry: float
    stop: float
    last: float
    duration: str
    session: str
    risk: float
    account_hash: str = ""
    account_number: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "order_type": self.order_type,
            "tif": self.tif,
            "shares": self.shares,
            "symbol": self.symbol,
            "entry": self.entry,
            "stop": self.stop,
            "last": self.last,
            "duration": self.duration,
            "session": self.session,
            "risk": self.risk,
        }


def _ticker(value: Any) -> str:
    symbol_text = str(value or "").strip()
    if not symbol_text:
        raise OrderError("Enter a ticker")
    if re.search(r"\s", symbol_text) or any(ch in symbol_text for ch in ",;"):
        raise OrderError("Enter a ticker only, not an order sentence")
    symbol = symbol_text.upper().lstrip("$")
    if not SYMBOL.fullmatch(symbol):
        raise OrderError("Enter a ticker only, not an order sentence")
    return symbol


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return Decimal(str(value).strip())
    except Exception:
        return None


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quote_price(quote: dict[str, Any], key: str) -> Decimal | None:
    if not isinstance(quote, dict):
        return None
    return _decimal(quote.get(key))


def _quick_inputs(raw: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Side, entry, and stop from the quote. Raises before any account or place call."""
    side = str((raw or {}).get("side") or "buy").strip().lower()
    if side not in {"buy", "sell"}:
        raise OrderError("Side must be buy or sell")
    order_type = str(raw.get("order_type") or "market").strip().lower()
    if order_type not in {"market", "limit"}:
        raise OrderError("Order type must be market or limit")
    tif = str(raw.get("tif") or "day").strip().lower()
    if tif in REMOVED_TIF:
        raise OrderError("Extended and overnight time in force are not available")
    if tif not in {"day", "gtc"}:
        raise OrderError("Time in force must be Day or GTC")
    last = _quote_price(quote, "lastPrice")
    if last is None:
        raise OrderError("No last price for that ticker")
    custom = _decimal(raw.get("stop"))
    if custom is not None:
        if custom <= 0:
            raise OrderError("Stop must be positive")
        stop = _cents(custom)
    elif side == "buy":
        low = _quote_price(quote, "lowPrice")
        if low is None:
            raise OrderError("No low of day for that ticker")
        stop = _cents(low - STOP_OFFSET)
    else:
        high = _quote_price(quote, "highPrice")
        if high is None:
            raise OrderError("No high of day for that ticker")
        stop = _cents(high + STOP_OFFSET)
    if order_type == "limit":
        price = _decimal(raw.get("price"))
        if price is None:
            raise OrderError("Enter a limit price")
        if price <= 0:
            raise OrderError("Price must be positive")
        entry = _cents(price)
    else:
        entry = last
    if side == "buy" and last <= stop:
        raise OrderError("Last price is already through the stop")
    if side == "sell" and last >= stop:
        raise OrderError("Last price is already through the stop")
    duration = "DAY" if tif == "day" else "GOOD_TILL_CANCEL"
    return {
        "side": side,
        "order_type": order_type,
        "tif": tif,
        "symbol": _ticker(raw.get("symbol")),
        "entry": entry,
        "stop": stop,
        "last": last,
        "duration": duration,
    }


def _quick_risk(raw: dict[str, Any], equity: Any) -> Decimal:
    """Dollar risk wins when the box has a number. Otherwise equity times the percent."""
    dollars = _decimal(raw.get("risk_dollars"))
    if dollars is not None:
        if dollars <= 0:
            raise OrderError("Risk must be positive")
        return dollars
    percent = raw.get("risk_percent")
    if isinstance(percent, str):
        percent = percent.strip().rstrip("%")
    key = None
    parsed = _decimal(percent) if percent not in (None, "") else None
    if parsed is not None:
        for name, points in QUICK_PERCENTS.items():
            if parsed == points:
                key = name
                break
        if key is None:
            raise OrderError("Choose a risk")
    if key is None:
        raise OrderError("Choose a risk")
    account = _decimal(equity)
    if account is None or account <= 0:
        raise OrderError("Account equity is missing")
    return account * QUICK_PERCENTS[key] / Decimal("100")


def quick_shares(risk: Decimal, entry: Decimal, stop: Decimal) -> int:
    """floor(risk / abs(entry - stop)). Zero shares are refused."""
    distance = abs(entry - stop)
    if distance == 0:
        raise OrderError("Share size is 0")
    shares = int((risk / distance).to_integral_value(rounding=ROUND_FLOOR))
    if shares < 1:
        raise OrderError("Share size is 0")
    return shares


def build_quick(raw: dict[str, Any], quote: dict[str, Any], equity: Any) -> QuickOrder:
    """Size a bracket. Does not look up an account and does not place an order."""
    inputs = _quick_inputs(raw, quote)
    risk = _quick_risk(raw, equity)
    shares = quick_shares(risk, inputs["entry"], inputs["stop"])
    return QuickOrder(
        side=inputs["side"],
        order_type=inputs["order_type"],
        tif=inputs["tif"],
        shares=shares,
        symbol=inputs["symbol"],
        entry=float(inputs["entry"]),
        stop=float(inputs["stop"]),
        last=float(inputs["last"]),
        duration=inputs["duration"],
        session="NORMAL",
        risk=float(risk),
    )


def quick_summary(plan: QuickOrder) -> str:
    side = "Buy" if plan.side == "buy" else "Sell"
    kind = "market" if plan.order_type == "market" else f"limit ${plan.entry:.2f}"
    tif = "Day" if plan.duration == "DAY" else "GTC"
    return f"{side} {plan.shares} {plan.symbol} {kind}, stop ${plan.stop:.2f}, {tif}."


def bracket_order(plan: QuickOrder):
    """Entry parent plus a stop-market child. Session is NORMAL. No EXTO."""
    from schwab.orders.common import Duration, EquityInstruction, OrderStrategyType, OrderType, Session
    from schwab.orders.common import first_triggers_second
    from schwab.orders.generic import OrderBuilder

    duration = Duration.DAY if plan.duration == "DAY" else Duration.GOOD_TILL_CANCEL
    parent_side = EquityInstruction.BUY if plan.side == "buy" else EquityInstruction.SELL
    child_side = EquityInstruction.SELL if plan.side == "buy" else EquityInstruction.BUY
    parent_type = OrderType.MARKET if plan.order_type == "market" else OrderType.LIMIT

    def leg(instruction, order_type, *, price: float | None = None, stop: float | None = None):
        builder = OrderBuilder()
        builder.set_session(Session.NORMAL)
        builder.set_duration(duration)
        builder.set_order_type(order_type)
        builder.set_order_strategy_type(OrderStrategyType.SINGLE)
        if price is not None:
            builder.set_price(f"{price:.2f}")
        if stop is not None:
            builder.set_stop_price(f"{stop:.2f}")
        builder.add_equity_leg(instruction, plan.symbol, int(plan.shares))
        return builder

    parent = leg(parent_side, parent_type, price=plan.entry if plan.order_type == "limit" else None)
    child = leg(child_side, OrderType.STOP, stop=plan.stop)
    return first_triggers_second(parent, child)


def equity_order(ticket: OrderTicket):
    """One single-leg equity order. Session and duration come from the time in force."""
    from schwab.orders.common import EquityInstruction, OrderStrategyType, OrderType
    from schwab.orders.generic import OrderBuilder

    instruction = EquityInstruction.BUY if ticket.side == "buy" else EquityInstruction.SELL
    order_type = {"market": OrderType.MARKET, "limit": OrderType.LIMIT, "stop": OrderType.STOP}[ticket.order_type]
    builder = OrderBuilder()
    builder.set_enforce_enums(False)
    builder.set_order_strategy_type(OrderStrategyType.SINGLE)
    builder.set_order_type(order_type)
    builder.set_duration(ticket.duration)
    builder.set_session(ticket.session)
    if ticket.order_type == "limit":
        builder.set_price(f"{ticket.price:.2f}")
    elif ticket.order_type == "stop":
        builder.set_stop_price(f"{ticket.price:.2f}")
    builder.add_equity_leg(instruction, ticket.symbol, int(ticket.shares))
    return builder


def schwab_detail(response: Any) -> str:
    """Schwab's own error text, trimmed. Empty when the body is blank."""
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return ""
    try:
        body = response.json()
    except Exception:
        return text[:400]
    if isinstance(body, dict):
        for key in ("message", "error_description", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                for key in ("message", "detail", "title"):
                    value = first.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:400]
            if isinstance(first, str) and first.strip():
                return first.strip()[:400]
    return text[:400]


def _check(response: Any, what: str) -> None:
    status = getattr(response, "status_code", 200)
    if status < 400:
        return
    detail = schwab_detail(response)
    if status == 401:
        message = (
            "Schwab accounts returned 401. This trading token cannot read accounts. "
            "The trading app needs Accounts and Trading Production and Ready For Use. "
            + TRADE_LOGIN_HINT
        )
        if detail:
            message = f"{message} {detail}"
        raise OrderError(message)
    message = f"{what} failed ({status})"
    if detail:
        message = f"{message}: {detail}"
    raise OrderError(message)


def read_account(client: Any) -> tuple[str, str]:
    """Account hash for place_order. A 401 is Schwab's error, not a bad ticker."""
    try:
        numbers = client.get_account_numbers()
    except Exception as exc:
        raise OrderError(f"Schwab accounts failed: {exc}") from exc
    _check(numbers, "Schwab accounts")
    try:
        accounts = numbers.json()
    except Exception as exc:
        raise OrderError(f"Schwab accounts failed: {exc}") from exc
    if not isinstance(accounts, list) or not accounts:
        raise OrderError("No Schwab account on this login")
    chosen = accounts[0] if isinstance(accounts[0], dict) else {}
    account_hash = str(chosen.get("hashValue") or "")
    account_number = str(chosen.get("accountNumber") or "")
    if not account_hash:
        raise OrderError("Schwab account hash is missing")
    return account_hash, account_number


def read_equity(client: Any, account_hash: str) -> float:
    """Account equity from the trading client. Used only to size a quick order."""
    try:
        response = client.get_account(account_hash)
    except Exception as exc:
        raise OrderError(f"Schwab accounts failed: {exc}") from exc
    _check(response, "Schwab accounts")
    try:
        body = response.json()
    except Exception as exc:
        raise OrderError(f"Schwab accounts failed: {exc}") from exc
    account = body.get("securitiesAccount") if isinstance(body, dict) else None
    balances = account.get("currentBalances") if isinstance(account, dict) else None
    equity = _decimal((balances or {}).get("equity") if isinstance(balances, dict) else None)
    if equity is None or equity <= 0:
        raise OrderError("Account equity is missing")
    return float(equity)


def order_event(order: dict[str, Any]) -> str | None:
    """``filled`` or ``rejected`` once Schwab finishes the order, else wait."""
    if not isinstance(order, dict):
        return None
    status = order.get("status")
    if status == "FILLED":
        return "filled"
    if status == "REJECTED":
        return "rejected"
    return None


class OrderBook:
    def __init__(
        self,
        *,
        mode: Callable[[], str],
        quote: Callable[[str], dict[str, Any]],
        account: Callable[[], tuple[str, str]],
        place: Callable[[str, Any], Any],
        client: Callable[[], Any] | None = None,
        equity: Callable[[], Any] | None = None,
    ):
        self.mode = mode
        self.quote = quote
        self.account = account
        self.place = place
        self.client = client or (lambda: None)
        self.equity = equity or (lambda: (_ for _ in ()).throw(OrderError("Account equity is missing")))
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

        def trade_client() -> Any:
            if screener.mode == "demo":
                raise OrderError("Demo mode does not place orders")
            screener.ensure_trade()
            client = screener.trade_client
            if client is None:
                raise OrderError(TRADE_LOGIN_HINT)
            return client

        def account() -> tuple[str, str]:
            if screener.mode == "demo":
                return "demo", "demo"
            return read_account(trade_client())

        def place(account_hash: str, order: Any):
            return trade_client().place_order(account_hash, order)

        def current_trade() -> Any:
            if screener.mode == "demo":
                return None
            screener.ensure_trade()
            return screener.trade_client

        def equity() -> float:
            client = trade_client()
            account_hash, _account_number = read_account(client)
            return read_equity(client, account_hash)

        return cls(
            mode=lambda: screener.mode,
            quote=quote,
            account=account,
            place=place,
            client=current_trade,
            equity=equity,
        )

    def preview(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Check the ticket and read a quote. Does not look up an account or place an order."""
        ticket = normalize_ticket(raw)
        quote = self.quote(ticket.symbol)
        last = quote.get("lastPrice")
        try:
            ticket.last = round(float(last), 2) if last is not None else None
        except (TypeError, ValueError):
            ticket.last = None
        preview_id = secrets.token_hex(8)
        with self.lock:
            self.previews[preview_id] = {"ticket": ticket, "at": time.monotonic()}
        return {
            "preview_id": preview_id,
            "placed": False,
            "summary": summary(ticket),
            "plan": ticket.public(),
        }

    def send(self, preview_id: str) -> dict[str, Any]:
        """Place the reviewed ticket. Demo mode describes it and does not call Schwab."""
        with self.lock:
            item = self.previews.pop(str(preview_id), None)
        if item is None or time.monotonic() - item["at"] > PREVIEW_TTL:
            raise OrderError("Review an order first. Nothing was sent.")
        ticket: OrderTicket = item["ticket"]
        if self.mode() == "demo":
            message = "Demo mode: order not sent. " + summary(ticket)
            return self._view(ticket, placed=False, mode="demo", alert=self.notify("submit", message))
        try:
            account_hash, account_number = self.account()
        except Exception:
            self._restore(preview_id, item)
            raise
        ticket.account_hash = account_hash
        ticket.account_number = account_number
        try:
            return self._place(ticket)
        except OrderError:
            self._restore(preview_id, item)
            raise

    def quick_send(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Place a sized bracket now. Demo mode quotes locally and does not call Schwab."""
        symbol = _ticker((raw or {}).get("symbol"))
        quote = self.quote(symbol)
        inputs = _quick_inputs(raw, quote)
        if self.mode() == "demo":
            side = "Buy" if inputs["side"] == "buy" else "Sell"
            message = f"Demo mode: order not sent. {side} {symbol}, stop ${float(inputs['stop']):.2f}."
            alert = self.notify("submit", message)
            return {"placed": False, "mode": "demo", "summary": message, "plan": None, "alert": alert}
        plan = build_quick(raw, quote, self.equity())
        account_hash, account_number = self.account()
        plan.account_hash = account_hash
        plan.account_number = account_number
        order = bracket_order(plan)
        try:
            response = self.place(account_hash, order)
        except OrderError:
            raise
        except Exception as exc:
            raise OrderError(f"Schwab order failed: {exc}") from exc
        status = getattr(response, "status_code", 0)
        if status >= 400:
            _check(response, "Schwab order")
        order_id = _order_id(getattr(response, "headers", {}) or {})
        if order_id is not None:
            side = "Buy" if plan.side == "buy" else "Sell"
            with self.lock:
                self.watched.append({
                    "order_id": order_id,
                    "account_hash": account_hash,
                    "done": False,
                    "fill_message": f"Filled: {side} {plan.shares} {plan.symbol}.",
                    "reject_message": f"Schwab rejected the {plan.symbol} order.",
                })
        message = "Order sent. " + quick_summary(plan)
        if order_id is None:
            message += " Schwab did not return an order id."
        else:
            message += f" Order id {order_id}."
        return {
            "placed": True,
            "mode": "schwab",
            "order_id": order_id,
            "summary": quick_summary(plan),
            "plan": plan.public(),
            "alert": self.notify("submit", message),
        }

    def notify(self, kind: str, message: str, *, enqueue: bool = False) -> dict[str, str]:
        dispatch_order_alert(kind, message)
        alert = {"kind": kind, "message": message}
        if enqueue:
            with self.lock:
                self.pending.append(alert)
        return alert

    def working_orders(self) -> dict[str, Any]:
        """Open orders on the trading account. Demo mode does not call Schwab."""
        if self.mode() == "demo":
            return {"orders": [], "mode": "demo"}
        client = self.client()
        if client is None:
            raise OrderError(TRADE_LOGIN_HINT)
        account_hash, _account_number = self.account()
        try:
            response = client.get_orders_for_account(account_hash, max_results=50)
        except OrderError:
            raise
        except Exception as exc:
            raise OrderError(f"Schwab orders failed: {exc}") from exc
        _check(response, "Schwab orders")
        try:
            body = response.json()
        except Exception as exc:
            raise OrderError(f"Schwab orders failed: {exc}") from exc
        orders = []
        for item in body if isinstance(body, list) else []:
            if not isinstance(item, dict) or item.get("status") not in OPEN_STATUSES:
                continue
            public = _public_order(item)
            if public is not None:
                orders.append(public)
        return {"orders": orders, "mode": "schwab"}

    def cancel(self, order_id: int) -> dict[str, Any]:
        """Cancel one working order. A filled order is left alone. Demo mode does not call Schwab."""
        if self.mode() == "demo":
            raise OrderError("Demo mode does not cancel live orders")
        client = self.client()
        if client is None:
            raise OrderError(TRADE_LOGIN_HINT)
        account_hash, _account_number = self.account()
        try:
            current = client.get_order(int(order_id), account_hash)
        except OrderError:
            raise
        except Exception as exc:
            raise OrderError(f"Schwab order failed: {exc}") from exc
        _check(current, "Schwab order")
        try:
            body = current.json()
        except Exception as exc:
            raise OrderError(f"Schwab order failed: {exc}") from exc
        status = body.get("status") if isinstance(body, dict) else ""
        if status == "FILLED":
            raise OrderError("That order is already filled")
        if status not in OPEN_STATUSES:
            raise OrderError("That order is no longer working")
        try:
            response = client.cancel_order(int(order_id), account_hash)
        except OrderError:
            raise
        except Exception as exc:
            raise OrderError(f"Schwab cancel failed: {exc}") from exc
        if getattr(response, "status_code", 200) >= 400:
            _check(response, "Schwab cancel")
        return {"canceled": True, "order_id": int(order_id)}

    def drain_alerts(self) -> list[dict[str, str]]:
        with self.lock:
            items = list(self.pending)
            self.pending.clear()
        return items

    def watch_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.mode() == "schwab":
                    self.poll_orders()
            except Exception as exc:
                log.warning("Order status: %s", exc)
            stop.wait(5)

    def poll_orders(self) -> None:
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
            try:
                body = response.json()
            except Exception:
                continue
            event = order_event(body if isinstance(body, dict) else {})
            if event is None:
                continue
            with self.lock:
                if item["done"]:
                    continue
                item["done"] = True
            if event == "filled":
                self.notify("submit", item["fill_message"], enqueue=True)
            else:
                self.notify("error", item["reject_message"], enqueue=True)

    def _restore(self, preview_id: str, item: dict[str, Any]) -> None:
        with self.lock:
            self.previews[str(preview_id)] = item

    def _view(self, ticket: OrderTicket, *, placed: bool, mode: str, alert: dict | None = None) -> dict[str, Any]:
        return {
            "placed": placed,
            "mode": mode,
            "order_id": None,
            "summary": summary(ticket),
            "plan": ticket.public(),
            "alert": alert,
        }

    def _place(self, ticket: OrderTicket) -> dict[str, Any]:
        order = equity_order(ticket)
        try:
            response = self.place(ticket.account_hash, order)
        except OrderError:
            raise
        except Exception as exc:
            raise OrderError(f"Schwab order failed: {exc}") from exc
        status = getattr(response, "status_code", 0)
        if status >= 400:
            _check(response, "Schwab order")
        order_id = _order_id(getattr(response, "headers", {}) or {})
        if order_id is not None:
            self._watch(ticket, order_id)
        message = "Order sent. " + summary(ticket)
        if order_id is None:
            message += " Schwab did not return an order id."
        else:
            message += f" Order id {order_id}."
        body = self._view(ticket, placed=True, mode="schwab", alert=self.notify("submit", message))
        body["order_id"] = order_id
        return body

    def _watch(self, ticket: OrderTicket, order_id: int) -> None:
        side = "Buy" if ticket.side == "buy" else "Sell"
        with self.lock:
            self.watched.append(
                {
                    "order_id": order_id,
                    "account_hash": ticket.account_hash,
                    "done": False,
                    "fill_message": f"Filled: {side} {ticket.shares} {ticket.symbol}.",
                    "reject_message": f"Schwab rejected the {ticket.symbol} order.",
                }
            )


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_order(order: dict[str, Any]) -> dict[str, Any] | None:
    """Fields shown under order entry. No account hash and no token."""
    legs = order.get("orderLegCollection") or []
    leg = legs[0] if isinstance(legs, list) and legs and isinstance(legs[0], dict) else {}
    instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
    symbol = str(instrument.get("symbol") or "").upper()
    try:
        order_id = int(order.get("orderId"))
    except (TypeError, ValueError):
        return None
    if not symbol:
        return None
    quantity = leg.get("quantity", order.get("quantity"))
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
        quantity = None
    price = _number(order.get("price"))
    if price is None:
        price = _number(order.get("stopPrice"))
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": str(leg.get("instruction") or ""),
        "order_type": str(order.get("orderType") or ""),
        "quantity": quantity,
        "price": price,
        "status": str(order.get("status") or ""),
    }


def _order_id(headers: Any) -> int | None:
    if not headers:
        return None
    location = headers.get("Location") or headers.get("location") or ""
    match = re.search(r"/orders/(\d+)", str(location))
    return int(match.group(1)) if match else None
