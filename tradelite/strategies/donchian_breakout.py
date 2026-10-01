"""Breakout: close above the prior N-bar high on above-average volume, in an uptrend."""
from __future__ import annotations

import pandas as pd

from ..domain import Side, Signal
from ..indicators import atr, ema
from .base import Strategy, StrategyMeta


class DonchianBreakout(Strategy):
    meta = StrategyMeta(
        name="donchian_breakout",
        description="First close above prior N-bar high with volume confirmation; ATR stop, R-multiple target.",
        markets=("equity", "futures", "forex"),
        timeframes=("1d",),
        min_bars=120,
    )
    default_params = {
        "lookback": 20, "trend": 100, "vol_mult": 1.2,
        "atr_n": 14, "atr_mult": 2.0, "rr": 2.5,
        "min_avg_volume": 0.0,
    }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["prior_high"] = out["high"].rolling(p["lookback"]).max().shift(1)
        out["ema_trend"] = ema(out["close"], p["trend"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean().shift(1)
        breakout = out["close"] > out["prior_high"]
        out["breakout"] = breakout & ~breakout.shift(1, fill_value=False)
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        p = self.params
        row = df.iloc[i]
        if not bool(row["breakout"]):
            return None
        if pd.isna(row["atr"]) or pd.isna(row["vol_ma"]) or pd.isna(row["ema_trend"]):
            return None
        if row["close"] <= row["ema_trend"]:
            return None
        if row["volume"] < p["vol_mult"] * row["vol_ma"] or row["vol_ma"] < p["min_avg_volume"]:
            return None
        risk = p["atr_mult"] * float(row["atr"])
        close = float(row["close"])
        return Signal(
            self.meta.name, symbol, Side.BUY, df.index[i].to_pydatetime(),
            entry=close, stop=close - risk, target=close + p["rr"] * risk,
        )
