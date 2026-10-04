from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..domain import Side


class BrokerError(RuntimeError):
    """The broker refused or failed in a way that is known: nothing was placed."""


class BrokerUncertain(BrokerError):
    """We cannot tell whether an order reached the broker (timeout, dropped connection).
    NEVER retry blindly: a human checks the broker's order book first."""


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    status: str            # FILLED | REJECTED | OPEN (accepted, not filled yet) | CANCELLED
    filled_qty: int | None = None   # defaults to qty when FILLED; may be < qty for a partly filled CANCELLED order
    reason: str = ""

    @property
    def done_qty(self) -> int:
        return self.qty if self.filled_qty is None and self.status == "FILLED" else (self.filled_qty or 0)


class Broker(ABC):
    supports_exchange_stops = False   # True: the pipeline expects a protective stop at the exchange after every entry

    @abstractmethod
    def place_order(self, *, symbol: str, side: Side, qty: int, price: float, tag: str,
                    signal_ts: datetime | None = None) -> Fill:
        """Place a buy/sell. MUST be idempotent on `tag`. May return status OPEN (filled later: see get_order).
        `price` is the reference price; `signal_ts` the bar the decision was made on (paper uses it to find the next open)."""

    # ---- optional capabilities; defaults describe a synchronous broker with no exchange-side stops ----
    def get_order(self, order_id: str) -> Fill:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        return False

    def place_protective_stop(self, *, symbol: str, qty: int, trigger: float, tag: str) -> str | None:
        """Exchange-side stop for a long position. Returns an id, or None if unsupported / not placed."""
        return None

    def stop_status(self, stop_id: str) -> Fill | None:
        """FILLED (with price) if the stop fired and sold, OPEN if still waiting, CANCELLED/REJECTED otherwise."""
        return None

    def cancel_protective_stop(self, stop_id: str) -> bool:
        return False

    def held_quantities(self) -> dict[str, int] | None:
        """symbol -> shares the broker says we hold. None = this broker cannot report (no reconciliation)."""
        return None
