"""SMC liquidity sweep: price stabs below a swing low (taking the stops resting there) and closes back above it."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..domain import Signal
from ..indicators import ema
from ..smc import analyze, smc_signal, write_columns
from .base import Strategy, StrategyMeta


class SmcSweep(Strategy):
    meta = StrategyMeta(
        name="smc_sweep",
        description="Sell-side liquidity sweep: a bar pierces an untouched swing low or the N-bar low, closes back above it with a long lower wick; stop below the sweep low.",
        markets=("equity", "futures", "forex"), timeframes=("1d",), min_bars=120,
    )
    default_params = {
        "swing_left": 3, "swing_right": 3, "atr_n": 14,
        "pool": "both",            # which liquidity to hunt: swing (swing lows) | range (lowest low of the last range_n bars) | both
        "range_n": 20,
        "lookback": 60,            # swing lows only: formed within this many bars
        "min_age": 5,              # ... and at least this old
        "n_levels": 5,             # consider the most recent N such swing lows
        "wick_min": 0.35,          # lower wick (below the body) must be at least this fraction of the bar's range
        "min_depth_atr": 0.1,      # pierce the level by at least this many ATRs ...
        "max_depth_atr": 2.0,      # ... but not by more than this (a crash through is not a sweep)
        "bias": "any",             # any | not_bearish | bullish   (market structure at the sweep bar; a range-low sweep mostly happens while structure is bearish)
        "discount_only": False, "trend_ema": 0,
        "stop_buf_atr": 0.1, "min_risk_atr": 0.5, "rr": 2.0,
    }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        if p["bias"] not in ("not_bearish", "bullish", "any"):
            raise ValueError("bias must be not_bearish, bullish or any")
        if p["pool"] not in ("swing", "range", "both"):
            raise ValueError("pool must be swing, range or both")
        fr = analyze(df, left=p["swing_left"], right=p["swing_right"], atr_n=p["atr_n"])
        o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        n = len(df)
        em = ema(df["close"], p["trend_ema"]).to_numpy() if p["trend_ema"] else None
        sig, stop = np.zeros(n, bool), np.full(n, np.nan)
        zlo, zhi = np.full(n, np.nan), np.full(n, np.nan)
        known = 0                                           # swing lows known by bar t: those with confirm index <= t
        lows = fr.swing_lows
        for t in range(n):
            while known < len(lows) and lows[known][2] <= t:
                known += 1
            a, rng = fr.atr[t], h[t] - l[t]
            if not (a > 0 and rng > 0):
                continue
            tr = fr.trend[t]
            if (p["bias"] == "bullish" and tr != 1) or (p["bias"] == "not_bearish" and tr == -1):
                continue
            if p["discount_only"] and not (not np.isnan(fr.eq[t]) and c[t] <= fr.eq[t]):
                continue
            if em is not None and not c[t] > em[t]:
                continue
            levels: list[float] = []
            if p["pool"] in ("swing", "both"):
                for pidx, level, _conf in reversed(lows[max(0, known - p["n_levels"]):known]):
                    age = t - pidx
                    if p["min_age"] <= age <= p["lookback"] and not (l[pidx + 1:t].size and l[pidx + 1:t].min() < level):
                        levels.append(level)                      # untouched since it formed: the liquidity is still there
            if p["pool"] in ("range", "both") and t >= p["range_n"]:
                levels.append(float(l[t - p["range_n"]:t].min()))
            for level in sorted(levels, reverse=True):            # the highest level pierced is the one that was actually swept
                if not (l[t] < level < c[t]):
                    continue
                depth = (level - l[t]) / a
                if not (p["min_depth_atr"] <= depth <= p["max_depth_atr"]):
                    continue
                if (min(o[t], c[t]) - l[t]) / rng < p["wick_min"]:
                    continue
                s = l[t] - p["stop_buf_atr"] * a
                if c[t] - s < p["min_risk_atr"] * a:
                    s = c[t] - p["min_risk_atr"] * a
                sig[t], stop[t], zlo[t], zhi[t] = True, s, l[t], level
                break
        return write_columns(df.copy(), {"sig": sig, "stop": stop, "zlo": zlo, "zhi": zhi}, fr.atr)

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        return smc_signal(self.meta.name, symbol, df, i, self.params["rr"])
