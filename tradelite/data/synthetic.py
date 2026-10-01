"""Deterministic synthetic OHLCV for tests and demos. Proves PLUMBING only — a strategy
'working' on random data means nothing about real markets."""
from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from .base import validate_bars


def make_bars(symbol: str, n: int = 1500, start: str = "2018-01-01", regime_len: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    rets = np.empty(n)
    i = 0
    while i < n:
        length = min(regime_len + int(rng.integers(-30, 30)), n - i)
        drift = rng.choice([-0.0006, 0.0, 0.0009, 0.0015])
        vol = rng.choice([0.008, 0.012, 0.018])
        rets[i : i + length] = rng.normal(drift, vol, length)
        i += length
    close = 100 * np.exp(np.cumsum(rets))
    prev_close = np.concatenate([[100.0], close[:-1]])
    open_ = prev_close * (1 + rng.normal(0, 0.003, n))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    volume = rng.lognormal(13, 0.4, n) * (1 + 2 * (np.abs(rets) > 0.02))
    idx = pd.bdate_range(start, periods=n)
    df = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": volume}, index=idx)
    return validate_bars(df)


class DemoProvider:
    def __init__(self, symbols: list[str] | None = None) -> None:
        self._symbols = symbols or ["DEMO1", "DEMO2", "DEMO3"]

    def symbols(self) -> list[str]:
        return list(self._symbols)

    def get_bars(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame:
        return make_bars(symbol)
