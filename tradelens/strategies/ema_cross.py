"""Trend-following: fast EMA crosses above slow EMA while price is above the long-term trend EMA."""
from __future__ import annotations

import pandas as pd

from ..domain import Side, Signal
from ..indicators import atr, ema
from .base import ExitHint, Strategy, StrategyMeta, WatchNote, watch_note


class EmaCross(Strategy):
    meta = StrategyMeta(
        name="ema_cross",
        description="Fast/slow EMA cross-up in an uptrend; ATR stop, fixed R-multiple target.",
        markets=("equity", "futures", "forex"),
        timeframes=("1d",),
        min_bars=210, regimes=("up",),
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

    def watch(self, df, i, symbol):
        p, row = self.params, df.iloc[i]
        if pd.isna(row["ema_trend"]) or i < 1 or row["close"] <= row["ema_trend"]:
            return None
        ef, es, ef0, es0 = row["ema_fast"], row["ema_slow"], df["ema_fast"].iloc[i - 1], df["ema_slow"].iloc[i - 1]
        if ef >= es or (ef - es) <= (ef0 - es0):          # already crossed, or not converging
            return None
        a_f, a_s = 2 / (p["fast"] + 1), 2 / (p["slow"] + 1)
        need = ((1 - a_s) * es - (1 - a_f) * ef) / (a_f - a_s)   # next close at which fast EMA > slow EMA
        if need <= 0 or need / row["close"] - 1 > 0.03:
            return None
        return watch_note(f"fast EMA ({p['fast']}) is below slow EMA ({p['slow']}) and closing in; a close above this level completes the cross-up", need, row["close"])

    def exit_hint(self, df, i, entry_price):
        row = df.iloc[i]
        if row["ema_fast"] < row["ema_slow"]:
            return ExitHint("EXIT", "fast EMA has fallen below slow EMA: the trend that triggered the entry has turned")
        return None
