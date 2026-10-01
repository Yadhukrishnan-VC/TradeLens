from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain import Side


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    status: str  # "FILLED" | "REJECTED"


class Broker(ABC):
    @abstractmethod
    def place_order(self, *, symbol: str, side: Side, qty: int, price: float, tag: str) -> Fill:
        """Place a market-style order. MUST be idempotent on `tag`."""
