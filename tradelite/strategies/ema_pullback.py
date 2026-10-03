"""Trend pullback: in an uptrend, buy the bar that dips to the fast EMA and closes back above it."""
from __future__ import annotations

import pandas as pd

from ..domain import Signal
from ..indicators import atr, ema
from .base import ExitHint, Strategy, StrategyMeta, WatchNote, long_signal, watch_note


class EmaPullback(Strategy):
    meta = StrategyMeta(
        name="ema_pullback",
        description="Uptrend (fast EMA above slow EMA): buy a dip that touches the fast EMA and closes above it.",
        markets=("equity", "futures", "forex"), timeframes=("1d",), min_bars=120, regimes=("up",),
    )
    default_params = {"fast": 20, "slow": 50, "atr_n": 14, "atr_mult": 2.0, "rr": 2.0, "min_avg_volume": 0.0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        out["ema_fast"] = ema(out["close"], p["fast"])
        out["ema_slow"] = ema(out["close"], p["slow"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean()
        touch = (out["low"] <= out["ema_fast"]) & (out["close"] > out["ema_fast"]) & (out["ema_fast"] > out["ema_slow"])
        out["pullback"] = touch & ~touch.shift(1, fill_value=False)
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        row = df.iloc[i]
        if not bool(row["pullback"]) or pd.isna(row["atr"]) or pd.isna(row["vol_ma"]):
            return None
        if row["vol_ma"] < self.params["min_avg_volume"]:
            return None
        return long_signal(self.meta.name, symbol, df, i, row["atr"], self.params["atr_mult"], self.params["rr"])

    def watch(self, df, i, symbol):
        row = df.iloc[i]
        if pd.isna(row["atr"]) or row["ema_fast"] <= row["ema_slow"] or row["close"] <= row["ema_fast"]:
            return None
        if row["low"] <= row["ema_fast"]:      # it touched today already: tomorrow cannot be a FIRST touch
            return None
        if row["close"] - row["ema_fast"] > 0.5 * row["atr"]:
            return None
        return watch_note(f"uptrend, price is {(row['close'] - row['ema_fast']):.2f} above the {self.params['fast']}-day EMA; a dip to it that closes back above triggers", float(row["ema_fast"]), row["close"])

    def exit_hint(self, df, i, entry_price):
        row = df.iloc[i]
        if row["close"] < row["ema_slow"]:
            return ExitHint("EXIT", f"closed below the {self.params['slow']}-day EMA: the uptrend is broken")
        if row["close"] < row["ema_fast"]:
            return ExitHint("REDUCE", f"closed below the {self.params['fast']}-day EMA: the pullback is deeper than a dip")
        return None
