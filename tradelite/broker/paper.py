from __future__ import annotations

import uuid

from ..domain import Side
from .base import Broker, Fill


class PaperBroker(Broker):
    """Simulated broker: fills immediately at reference price +/- slippage. Idempotent on tag."""

    def __init__(self, slippage_bps: float = 5.0) -> None:
        self.slip = slippage_bps / 10_000
        self._fills: dict[str, Fill] = {}

    def place_order(self, *, symbol: str, side: Side, qty: int, price: float, tag: str) -> Fill:
        if tag in self._fills:
            return self._fills[tag]
        if qty < 1 or price <= 0:
            fill = Fill(str(uuid.uuid4()), symbol, side, qty, price, "REJECTED")
        else:
            fill = Fill(str(uuid.uuid4()), symbol, side, qty, price * (1 + self.slip * side.sign), "FILLED")
        self._fills[tag] = fill
        return fill
