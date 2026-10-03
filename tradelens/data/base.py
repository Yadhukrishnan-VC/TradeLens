from __future__ import annotations

from typing import Protocol

import pandas as pd

REQUIRED = ["open", "high", "low", "close", "volume"]


class DataQualityError(ValueError):
    pass


class DataProvider(Protocol):
    def symbols(self) -> list[str]: ...
    def get_bars(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame: ...


def validate_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Reject bad data loudly. Garbage in = fake edge out."""
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise DataQualityError(f"missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataQualityError("index must be a DatetimeIndex")
    if df.empty:
        raise DataQualityError("no bars")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise DataQualityError("index must be strictly increasing with no duplicates")
    if df[REQUIRED].isna().any().any():
        raise DataQualityError("NaN values in OHLCV")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise DataQualityError("non-positive prices")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any() or (
        df["low"] > df[["open", "close", "high"]].min(axis=1)
    ).any():
        raise DataQualityError("OHLC inconsistency (high/low do not bound open/close)")
    if (df["volume"] < 0).any():
        raise DataQualityError("negative volume")
    return df
