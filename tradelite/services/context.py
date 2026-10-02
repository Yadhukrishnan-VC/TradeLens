"""One honest picture of the market and of the account, attached to every signal and shown on the dashboard.

Regime = what the benchmark is doing (trend x volatility) plus how many stocks are healthy (breadth).
It is computed from prices up to the decision bar only (no look-ahead) and with plain rules, not a model:
a rule you can read is a rule you can doubt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.base import DataProvider
from ..domain import AccountState
from ..models import PositionRow
from ..risk.engine import RiskConfig

MIN_BARS = 210            # 200-day average + room for its slope
BREADTH_MIN_STOCKS = 5


@dataclass(frozen=True)
class Regime:
    trend: str                     # up | down | sideways | unknown
    vol: str                       # calm | normal | volatile | unknown
    breadth_pct: float | None      # % of stored stocks above their 50-day average
    label: str                     # e.g. "up/calm"
    note: str = ""
    as_of: str | None = None
    benchmark: str = ""


UNKNOWN = Regime("unknown", "unknown", None, "unknown")


def regime_from_close(close: pd.Series, breadth_pct: float | None = None, benchmark: str = "") -> Regime:
    """Causal: the label for the LAST bar of `close` uses only that bar and earlier ones."""
    close = close.dropna()
    if len(close) < MIN_BARS:
        return Regime("unknown", "unknown", breadth_pct, "unknown", f"needs {MIN_BARS} bars of {benchmark or 'benchmark'}",
                      close.index[-1].date().isoformat() if len(close) else None, benchmark)
    sma200, sma50 = close.rolling(200).mean(), close.rolling(50).mean()
    last, slope_up, slope_dn = close.iloc[-1], sma50.iloc[-1] > sma50.iloc[-11], sma50.iloc[-1] < sma50.iloc[-11]
    if last > sma200.iloc[-1] and slope_up:
        trend = "up"
    elif last < sma200.iloc[-1] and slope_dn:
        trend = "down"
    else:
        trend = "sideways"
    rv = close.pct_change().rolling(20).std() * np.sqrt(252)
    hist = rv.iloc[-253:-1].dropna()
    if len(hist) >= 100 and not np.isnan(rv.iloc[-1]):
        pct = float((hist < rv.iloc[-1]).mean())
        vol = "calm" if pct <= 0.3 else ("volatile" if pct >= 0.8 else "normal")
    else:
        vol = "unknown"
    return Regime(trend, vol, breadth_pct, f"{trend}/{vol}", "", close.index[-1].date().isoformat(), benchmark)


def breadth(provider: DataProvider, symbols: list[str], upto: date | None = None,
            frames: dict[str, pd.DataFrame] | None = None) -> float | None:
    """Share of stocks closing above their own 50-day average (None when there are too few to say)."""
    above = total = 0
    for sym in symbols:
        try:
            c = (frames[sym] if frames is not None and sym in frames else provider.get_bars(sym))["close"]
        except (FileNotFoundError, ValueError, KeyError):
            continue
        if upto is not None:
            c = c[c.index <= pd.Timestamp(upto)]
        if len(c) < 60:
            continue
        total += 1
        above += int(c.iloc[-1] > c.rolling(50).mean().iloc[-1])
    return round(100.0 * above / total, 1) if total >= BREADTH_MIN_STOCKS else None


def compute_regime(provider: DataProvider, benchmark: str, symbols: list[str] | None = None,
                   upto: date | None = None, frames: dict[str, pd.DataFrame] | None = None) -> Regime:
    if not benchmark:
        return UNKNOWN
    try:
        close = provider.get_bars(benchmark)["close"]
    except (FileNotFoundError, ValueError, KeyError):
        return Regime("unknown", "unknown", None, "unknown",
                      f"benchmark {benchmark} has no stored prices: fetch it on the Data page", None, benchmark)
    if upto is not None:
        close = close[close.index <= pd.Timestamp(upto)]
    syms = [s for s in (symbols if symbols is not None else provider.symbols()) if s != benchmark]
    return regime_from_close(close, breadth(provider, syms, upto, frames), benchmark)


def portfolio_snapshot(session: Session, state: AccountState, starting_capital: float, cfg: RiskConfig) -> dict:
    closed = session.scalars(select(PositionRow).where(PositionRow.closed_at.is_not(None))
                             .order_by(PositionRow.closed_at)).all()
    eq = peak = starting_capital
    for p in closed:
        eq += p.pnl or 0.0
        peak = max(peak, eq)
    peak = max(peak, state.equity)
    limit = cfg.daily_loss_limit_pct * state.equity
    return {
        "equity": round(state.equity, 2), "starting_capital": starting_capital,
        "open_positions": state.open_positions, "slots_free": max(0, cfg.max_open_positions - state.open_positions),
        "exposure_pct": round(100.0 * state.exposure / state.equity, 1) if state.equity > 0 else 0.0,
        "drawdown_pct": round(100.0 * (state.equity / peak - 1.0), 2) if peak > 0 else 0.0,
        "realized_today": round(state.realized_pnl_today, 2),
        "daily_loss_used_pct": round(100.0 * max(0.0, -state.realized_pnl_today) / limit, 1) if limit > 0 else 0.0,
        "kill_switch": state.kill_switch,
    }


def regime_dict(r: Regime) -> dict:
    return asdict(r)
