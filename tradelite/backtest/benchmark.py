"""Benchmarks for a portfolio backtest. A system that cannot beat simply holding the index, after
costs and taking risk into account, is not worth automating.

  buy_and_hold_curve   invest everything on day one in one instrument (e.g. an index ETF), pay costs
  equal_weight_curve   daily-rebalanced equal weight of the tradable universe (no costs): the fair
                       "did stock picking add anything" comparison
  compare              return, risk, beta/alpha and a one-word verdict
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..domain import Side
from ..data.universe import Universe
from .costs import ETF_DELIVERY, CostModel
from .metrics import curve_returns, curve_stats


def buy_and_hold_curve(df: pd.DataFrame, calendar: pd.DatetimeIndex, capital: float, *,
                       cost_model: CostModel | None = None, slippage_bps: float = 5.0) -> pd.Series:
    """Buy at the first calendar day's open, hold, and pay the sell-side cost on the last point."""
    cm = cost_model or ETF_DELIVERY
    slip = slippage_bps / 10_000
    bars = df.reindex(calendar)
    valid = bars["close"].notna()
    if not valid.any():
        raise ValueError("benchmark has no bars in the test window")
    first = valid.idxmax()
    if (first - calendar[0]).days > 5:
        raise ValueError(f"benchmark data starts {first.date()}, after the test start {calendar[0].date()}; "
                         f"fetch more history for it")
    fill = float(bars.loc[first, "open"]) * (1 + slip)
    qty = int(capital // fill)
    while qty > 0 and qty * fill + cm.cost(Side.BUY, fill, qty) > capital:
        qty -= 1
    cash = capital - qty * fill - (cm.cost(Side.BUY, fill, qty) if qty else 0.0)
    close = bars["close"].ffill()
    curve = (cash + qty * close).where(calendar >= first, capital).astype(float)
    last = float(close.iloc[-1]) * (1 - slip)
    curve.iloc[-1] = cash + qty * last - (cm.cost(Side.SELL, last, qty) if qty else 0.0)
    return curve


def equal_weight_curve(frames: dict[str, pd.DataFrame], calendar: pd.DatetimeIndex, capital: float,
                       universe: Universe | None = None) -> pd.Series:
    """Mean daily return of every symbol that has a bar (and is eligible, judged by the previous day)."""
    rets = pd.DataFrame({s: f["close"].pct_change() for s, f in frames.items()}).reindex(calendar)
    if universe is not None:
        mask = pd.DataFrame(False, index=calendar, columns=rets.columns)
        for t, ts in enumerate(calendar):
            elig = universe.eligible(calendar[max(t - 1, 0)])
            cols = [c for c in rets.columns if c in elig]
            mask.loc[ts, cols] = True
        rets = rets.where(mask)
    daily = rets.mean(axis=1, skipna=True).fillna(0.0)
    daily.iloc[0] = 0.0
    return capital * (1.0 + daily).cumprod()


def compare(strategy: pd.Series, benchmark: pd.Series, capital: float, risk_free: float = 0.0) -> dict:
    s, b = curve_stats(strategy, capital, risk_free), curve_stats(benchmark, capital, risk_free)
    rs, rb = curve_returns(strategy, capital), curve_returns(benchmark, capital)
    var = float(np.var(rb, ddof=1)) if len(rb) > 1 else 0.0
    beta = float(np.cov(rs, rb, ddof=1)[0, 1] / var) if var > 0 else None
    rf_d = risk_free / 252
    alpha = ((float(np.mean(rs)) - rf_d) - beta * (float(np.mean(rb)) - rf_d)) * 252 * 100 if beta is not None else None
    corr = float(np.corrcoef(rs, rb)[0, 1]) if var > 0 and np.std(rs) > 0 else None
    excess = s["cagr_pct"] - b["cagr_pct"]
    better_sharpe = s["sharpe"] is not None and b["sharpe"] is not None and s["sharpe"] > b["sharpe"]
    if excess > 0 and (better_sharpe or b["sharpe"] is None):
        verdict = "beats_benchmark"
    elif better_sharpe:
        verdict = "better_risk_adjusted_only"     # lower return, but more return per unit of risk
    else:
        verdict = "underperforms"
    return {"strategy": s, "benchmark": b, "excess_cagr_pct": excess, "beta": beta, "alpha_annual_pct": alpha,
            "correlation": corr, "verdict": verdict}
