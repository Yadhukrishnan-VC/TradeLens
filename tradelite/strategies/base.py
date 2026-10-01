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


def long_signal(name: str, symbol: str, df: pd.DataFrame, i: int, atr_value: float,
                atr_mult: float, rr: float) -> Signal:
    """Standard long signal: entry estimate = close, ATR-multiple stop, R-multiple target."""
    close = float(df["close"].iloc[i])
    risk = atr_mult * float(atr_value)
    return Signal(name, symbol, Side.BUY, df.index[i].to_pydatetime(),
                  entry=close, stop=close - risk, target=close + rr * risk)
