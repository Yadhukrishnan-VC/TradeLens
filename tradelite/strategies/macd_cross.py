"""Momentum: MACD line crosses above its signal line while price is above the trend EMA."""
from __future__ import annotations

import pandas as pd

from ..domain import Signal
from ..indicators import atr, ema
from .base import Strategy, StrategyMeta, long_signal


class MacdCross(Strategy):
    meta = StrategyMeta(
        name="macd_cross",
        description="MACD crosses above its signal line in an uptrend; ATR stop, R-multiple target.",
        markets=("equity", "futures", "forex"), timeframes=("1d",), min_bars=120,
    )
    default_params = {"fast": 12, "slow": 26, "signal": 9, "trend": 100,
                      "atr_n": 14, "atr_mult": 2.0, "rr": 2.0, "min_avg_volume": 0.0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        macd = ema(out["close"], p["fast"]) - ema(out["close"], p["slow"])
        sig = ema(macd, p["signal"])
        out["macd"], out["macd_sig"] = macd, sig
        out["cross_up"] = (macd > sig) & (macd.shift(1) <= sig.shift(1))
        out["ema_trend"] = ema(out["close"], p["trend"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean()
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        row = df.iloc[i]
        if not bool(row["cross_up"]) or pd.isna(row["atr"]) or pd.isna(row["ema_trend"]) or pd.isna(row["vol_ma"]):
            return None
        if row["close"] <= row["ema_trend"] or row["vol_ma"] < self.params["min_avg_volume"]:
            return None
        return long_signal(self.meta.name, symbol, df, i, row["atr"], self.params["atr_mult"], self.params["rr"])
