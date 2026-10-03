"""Strategy contract. One strategy = one file in this folder; the registry auto-discovers it.

prepare(df)  -> copy of df with CAUSAL indicator columns (value at i uses bars <= i only)
on_bar(...)  -> Signal decided at the CLOSE of bar i, or None
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..domain import Side, Signal


@dataclass(frozen=True)
class StrategyMeta:
    name: str
    description: str
    markets: tuple[str, ...] = ("equity",)
    timeframes: tuple[str, ...] = ("1d",)
    min_bars: int = 50   # warm-up bars before the first signal is allowed
    regimes: tuple[str, ...] = ()   # market trends it is made for (up / sideways / down); empty = any


@dataclass(frozen=True)
class WatchNote:
    """A setup that is close to triggering. `trigger` is the price that would complete it (None when the setup is
    not a single price level); distance_pct is trigger vs the last close, signed."""
    note: str
    trigger: float | None = None
    distance_pct: float | None = None


@dataclass(frozen=True)
class ExitHint:
    """What the strategy itself thinks about an open position. EXIT or REDUCE. Advice only."""
    action: str          # EXIT | REDUCE
    reason: str


def watch_note(note: str, trigger: float, close: float) -> WatchNote:
    return WatchNote(note, round(trigger, 2), round(100.0 * (trigger / close - 1.0), 2))


class Strategy(ABC):
    meta: StrategyMeta
    default_params: dict[str, Any] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"unknown params for {self.meta.name}: {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame: ...

    @abstractmethod
    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None: ...

    def watch(self, df: pd.DataFrame, i: int, symbol: str) -> WatchNote | None:
        """Called on a PREPARED frame when on_bar() found nothing: is a signal one step away? Default: no."""
        return None

    def exit_hint(self, df: pd.DataFrame, i: int, entry_price: float) -> ExitHint | None:
        """Called on a PREPARED frame for an open long position: has the idea behind the entry stopped
        working? Default: no opinion (the stop and target still apply)."""
        return None


def long_signal(name: str, symbol: str, df: pd.DataFrame, i: int, atr_value: float,
                atr_mult: float, rr: float) -> Signal:
    """Standard long signal: entry estimate = close, ATR-multiple stop, R-multiple target."""
    close = float(df["close"].iloc[i])
    risk = atr_mult * float(atr_value)
    return Signal(name, symbol, Side.BUY, df.index[i].to_pydatetime(),
                  entry=close, stop=close - risk, target=close + rr * risk)
