"""Zerodha (Kite Connect) adapter: delivery (CNC) equity on NSE.

Facts this was written against (Zerodha's published docs, checked 1 Oct 2026; RE-CHECK before real money):
  * no sandbox exists: the only real test is a real order. Hence DRY_RUN (default ON) and hard value caps.
  * orders are accepted only from the static IP registered in the developer console (exact egress IP; IPv4 vs IPv6 matters)
  * market orders need a non-zero market_protection; limit orders do not. This adapter uses LIMIT orders only
    ("marketable limits": buy a little above / sell a little below the reference), which cannot slip past the buffer
  * at most 10 orders/second (irrelevant at this scale); order `tag` max 20 alphanumerics
  * GTT (good-till-triggered) is the way to keep a stop alive for a delivery position across days. A triggered GTT
    places a LIMIT order, so a gap through the limit leaves the position UNSOLD: stop_status() reports that, never hides it.
    Selling held shares may require CDSL TPIN authorisation (or a pre-authorisation/DDPI) or the triggered sell fails.
  * access tokens expire daily (~06:00 IST): `tradelens kite-login` each trading morning.

Everything the broker class itself can do wrong with money is capped HERE, independent of the risk engine:
  max value per BUY order, max total BUY value per day (read from the broker's own order book, so it survives restarts).
  Exits are never capped or blocked.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..domain import Side
from ..models import BrokerSessionRow
from .base import Broker, BrokerError, BrokerUncertain, Fill

log = logging.getLogger("tradelens.zerodha")
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)


def _tag(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw)[:20]


def _round_tick(price: float, tick: float, up: bool) -> float:
    n = price / tick
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * tick, 2)


def _translate(e: Exception) -> BrokerError:
    """Map a kiteconnect exception to ours without importing the SDK (matched by class name)."""
    name, msg = type(e).__name__, str(e)[:300]
    if name == "TokenException":
        return BrokerError(f"Zerodha session expired or invalid ({msg}). Run: python -m tradelens kite-login")
    if name == "NetworkException":
        return BrokerUncertain(f"network problem talking to Zerodha ({msg})")
    return BrokerError(f"{name}: {msg}")


class ZerodhaBroker(Broker):
    supports_exchange_stops = True

    def __init__(self, kite_factory: Callable[[], Any], *, dry_run: bool = True, max_order_value: float = 10_000.0,
                 max_daily_value: float = 25_000.0, limit_buffer_bps: float = 30.0, stop_limit_buffer_bps: float = 150.0,
                 tick_size: float = 0.05, exchange: str = "NSE", product: str = "CNC",
                 clock: Callable[[], datetime] = lambda: datetime.now(IST).replace(tzinfo=None)) -> None:
        if max_order_value <= 0 or max_daily_value <= 0:
            raise ValueError("value caps must be positive")
        self._factory, self.dry_run = kite_factory, dry_run
        self.max_order_value, self.max_daily_value = max_order_value, max_daily_value
        self.buf, self.stop_buf, self.tick = limit_buffer_bps / 10_000, stop_limit_buffer_bps / 10_000, tick_size
        self.exchange, self.product, self.clock = exchange, product, clock

    # ---------------------------------------------------------------- plumbing
    def _kite(self) -> Any:
        try:
            return self._factory()
        except BrokerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _translate(e) from e

    @staticmethod
    def _call(fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
        try:
            return fn(*a, **kw)
        except BrokerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _translate(e) from e

    def _variety(self) -> str:
        t = self.clock()
        return "regular" if (t.weekday() < 5 and SESSION_OPEN <= t.time() < SESSION_CLOSE) else "amo"

    def _todays_orders(self, kite: Any) -> list[dict]:
        return list(self._call(kite.orders))

    # ---------------------------------------------------------------- orders
    def place_order(self, *, symbol: str, side: Side, qty: int, price: float, tag: str,
                    signal_ts: datetime | None = None) -> Fill:
        if qty < 1 or price <= 0:
            return Fill("", symbol, side, qty, price, "REJECTED", reason="BAD_ORDER")
        kite, ztag = self._kite(), _tag(tag)
        orders = self._todays_orders(kite)
        existing = next((o for o in orders if o.get("tag") == ztag), None)
        if existing is not None:                                  # idempotent on tag: a retry never doubles
            return self._to_fill(existing, symbol, side, qty)
        limit = _round_tick(price * (1 + self.buf), self.tick, up=True) if side is Side.BUY \
            else _round_tick(price * (1 - self.buf), self.tick, up=False)
        if side is Side.BUY:
            value = qty * limit
            if value > self.max_order_value:
                raise BrokerError(f"BUY {qty} {symbol} is worth {value:,.0f}, over the per-order cap "
                                  f"{self.max_order_value:,.0f} (LIVE_MAX_ORDER_VALUE)")
            today = sum(float(o.get("quantity", 0)) * float(o.get("price") or o.get("average_price") or 0)
                        for o in orders if o.get("transaction_type") == "BUY"
                        and str(o.get("status", "")).upper() not in ("REJECTED", "CANCELLED"))
            if today + value > self.max_daily_value:
                raise BrokerError(f"BUY would take today's total to {today + value:,.0f}, over the daily cap "
                                  f"{self.max_daily_value:,.0f} (LIVE_MAX_DAILY_VALUE)")
        variety = self._variety()
        if self.dry_run:
            return Fill("", symbol, side, qty, limit, "REJECTED",
                        reason=f"DRY_RUN: would place {variety} LIMIT {side.value} {qty} {symbol} @ {limit} (tag {ztag})")
        try:
            oid = kite.place_order(variety=variety, exchange=self.exchange, tradingsymbol=symbol,
                                   transaction_type=side.value, quantity=qty, product=self.product,
                                   order_type="LIMIT", price=limit, validity="DAY", tag=ztag)
        except Exception as e:  # noqa: BLE001
            err = _translate(e)
            if isinstance(err, BrokerUncertain):
                # did it go through anyway? ask the order book by tag before declaring anything
                try:
                    found = next((o for o in self._todays_orders(kite) if o.get("tag") == ztag), None)
                except BrokerError:
                    raise BrokerUncertain(f"{err} and the order book could not be read to check") from e
                if found is None:
                    raise BrokerUncertain(f"{err}; the order book shows no order with tag {ztag}, "
                                          f"but verify at the broker before retrying") from e
                return self._to_fill(found, symbol, side, qty)
            raise err from e
        return Fill(str(oid), symbol, side, qty, limit, "OPEN")

    @staticmethod
    def _to_fill(o: dict, symbol: str, side: Side, qty: int) -> Fill:
        status = str(o.get("status", "")).upper()
        filled = int(o.get("filled_quantity") or 0)
        avg = float(o.get("average_price") or o.get("price") or 0.0)
        oid, why = str(o.get("order_id", "")), str(o.get("status_message") or "")
        if status == "COMPLETE":
            return Fill(oid, symbol, side, qty, avg, "FILLED", filled_qty=filled or qty)
        if status == "REJECTED":
            return Fill(oid, symbol, side, qty, avg, "REJECTED", filled_qty=filled, reason=why)
        if status.startswith("CANCELLED"):
            return Fill(oid, symbol, side, qty, avg, "CANCELLED", filled_qty=filled, reason=why)
        return Fill(oid, symbol, side, qty, avg, "OPEN", filled_qty=filled)

    def get_order(self, order_id: str) -> Fill:
        kite = self._kite()
        history = self._call(kite.order_history, order_id)
        if not history:
            raise BrokerError(f"order {order_id} not found at the broker")
        o = history[-1]
        return self._to_fill(o, str(o.get("tradingsymbol", "")), Side(o.get("transaction_type", "BUY")),
                             int(o.get("quantity") or 0))

    def cancel_order(self, order_id: str) -> bool:
        kite = self._kite()
        try:
            variety = self._call(kite.order_history, order_id)[-1].get("variety", "regular")
            self._call(kite.cancel_order, variety=variety, order_id=order_id)
            return True
        except BrokerError as e:
            log.error("cancel failed: %s", e)
            return False

    # ---------------------------------------------------------------- exchange-side stop (GTT)
    def place_protective_stop(self, *, symbol: str, qty: int, trigger: float, tag: str) -> str | None:
        if self.dry_run:
            return None
        kite = self._kite()
        key = f"{self.exchange}:{symbol}"
        ltp = float(self._call(kite.ltp, [key])[key]["last_price"])
        if trigger >= ltp:
            raise BrokerError(f"stop {trigger} is not below the last price {ltp}: it would trigger at once")
        limit = _round_tick(trigger * (1 - self.stop_buf), self.tick, up=False)
        res = self._call(kite.place_gtt, trigger_type="single", tradingsymbol=symbol, exchange=self.exchange,
                         trigger_values=[round(trigger, 2)], last_price=ltp,
                         orders=[{"exchange": self.exchange, "tradingsymbol": symbol, "transaction_type": "SELL",
                                  "quantity": qty, "order_type": "LIMIT", "product": self.product, "price": limit}])
        return str(res["trigger_id"])

    def stop_status(self, stop_id: str) -> Fill | None:
        kite = self._kite()
        g = self._call(kite.get_gtt, int(stop_id))
        status = str(g.get("status", "")).lower()
        leg = (g.get("orders") or [{}])[0]
        sym = str((g.get("condition") or {}).get("tradingsymbol", leg.get("tradingsymbol", "")))
        qty = int(leg.get("quantity") or 0)
        if status == "active":
            return Fill(stop_id, sym, Side.SELL, qty, 0.0, "OPEN")
        if status == "triggered":
            result = (leg.get("result") or {}).get("order_result") or {}
            oid = result.get("order_id")
            if not oid:
                return Fill(stop_id, sym, Side.SELL, qty, 0.0, "REJECTED",
                            reason=f"GTT triggered but its order failed: {result.get('rejection_reason', 'unknown')}")
            f = self.get_order(str(oid))
            if f.status == "OPEN":
                return Fill(str(oid), sym, Side.SELL, qty, 0.0, "OPEN", reason="TRIGGERED_RESTING")
            if f.status == "FILLED":
                return f
            return Fill(str(oid), sym, Side.SELL, qty, 0.0, "REJECTED", reason=f"GTT order {f.status.lower()}: {f.reason}")
        return Fill(stop_id, sym, Side.SELL, qty, 0.0, "CANCELLED", reason=f"GTT is {status}")

    def cancel_protective_stop(self, stop_id: str) -> bool:
        kite = self._kite()
        try:
            g = self._call(kite.get_gtt, int(stop_id))
            status = str(g.get("status", "")).lower()
            if status == "active":
                self._call(kite.delete_gtt, int(stop_id))
                return True
            if status == "triggered":
                st = self.stop_status(stop_id)
                if st is not None and st.status == "OPEN":              # a resting limit order: cancel that
                    return self.cancel_order(st.order_id)
                return st is not None and st.status in ("REJECTED", "CANCELLED")   # filled => do NOT sell again
            return True                                                  # already gone
        except BrokerError as e:
            log.error("stop cancel failed: %s", e)
            return False

    # ---------------------------------------------------------------- reconciliation
    def held_quantities(self) -> dict[str, int]:
        """Shares held per symbol: delivery holdings (including unsettled T+1) or today's net CNC positions,
        whichever is larger. A heuristic: verify it on your first live day. Reconciliation only reports."""
        kite = self._kite()
        held: dict[str, int] = {}
        for h in self._call(kite.holdings):
            if h.get("exchange", self.exchange) == self.exchange:
                held[h["tradingsymbol"]] = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
        for p in (self._call(kite.positions) or {}).get("net", []):
            if p.get("product") == self.product and p.get("exchange", self.exchange) == self.exchange and p.get("quantity"):
                held[p["tradingsymbol"]] = max(held.get(p["tradingsymbol"], 0), int(p["quantity"]))
        return held


# ------------------------------------------------------------------ daily login + factory
def kite_login_url(api_key: str) -> str:
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


def extract_request_token(text: str) -> str:
    """Accepts the bare token or the whole redirect URL (...?request_token=XXXX&action=login&status=success)."""
    m = re.search(r"request_token=([A-Za-z0-9]+)", text)
    token = m.group(1) if m else text.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", token):
        raise ValueError("that does not look like a request_token")
    return token


def _import_kite() -> Any:
    try:
        from kiteconnect import KiteConnect
    except ImportError as e:   # pragma: no cover
        raise RuntimeError('Zerodha needs the SDK: pip install "kiteconnect>=5.1" (or pip install -e .[zerodha])') from e
    return KiteConnect


def complete_login(sf: sessionmaker[Session], api_key: str, api_secret: str, request_token_or_url: str, *,
                   kite_cls: Any = None,
                   now: Callable[[], datetime] = lambda: datetime.now(IST).replace(tzinfo=None)) -> str:
    """Exchange the request token for today's access token and store it. Returns the Zerodha user id."""
    kite = (kite_cls or _import_kite())(api_key=api_key)
    data = kite.generate_session(extract_request_token(request_token_or_url), api_secret=api_secret)
    with sf() as s:
        row = s.query(BrokerSessionRow).filter_by(broker="zerodha").one_or_none()
        if row is None:
            s.add(BrokerSessionRow(broker="zerodha", access_token=data["access_token"], created_at=now()))
        else:
            row.access_token, row.created_at = data["access_token"], now()
        s.commit()
    return str(data.get("user_id", ""))


def session_kite_factory(sf: sessionmaker[Session], api_key: str, *, kite_cls: Any = None) -> Callable[[], Any]:
    """Returns a function giving a Kite client with the LATEST stored token (the daily login happens while the
    API and the worker are already running, so the token is read on use, not at startup)."""
    cache: dict[str, Any] = {}

    def factory() -> Any:
        with sf() as s:
            row = s.query(BrokerSessionRow).filter_by(broker="zerodha").one_or_none()
        if row is None:
            raise BrokerError("not logged in to Zerodha. Run: python -m tradelens kite-login")
        if cache.get("token") != row.access_token:
            kite = (kite_cls or _import_kite())(api_key=api_key)
            kite.set_access_token(row.access_token)
            cache.update(token=row.access_token, kite=kite)
        return cache["kite"]
    return factory


def validate_live_settings(s: Settings) -> None:
    problems = []
    if not s.zerodha_api_key or not s.zerodha_api_secret:
        problems.append("ZERODHA_API_KEY and ZERODHA_API_SECRET are required")
    if s.mode == "auto" and not s.live_allow_auto:
        problems.append("TRADELENS_MODE=auto with a real broker is refused. Use semi_auto (you approve each order), "
                        "or set LIVE_ALLOW_AUTO=1 if you really mean it")
    if s.live_max_order_value <= 0 or s.live_max_daily_value < s.live_max_order_value:
        problems.append("LIVE_MAX_ORDER_VALUE must be > 0 and LIVE_MAX_DAILY_VALUE must be >= it")
    if problems:
        raise RuntimeError("Unsafe or incomplete live configuration:\n  - " + "\n  - ".join(problems))


def build_zerodha(s: Settings, sf: sessionmaker[Session], *, kite_cls: Any = None) -> ZerodhaBroker:
    validate_live_settings(s)
    return ZerodhaBroker(session_kite_factory(sf, s.zerodha_api_key, kite_cls=kite_cls), dry_run=s.zerodha_dry_run,
                         max_order_value=s.live_max_order_value, max_daily_value=s.live_max_daily_value,
                         limit_buffer_bps=s.live_limit_buffer_bps, stop_limit_buffer_bps=s.live_stop_buffer_bps)
