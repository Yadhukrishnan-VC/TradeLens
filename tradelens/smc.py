"""Smart Money Concepts (SMC / ICT-style) as explicit, testable, CAUSAL rules.

Everything here is one forward pass over the bars. Nothing at bar i uses a bar after i. The one place hindsight usually
sneaks in is swing points: a pivot at bar p needs `right` later bars to be a pivot, so it is only KNOWN at bar p+right.
This module registers a pivot at p+right and never earlier.

Definitions (long side only; the bearish mirror is used for market-structure bias, not for trades):

  swing high / low   bar p whose high (low) is strictly beyond the `left` bars before it and at least as far as the `right`
                     bars after it. Known from bar p + right.
  BOS up             a bar CLOSES above the latest unbroken swing high while the structure was already bullish (or undefined)
  CHoCH up           the same break, but the structure was bearish: first sign of a trend change
  BOS / CHoCH down   mirror image on the latest unbroken swing low (used for bias only)
  trend              +1 after an up-break, -1 after a down-break, 0 before the first break
  FVG (bullish)      three candles j-2, j-1, j with low[j] > high[j-2]. The gap is [high[j-2], low[j]]. Known at the close of j.
  order block (bull) when an up-break happens at bar j of swing high p: the candle with the LOWEST LOW in (p, j], i.e. the
                     origin of the leg that broke structure. Zone = that candle's [low, high] (or its body). Known at close of j.
  dealing range      [latest swing low, highest high since it]; equilibrium = its midpoint. Close below it = "discount".
  retest             the FIRST bar at/after the zone's birth whose low trades into the zone (low <= zone top). A zone is used
                     once. Entry is either on a close back above the zone's top within a few bars ("confirm", default) or on
                     the touch bar itself ("touch"); see zone_retest_signals.

This is one reasonable reading of concepts that traders define differently. The rules above are the definitions; change them
here (and in the tests) if yours differ.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import atr


@dataclass(frozen=True)
class Zone:
    kind: str            # "fvg" | "ob"
    born: int            # first bar on which the zone may be traded (the bar AFTER the one that created it)
    lo: float
    hi: float
    origin: int          # bar index of the candle that defines it (FVG: j-2; OB: the lowest-low candle)
    label: str           # fvg: "bull"; ob: "bos" | "choch"
    size_atr: float      # fvg: gap size / ATR; ob: leg height / ATR
    disp_atr: float      # fvg: middle-candle range / ATR (displacement); ob: 0
    trend_at_birth: int  # market-structure trend once the creating bar's break (if any) was applied
    created: int         # bar index at whose close the zone became known


@dataclass
class SmcFrame:
    atr: np.ndarray
    trend: np.ndarray            # -1 / 0 / +1 per bar
    eq: np.ndarray               # equilibrium of the dealing range per bar (NaN if undefined)
    bos_up: np.ndarray
    choch_up: np.ndarray
    bos_down: np.ndarray
    choch_down: np.ndarray
    swing_lows: list[tuple[int, float, int]] = field(default_factory=list)    # (pivot bar, price, bar it became known)
    swing_highs: list[tuple[int, float, int]] = field(default_factory=list)
    fvgs: list[Zone] = field(default_factory=list)
    obs: list[Zone] = field(default_factory=list)


def analyze(df: pd.DataFrame, *, left: int = 3, right: int = 3, atr_n: int = 14, ob_body: bool = False) -> SmcFrame:
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(df)
    a = atr(df, atr_n).to_numpy(dtype=float)
    fr = SmcFrame(atr=a, trend=np.zeros(n, dtype=np.int8), eq=np.full(n, np.nan), bos_up=np.zeros(n, bool),
                  choch_up=np.zeros(n, bool), bos_down=np.zeros(n, bool), choch_down=np.zeros(n, bool))
    last_sh: tuple[float, int] | None = None
    last_sl: tuple[float, int] | None = None
    sh_broken = sl_broken = True
    trend = 0
    run_hi = np.nan                      # highest high since the latest swing low

    for i in range(n):
        # 1) pivots that become known at i (pivot bar p = i - right)
        p = i - right
        if p - left >= 0:
            if h[p] > h[p - left:p].max() and h[p] >= h[p + 1:i + 1].max():
                last_sh, sh_broken = (h[p], p), False
                fr.swing_highs.append((p, h[p], i))
            if l[p] < l[p - left:p].min() and l[p] <= l[p + 1:i + 1].min():
                last_sl, sl_broken = (l[p], p), False
                fr.swing_lows.append((p, l[p], i))
                run_hi = h[p:i + 1].max()
            else:
                run_hi = max(run_hi, h[i]) if not np.isnan(run_hi) else run_hi
        elif not np.isnan(run_hi):
            run_hi = max(run_hi, h[i])

        # 2) structure breaks on the CLOSE of bar i
        if last_sh is not None and not sh_broken and c[i] > last_sh[0]:
            (fr.choch_up if trend == -1 else fr.bos_up)[i] = True
            sh_idx = last_sh[1]
            m = sh_idx + 1 + int(np.argmin(l[sh_idx + 1:i + 1]))                    # origin of the leg: lowest low since the swing high
            lo_, hi_ = (min(o[m], c[m]), max(o[m], c[m])) if ob_body else (l[m], h[m])
            leg = h[m:i + 1].max() - l[m]
            fr.obs.append(Zone("ob", i + 1, float(lo_), float(hi_), m, "choch" if trend == -1 else "bos",
                               float(leg / a[i]) if a[i] > 0 else 0.0, 0.0, 1, i))
            trend, sh_broken = 1, True
        if last_sl is not None and not sl_broken and c[i] < last_sl[0]:
            (fr.choch_down if trend == 1 else fr.bos_down)[i] = True
            trend, sl_broken = -1, True
        fr.trend[i] = trend

        # 3) bullish fair value gap completed by bar i
        if i >= 2 and l[i] > h[i - 2] and a[i] > 0:
            mid_range = h[i - 1] - l[i - 1]
            fr.fvgs.append(Zone("fvg", i + 1, float(h[i - 2]), float(l[i]), i - 2, "bull" if c[i - 1] > o[i - 1] else "bull_weak",
                                float((l[i] - h[i - 2]) / a[i]), float(mid_range / a[i]), trend, i))

        # 4) dealing range / equilibrium
        if last_sl is not None and not np.isnan(run_hi):
            fr.eq[i] = (run_hi + last_sl[0]) / 2
    return fr


def first_touch(low: np.ndarray, z: Zone, max_age: int) -> int | None:
    """First bar in [born, born+max_age] whose low trades into the zone (low <= zone top)."""
    end = min(len(low) - 1, z.born + max_age)
    if z.born > end:
        return None
    hits = np.nonzero(low[z.born:end + 1] <= z.hi)[0]
    return int(z.born + hits[0]) if hits.size else None


def zone_retest_signals(df: pd.DataFrame, fr: SmcFrame, zones: list[Zone], *, max_age: int = 20, entry_mode: str = "confirm",
                        confirm_bars: int = 3, require_rejection: bool = True, require_bias: bool = True,
                        discount_only: bool = False, stop_buf_atr: float = 0.1, min_risk_atr: float = 0.5,
                        ema_filter: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Turn zones into long signals. Each zone is used once, starting at its FIRST retest bar t0 (low trades into the zone).

    entry_mode="confirm" (default): wait up to `confirm_bars` bars after t0 (t0 included) for a bar that CLOSES back above
        the zone's top. The signal is that bar's close. A close at/below the zone's bottom first spends the zone.
        The stop goes below the lower of the zone bottom and the lowest low since t0.
    entry_mode="touch": signal at t0 itself if it closes above the zone bottom, and (require_rejection) closes up.
        The stop goes below the zone bottom.

    Also required at the signal bar: bullish structure (require_bias), inside the discount half of the dealing range
    (discount_only), above an EMA (ema_filter). The stop is never closer than min_risk_atr ATRs to the close.
    Only bars <= the signal bar are consulted, so the result is causal."""
    if entry_mode not in ("confirm", "touch"):
        raise ValueError("entry_mode must be confirm or touch")
    o, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "low", "close"))
    n = len(df)
    sig, stop = np.zeros(n, bool), np.full(n, np.nan)
    zlo, zhi = np.full(n, np.nan), np.full(n, np.nan)
    for z in sorted(zones, key=lambda z: z.born):
        t0 = first_touch(l, z, max_age)
        if t0 is None:
            continue
        last = t0 if entry_mode == "touch" else min(n - 1, t0 + confirm_bars)
        low_since = np.inf
        for t in range(t0, last + 1):
            low_since = min(low_since, l[t])
            if c[t] <= z.lo:
                break                                              # closed through the zone: spent
            if entry_mode == "confirm" and not c[t] > z.hi:
                continue                                           # still inside the zone: keep waiting
            if entry_mode == "touch" and require_rejection and not c[t] > o[t]:
                break
            a = fr.atr[t]
            if sig[t] or not (a > 0):
                break
            if require_bias and fr.trend[t] != 1:
                break
            if discount_only and not (not np.isnan(fr.eq[t]) and c[t] <= fr.eq[t]):
                break
            if ema_filter is not None and not c[t] > ema_filter[t]:
                break
            s = min(z.lo, low_since) - stop_buf_atr * a
            if c[t] - s < min_risk_atr * a:
                s = c[t] - min_risk_atr * a
            sig[t], stop[t], zlo[t], zhi[t] = True, s, z.lo, z.hi
            break
    return {"sig": sig, "stop": stop, "zlo": zlo, "zhi": zhi}


def smc_signal(name: str, symbol: str, df: pd.DataFrame, i: int, rr: float):
    """Long signal from the columns the SMC strategies' prepare() writes: entry = close, stop below the zone/sweep."""
    from .domain import Side, Signal
    row = df.iloc[i]
    if not bool(row["smc_sig"]):
        return None
    close, stop = float(row["close"]), float(row["smc_stop"])
    if not stop < close:
        return None
    return Signal(name, symbol, Side.BUY, df.index[i].to_pydatetime(), entry=close, stop=stop, target=close + rr * (close - stop))


def write_columns(out: pd.DataFrame, res: dict[str, np.ndarray], atr_values: np.ndarray) -> pd.DataFrame:
    out["atr"] = atr_values
    out["smc_sig"] = res["sig"]
    out["smc_stop"] = res["stop"]
    out["smc_zlo"] = res["zlo"]
    out["smc_zhi"] = res["zhi"]
    return out
