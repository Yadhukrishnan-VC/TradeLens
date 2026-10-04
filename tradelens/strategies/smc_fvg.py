"""SMC fair value gap: in bullish market structure, buy the first retest of a bullish FVG left by a strong up-candle."""
from __future__ import annotations

import pandas as pd

from ..domain import Signal
from ..indicators import ema
from ..smc import analyze, smc_signal, write_columns, zone_retest_signals
from .base import Strategy, StrategyMeta


class SmcFvg(Strategy):
    meta = StrategyMeta(
        name="smc_fvg",
        description="Bullish structure: buy the first retest of a bullish fair value gap (3-candle imbalance) that holds, with the stop below the gap.",
        markets=("equity", "futures", "forex"), timeframes=("1d",), min_bars=120,
    )
    default_params = {
        "swing_left": 3, "swing_right": 3, "atr_n": 14,
        "min_gap_atr": 0.3,        # the gap must be at least this many ATRs tall
        "min_disp_atr": 1.0,       # the middle candle must be a real up-move: range >= this many ATRs, and close > open
        "max_age": 20,             # the retest must come within this many bars of the gap forming
        "entry_mode": "confirm",   # confirm: close back above the gap top within confirm_bars | touch: enter on the first touch bar
        "confirm_bars": 3,
        "require_rejection": True, # touch mode only: the touch bar must close up (close > open)
        "require_bias": True,      # market structure bullish when the gap formed AND at the retest
        "discount_only": False,    # retest must close in the lower half of the dealing range
        "trend_ema": 0,            # >0: also require close above this EMA
        "stop_buf_atr": 0.1, "min_risk_atr": 0.5, "rr": 2.0,
    }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        fr = analyze(df, left=p["swing_left"], right=p["swing_right"], atr_n=p["atr_n"])
        zones = [z for z in fr.fvgs if z.label == "bull" and z.size_atr >= p["min_gap_atr"] and z.disp_atr >= p["min_disp_atr"]
                 and (z.trend_at_birth == 1 or not p["require_bias"])]
        em = ema(df["close"], p["trend_ema"]).to_numpy() if p["trend_ema"] else None
        res = zone_retest_signals(df, fr, zones, max_age=p["max_age"], entry_mode=p["entry_mode"],
                                  confirm_bars=p["confirm_bars"], require_rejection=p["require_rejection"],
                                  require_bias=p["require_bias"], discount_only=p["discount_only"],
                                  stop_buf_atr=p["stop_buf_atr"], min_risk_atr=p["min_risk_atr"], ema_filter=em)
        return write_columns(df.copy(), res, fr.atr)

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        return smc_signal(self.meta.name, symbol, df, i, self.params["rr"])
