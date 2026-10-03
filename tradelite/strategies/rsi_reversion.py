"""Mean reversion: buy a short-term oversold dip (RSI crosses below a threshold) inside an uptrend."""
from __future__ import annotations

import pandas as pd

from ..domain import Signal
from ..indicators import atr, ema, rsi
from .base import Strategy, StrategyMeta, long_signal


class RsiReversion(Strategy):
    meta = StrategyMeta(
        name="rsi_reversion",
        description="Buy when RSI drops below the oversold level while price is above its trend EMA; ATR stop, R-multiple target.",
        markets=("equity",), timeframes=("1d",), min_bars=120,
    )
    default_params = {"rsi_n": 5, "oversold": 30.0, "trend": 100,
                      "atr_n": 14, "atr_mult": 2.0, "rr": 1.5, "min_avg_volume": 0.0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["rsi"] = rsi(out["close"], p["rsi_n"])
        out["ema_trend"] = ema(out["close"], p["trend"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean()
        below = out["rsi"] < p["oversold"]
        out["dip"] = below & ~below.shift(1, fill_value=False)   # first bar below the level
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        row = df.iloc[i]
        if not bool(row["dip"]) or pd.isna(row["atr"]) or pd.isna(row["ema_trend"]) or pd.isna(row["vol_ma"]):
            return None
        if row["close"] <= row["ema_trend"] or row["vol_ma"] < self.params["min_avg_volume"]:
            return None
        return long_signal(self.meta.name, symbol, df, i, row["atr"], self.params["atr_mult"], self.params["rr"])
