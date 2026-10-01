from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Mode(str, Enum):
    SIGNAL_ONLY = "signal_only"  # record signals, never create orders
    SEMI_AUTO = "semi_auto"      # create orders that wait for human approval
    AUTO = "auto"                # send approved-by-risk orders straight to the broker


@dataclass(frozen=True)
class Signal:
    """A strategy's decision made at the CLOSE of bar `ts`. Entry is an estimate;
    the backtester fills at the next bar's open, live fills at market."""

    strategy: str
    symbol: str
    side: Side
    ts: datetime
    entry: float
    stop: float
    target: float | None = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def rr(self) -> float | None:
        if self.target is None or self.risk_per_unit == 0:
            return None
        return abs(self.target - self.entry) / self.risk_per_unit

    def stop_is_valid(self) -> bool:
        return self.stop < self.entry if self.side is Side.BUY else self.stop > self.entry


@dataclass
class AccountState:
    equity: float
    cash: float                      # capital available for new notional
    open_positions: int
    exposure: float                  # cost-basis notional of open positions
    realized_pnl_today: float
    open_symbols: frozenset[str] = field(default_factory=frozenset)
    kill_switch: bool = False
