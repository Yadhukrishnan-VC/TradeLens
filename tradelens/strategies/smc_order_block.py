"""SMC order block: buy the first retest of the candle that launched a structure break."""
from __future__ import annotations

import pandas as pd

from ..domain import Signal
from ..indicators import ema
from ..smc import analyze, smc_signal, write_columns, zone_retest_signals
from .base import Strategy, StrategyMeta


class SmcOrderBlock(Strategy):
    meta = StrategyMeta(
        name="smc_order_block",
        description="Buy the first retest of a bullish order block (the origin candle of the leg that broke a swing high) in bullish structure.",
        markets=("equity", "futures", "forex"), timeframes=("1d",), min_bars=120,
    )
    default_params = {
        "swing_left": 3, "swing_right": 3, "atr_n": 14,
        "mode": "bos",             # bos = continuation breaks | choch = trend-change breaks | any
        "min_leg_atr": 2.0,        # the impulse leg (order block low to leg high) must be at least this many ATRs
        "ob_body": False,          # False: whole candle [low, high]; True: candle body only
        "require_fvg": False,      # the impulse must also have left a bullish FVG (a stricter, rarer setup)
        "max_age": 30,
        "entry_mode": "confirm",   # confirm: close back above the zone top within confirm_bars | touch: enter on the first touch bar
        "confirm_bars": 3,
        "require_rejection": True, "require_bias": True, "discount_only": False, "trend_ema": 0,
        "stop_buf_atr": 0.1, "min_risk_atr": 0.5, "rr": 2.0,
    }

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        if p["mode"] not in ("bos", "choch", "any"):
            raise ValueError("mode must be bos, choch or any")
        fr = analyze(df, left=p["swing_left"], right=p["swing_right"], atr_n=p["atr_n"], ob_body=p["ob_body"])
        zones = []
        for z in fr.obs:
            if z.size_atr < p["min_leg_atr"] or (p["mode"] != "any" and z.label != p["mode"]):
                continue
            if p["require_fvg"] and not any(z.origin < f.created <= z.created and f.label == "bull" for f in fr.fvgs):
                continue
            zones.append(z)
        em = ema(df["close"], p["trend_ema"]).to_numpy() if p["trend_ema"] else None
        res = zone_retest_signals(df, fr, zones, max_age=p["max_age"], entry_mode=p["entry_mode"],
                                  confirm_bars=p["confirm_bars"], require_rejection=p["require_rejection"],
                                  require_bias=p["require_bias"], discount_only=p["discount_only"],
                                  stop_buf_atr=p["stop_buf_atr"], min_risk_atr=p["min_risk_atr"], ema_filter=em)
        return write_columns(df.copy(), res, fr.atr)

    def on_bar(self, df: pd.DataFrame, i: int, symbol: str) -> Signal | None:
        return smc_signal(self.meta.name, symbol, df, i, self.params["rr"])
