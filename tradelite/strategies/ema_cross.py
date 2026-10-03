"""Trend-following: fast EMA crosses above slow EMA while price is above the long-term trend EMA."""
from __future__ import annotations

import pandas as pd

from ..domain import Side, Signal
from ..indicators import atr, ema
from .base import Strategy, StrategyMeta


class EmaCross(Strategy):
    meta = StrategyMeta(
        name="ema_cross",
        description="Fast/slow EMA cross-up in an uptrend; ATR stop, fixed R-multiple target.",
        markets=("equity", "futures", "forex"),
        timeframes=("1d",),
        min_bars=210,
    )
    default_params = {
        "fast": 20, "slow": 50, "trend": 200,
        "atr_n": 14, "atr_mult": 2.0, "rr": 2.0,
        "min_avg_volume": 0.0,   # liquidity filter on 20-bar average volume
    }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["ema_fast"] = ema(out["close"], p["fast"])
        out["ema_slow"] = ema(out["close"], p["slow"])
        out["ema_trend"] = ema(out["close"], p["trend"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean()
        out["cross_up"] = (out["ema_fast"] > out["ema_slow"]) & (
            out["ema_fast"].shift(1) <= out["ema_slow"].shift(1)
        )
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        p = self.params
        row = df.iloc[i]
        if not bool(row["cross_up"]):
            return None
        if pd.isna(row["atr"]) or pd.isna(row["ema_trend"]) or pd.isna(row["vol_ma"]):
            return None
        if row["close"] <= row["ema_trend"] or row["vol_ma"] < p["min_avg_volume"]:
            return None
        risk = p["atr_mult"] * float(row["atr"])
        close = float(row["close"])
        return Signal(
            self.meta.name, symbol, Side.BUY, df.index[i].to_pydatetime(),
            entry=close, stop=close - risk, target=close + p["rr"] * risk,
        )
