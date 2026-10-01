"""Momentum: first close at a new N-day high (default ~52 weeks) with the fast trend intact."""
from __future__ import annotations

import pandas as pd

from ..domain import Signal
from ..indicators import atr, ema
from .base import Strategy, StrategyMeta, long_signal


class NewHighMomentum(Strategy):
    meta = StrategyMeta(
        name="new_high_momentum",
        description="First close above the prior 52-week (N-bar) high while price is above its trend EMA.",
        markets=("equity",), timeframes=("1d",), min_bars=260,
    )
    default_params = {"lookback": 250, "trend": 50, "atr_n": 14, "atr_mult": 2.5, "rr": 3.0, "min_avg_volume": 0.0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["prior_high"] = out["close"].rolling(p["lookback"]).max().shift(1)
        new_high = out["close"] > out["prior_high"]
        out["new_high"] = new_high & ~new_high.shift(1, fill_value=False)
        out["ema_trend"] = ema(out["close"], p["trend"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean()
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        row = df.iloc[i]
        if not bool(row["new_high"]) or pd.isna(row["atr"]) or pd.isna(row["ema_trend"]) or pd.isna(row["vol_ma"]):
            return None
        if row["close"] <= row["ema_trend"] or row["vol_ma"] < self.params["min_avg_volume"]:
            return None
        return long_signal(self.meta.name, symbol, df, i, row["atr"], self.params["atr_mult"], self.params["rr"])
