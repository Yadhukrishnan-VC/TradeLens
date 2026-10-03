"""Which symbols may be traded on a given day.

A backtest that only ever trades today's index members is biased upward: companies that were
dropped or delisted along the way are missing, and today's members are the ones that did well.
A Universe answers "who was tradable on this date?" using only information available that day.

  StaticUniverse     a fixed list. No membership history, so survivorship bias is UNCONTROLLED.
  IntervalUniverse   real membership history from a CSV (symbol,start,end). This is the honest one,
                     but only as good as the CSV and the price data you hold for delisted names.
  LiquidityUniverse  each day, the top-N most liquid symbols of the pool by trailing traded value
                     (yesterday's data at most). Removes hindsight in *which* names are picked, but
                     cannot repair a pool that already contains only survivors.
  CombinedUniverse   intersection of the above.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd


class Universe(Protocol):
    name: str
    has_membership_history: bool

    def eligible(self, ts: pd.Timestamp) -> set[str]: ...


class StaticUniverse:
    has_membership_history = False

    def __init__(self, symbols: list[str]) -> None:
        self.name = "static"
        self.symbols = sorted({s.strip().upper() for s in symbols if s.strip()})
        self._set = set(self.symbols)

    def eligible(self, ts: pd.Timestamp) -> set[str]:
        return set(self._set)


class IntervalUniverse:
    """Membership intervals, inclusive on both ends. `end` blank = still a member.
    A symbol may appear on several rows (removed, later re-added)."""
    has_membership_history = True

    def __init__(self, intervals: dict[str, list[tuple[pd.Timestamp, pd.Timestamp | None]]], name: str = "intervals") -> None:
        self.name = name
        self.intervals = intervals
        self.symbols = sorted(intervals)

    @classmethod
    def from_csv(cls, path: str | Path) -> "IntervalUniverse":
        df = pd.read_csv(path, dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]
        missing = {"symbol", "start", "end"} - set(df.columns)
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)} (need symbol,start,end)")
        out: dict[str, list[tuple[pd.Timestamp, pd.Timestamp | None]]] = {}
        for n, r in enumerate(df.itertuples(index=False), start=2):
            sym = str(r.symbol).strip().upper()
            if not sym:
                raise ValueError(f"{path} line {n}: empty symbol")
            try:
                a = pd.Timestamp(str(r.start).strip())
                b = pd.Timestamp(str(r.end).strip()) if str(r.end).strip() else None
            except ValueError as e:
                raise ValueError(f"{path} line {n}: bad date ({e})") from e
            if pd.isna(a) or (b is not None and b < a):
                raise ValueError(f"{path} line {n}: start must be a date and end must not be before start")
            out.setdefault(sym, []).append((a, b))
        if not out:
            raise ValueError(f"{path}: no rows")
        return cls(out, name=Path(path).name)

    def eligible(self, ts: pd.Timestamp) -> set[str]:
        return {s for s, ivs in self.intervals.items() if any(a <= ts and (b is None or ts <= b) for a, b in ivs)}


class LiquidityUniverse:
    """Top-N by trailing average traded value (close x volume), computed from PRIOR days only.
    A symbol needs `min_history` bars before it can qualify (keeps just-listed names out)."""
    has_membership_history = False

    def __init__(self, frames: dict[str, pd.DataFrame], top_n: int, lookback: int = 60, min_history: int = 250) -> None:
        if top_n < 1:
            raise ValueError("top_n must be >= 1")
        self.name = f"liquid_top_{top_n}"
        value = pd.DataFrame({s: f["close"] * f["volume"] for s, f in frames.items()}).sort_index()
        avg = value.rolling(lookback, min_periods=max(1, int(lookback * 0.8))).mean().shift(1)
        seasoned = value.notna().cumsum().shift(1) >= min_history
        self._rank = avg.where(seasoned).rank(axis=1, ascending=False, method="first")
        self.top_n = top_n

    def eligible(self, ts: pd.Timestamp) -> set[str]:
        pos = self._rank.index.get_indexer([ts], method="pad")[0]
        if pos < 0:
            return set()
        row = self._rank.iloc[pos]
        return set(row.index[(row <= self.top_n).to_numpy()])


class CombinedUniverse:
    def __init__(self, *parts: Universe) -> None:
        if not parts:
            raise ValueError("need at least one universe")
        self.parts = parts
        self.name = " & ".join(p.name for p in parts)
        self.has_membership_history = any(p.has_membership_history for p in parts)
        self.symbols = sorted(set.intersection(*(set(p.symbols) for p in parts if hasattr(p, "symbols")))) \
            if any(hasattr(p, "symbols") for p in parts) else []

    def eligible(self, ts: pd.Timestamp) -> set[str]:
        sets = [p.eligible(ts) for p in self.parts]
        return set.intersection(*sets)


def survivorship_report(frames: dict[str, pd.DataFrame], universe: Universe | None, start: pd.Timestamp,
                        end: pd.Timestamp, requested: list[str] | None = None) -> dict:
    """Plain-language warnings about how far the results can be trusted on survivorship grounds."""
    tol = timedelta(days=7)
    ended = sorted(s for s, f in frames.items() if f.index[-1] < end - tol)
    late = sorted(s for s, f in frames.items() if f.index[0] > start + tol)
    static = universe is None or not universe.has_membership_history
    warnings: list[str] = []
    if static:
        warnings.append("No membership history: the symbols were picked with today's knowledge, so past results are "
                        "biased upward (dropped and delisted companies are missing).")
        if not ended:
            warnings.append("Every symbol has data up to the end date, so the sample contains survivors only.")
    missing = []
    if universe is not None and hasattr(universe, "symbols"):
        missing = sorted(set(universe.symbols) - set(frames))
        if missing:
            warnings.append(f"{len(missing)} universe member(s) have no price data and cannot be traded "
                            f"(typically delisted names a free source no longer serves): {', '.join(missing[:10])}"
                            f"{' ...' if len(missing) > 10 else ''}. Results still lean toward survivors.")
    if requested is not None:
        unloaded = sorted(set(requested) - set(frames) - set(missing))
        if unloaded:
            warnings.append(f"{len(unloaded)} requested symbol(s) had no stored prices: {', '.join(unloaded[:10])}.")
    if ended:
        warnings.append(f"{len(ended)} symbol(s) stop trading before the end date; open positions in them are closed "
                        f"at the last available close, which is optimistic for a collapse or bankruptcy.")
    if static and not ended:
        risk = "high"
    elif static or missing:
        risk = "moderate"
    else:
        risk = "lower"
    return {"universe": universe.name if universe else "static (all loaded symbols)", "bias_risk": risk,
            "n_symbols": len(frames), "n_ended_early": len(ended), "ended_early": ended,
            "n_late_start": len(late), "missing_price_data": missing, "warnings": warnings}
