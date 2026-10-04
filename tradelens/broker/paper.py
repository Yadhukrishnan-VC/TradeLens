from __future__ import annotations

import uuid
from typing import Callable

import pandas as pd

from ..domain import Side
from .base import Broker, Fill


class PaperBroker(Broker):
    """Simulated broker. Idempotent on tag.

    fill_model="instant"   fills at once at the reference price +/- slippage (simple; what unit tests use).
    fill_model="next_open" ENTRY orders (those carrying a signal_ts) wait and fill at the OPEN of the first bar AFTER
                           the signal bar, +/- slippage: exactly the backtest's assumption. Without this, paper and
                           backtest results are not comparable. Exits (no signal_ts) fill at once at the triggered
                           price, as the backtest does.

    The API and the worker are separate processes, so a pending order must be answerable by a DIFFERENT PaperBroker
    instance. Hence the order id carries everything needed to price it (symbol, side, qty, signal bar) and get_order
    is a pure function of that id and the stored bars.
    """

    def __init__(self, slippage_bps: float = 5.0, *, fill_model: str = "instant",
                 bars: Callable[[str], pd.DataFrame] | None = None) -> None:
        if fill_model not in ("instant", "next_open"):
            raise ValueError("fill_model must be 'instant' or 'next_open'")
        if fill_model == "next_open" and bars is None:
            raise ValueError("next_open needs a bars(symbol) function")
        self.slip = slippage_bps / 10_000
        self.fill_model, self._bars = fill_model, bars
        self._fills: dict[str, Fill] = {}
        self._cancelled: set[str] = set()

    def place_order(self, *, symbol: str, side: Side, qty: int, price: float, tag: str,
                    signal_ts: "pd.Timestamp | None" = None) -> Fill:
        if tag in self._fills:
            return self._fills[tag]
        if qty < 1 or price <= 0:
            fill = Fill(str(uuid.uuid4()), symbol, side, qty, price, "REJECTED", reason="BAD_ORDER")
        elif self.fill_model == "next_open" and signal_ts is not None:
            oid = "|".join(["paper", tag, symbol, side.value, str(qty), pd.Timestamp(signal_ts).isoformat()])
            fill = Fill(oid, symbol, side, qty, price, "OPEN")
        else:
            fill = Fill(str(uuid.uuid4()), symbol, side, qty, price * (1 + self.slip * side.sign), "FILLED")
        self._fills[tag] = fill
        return fill

    def get_order(self, order_id: str) -> Fill:
        parts = order_id.split("|")
        if len(parts) != 6 or parts[0] != "paper":
            raise KeyError(order_id)
        _, _, symbol, side_s, qty_s, ts_s = parts
        side, qty = Side(side_s), int(qty_s)
        if order_id in self._cancelled:
            return Fill(order_id, symbol, side, qty, 0.0, "CANCELLED")
        bars = self._bars(symbol)           # type: ignore[misc]
        bars = bars[bars.index > pd.Timestamp(ts_s)]
        if bars.empty:
            return Fill(order_id, symbol, side, qty, 0.0, "OPEN")           # the next session has not printed yet
        return Fill(order_id, symbol, side, qty, float(bars["open"].iloc[0]) * (1 + self.slip * side.sign), "FILLED")

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._cancelled or self.get_order(order_id).status != "OPEN":
            return False
        self._cancelled.add(order_id)
        return True
