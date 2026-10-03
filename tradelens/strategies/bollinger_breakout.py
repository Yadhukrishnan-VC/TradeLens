"""Volatility breakout: close above the upper Bollinger band after a low-volatility squeeze."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..domain import Signal
from ..indicators import atr, ema
from .base import ExitHint, Strategy, StrategyMeta, WatchNote, long_signal, watch_note


class BollingerBreakout(Strategy):
    meta = StrategyMeta(
        name="bollinger_breakout",
        description="First close above the upper Bollinger band shortly after a volatility squeeze, in an uptrend.",
        markets=("equity", "futures", "forex"), timeframes=("1d",), min_bars=150, regimes=("up", "sideways"),
    )
    default_params = {"bb_n": 20, "bb_k": 2.0, "squeeze_pct": 0.35, "squeeze_window": 100, "squeeze_lag": 10,
                      "trend": 100, "atr_n": 14, "atr_mult": 2.0, "rr": 2.5, "min_avg_volume": 0.0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        mid = out["close"].rolling(p["bb_n"]).mean()
        sd = out["close"].rolling(p["bb_n"]).std(ddof=0)
        out["upper"] = mid + p["bb_k"] * sd
        width = (2 * p["bb_k"] * sd) / mid
        rank = width.rolling(p["squeeze_window"]).apply(lambda w: (w[:-1] < w[-1]).mean() if len(w) > 1 else float("nan"), raw=True)
        squeezed = rank <= p["squeeze_pct"]
        out["squeezed"] = squeezed
        out["recent_squeeze"] = squeezed.rolling(p["squeeze_lag"]).max().shift(1).fillna(0).astype(bool)
        above = out["close"] > out["upper"]
        out["breakout"] = above & ~above.shift(1, fill_value=False)
        out["ema_trend"] = ema(out["close"], p["trend"])
        out["atr"] = atr(out, p["atr_n"])
        out["vol_ma"] = out["volume"].rolling(20).mean()
        return out

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        row = df.iloc[i]
        if not (bool(row["breakout"]) and bool(row["recent_squeeze"])):
            return None
        if pd.isna(row["atr"]) or pd.isna(row["ema_trend"]) or pd.isna(row["vol_ma"]):
            return None
        if row["close"] <= row["ema_trend"] or row["vol_ma"] < self.params["min_avg_volume"]:
            return None
        return long_signal(self.meta.name, symbol, df, i, row["atr"], self.params["atr_mult"], self.params["rr"])

    def watch(self, df, i, symbol):
        p, row = self.params, df.iloc[i]
        n, k = p["bb_n"], p["bb_k"]
        if i < n or pd.isna(row["upper"]) or pd.isna(row["ema_trend"]) or row["close"] <= row["ema_trend"] or row["close"] > row["upper"]:
            return None
        if not bool(df["squeezed"].iloc[i - p["squeeze_lag"] + 1: i + 1].any()):   # the squeeze window the NEXT bar will look at
            return None
        base = df["close"].iloc[i - n + 2: i + 1].to_numpy()

        def margin(c: float) -> float:        # >0 once a close at c would sit above its own (moved) upper band
            w = np.append(base, c)
            return c - (w.mean() + k * w.std())

        lo, hi = float(row["close"]), float(row["close"]) * 1.015
        if margin(hi) <= 0:
            return None                      # more than 1.5% away: not "close"
        if margin(lo) > 0:
            return watch_note("after a volatility squeeze, even a flat close would break above the upper Bollinger band", lo, row["close"])
        for _ in range(40):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if margin(mid) <= 0 else (lo, mid)
        return watch_note("after a volatility squeeze, price is near the upper Bollinger band; a close above this level triggers", hi, row["close"])

    def exit_hint(self, df, i, entry_price):
        n = self.params["bb_n"]
        if i < n:
            return None
        mid = float(df["close"].iloc[i - n + 1: i + 1].mean())
        if df["close"].iloc[i] < mid:
            return ExitHint("EXIT", f"closed back below the {n}-day average ({mid:.2f}): the breakout has faded")
        return None
