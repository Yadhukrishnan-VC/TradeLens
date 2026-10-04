"""Event-driven backtester with honest fills:
  * signal decided at bar i's CLOSE  -> filled at bar i+1's OPEN (no look-ahead)
  * sizing + approval use the SAME RiskEngine as live trading
  * exits use the SAME check_exit() as the live pipeline (stop wins ties, gaps fill at open)
  * slippage on market fills and stop exits, full transaction costs on both legs
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

import numpy as np
import pandas as pd

from ..data.base import validate_bars
from ..domain import AccountState, Side, Signal
from ..exits import check_exit
from ..risk.engine import RiskConfig, RiskEngine
from ..strategies.base import Strategy
from .costs import DELIVERY_EQUITY, CostModel
from .metrics import compute_metrics


@dataclass
class Trade:
    symbol: str
    strategy: str
    side: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    qty: int
    gross_pnl: float
    costs: float
    net_pnl: float
    risk_amount: float | None = None    # risk at entry (for r-multiple)
    r_multiple: float | None = None     # net_pnl / risk_amount
    reason: str = ""                    # reason for exit (stop, target, end_of_data, etc.)
    signal_ts: datetime | None = None   # the bar whose close produced the signal


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    params: dict
    trades: list[Trade]
    equity_curve: pd.Series
    metrics: dict
    skipped: dict[str, int]   # why signals were not traded (risk rejects, gaps...)


@dataclass
class _Open:
    signal: Signal
    qty: int
    entry_price: float
    entry_ts: datetime
    entry_cost: float
    stop: float | None = None


def run_backtest(
    strategy: Strategy,
    df: pd.DataFrame,
    symbol: str,
    *,
    capital: float = 100_000.0,
    risk: RiskEngine | None = None,
    cost_model: CostModel | None = None,
    slippage_bps: float = 5.0,
    impact_k: float = 10.0,           # impact coefficient for square-root formula
    adv20_value: float | None = None, # ADV20 value for impact calc; None = no cap
) -> BacktestResult:
    risk = risk or RiskEngine(RiskConfig())
    cost_model = cost_model or DELIVERY_EQUITY
    validate_bars(df)
    prep = strategy.prepare(df)
    slip = slippage_bps / 10_000
    o, h, l, c = (prep[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    idx = prep.index
    n = len(prep)

    cash = capital  # realized equity (costs and P&L applied on close)
    pos: _Open | None = None
    pending: Signal | None = None
    trades: list[Trade] = []
    curve = np.empty(n)
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for i in range(n):
        # 1) fill the signal decided on the previous bar's close, at this bar's open
        if pending is not None and pos is None:
            sig, pending = pending, None
            # Fill at the open with flat slippage; size the order with the SAME risk engine.
            fill = o[i] * (1 + slip * sig.side.sign)
            # Liquidity-aware slippage: base_bps + impact_k * sqrt(order_value / ADV20_value). The order size is
            # only known after risk sizing, so size once at the flat-slippage fill, then re-price with impact.
            if adv20_value is not None and adv20_value > 0:
                probe = risk.evaluate(replace(sig, entry=fill), AccountState(
                    equity=cash, cash=cash, open_positions=0, exposure=0.0, realized_pnl_today=0.0))
                if probe.approved:
                    order_value = probe.qty * fill
                    impact_bps = slip * 10_000 + impact_k * math.sqrt(max(order_value / adv20_value, 1e-8))
                    fill = o[i] * (1 + impact_bps / 10_000 * sig.side.sign)
            live_sig = replace(sig, entry=fill)
            state = AccountState(equity=cash, cash=cash, open_positions=0, exposure=0.0, realized_pnl_today=0.0)
            decision = risk.evaluate(live_sig, state)
            if decision.approved:
                qty = decision.qty
                pos = _Open(sig, qty, fill, idx[i].to_pydatetime(), cost_model.cost(sig.side, fill, qty), stop=sig.stop)
            else:
                skip(decision.reason)

        # 2) manage the open position against this bar
        if pos is not None:
            s = pos.signal
            px, reason = check_exit(s.side, s.stop, s.target, o[i], h[i], l[i], slip)
            if px is not None:
                trades.append(_close(pos, px, idx[i].to_pydatetime(), reason, cost_model, symbol))
                cash += trades[-1].net_pnl
                pos = None

        # 3) mark-to-market equity for the drawdown curve
        if pos is not None:
            unreal = (c[i] - pos.entry_price) * pos.qty * pos.signal.side.sign - pos.entry_cost
            curve[i] = cash + unreal
        else:
            curve[i] = cash

        # 4) look for a new signal (decided at this close, filled next open)
        if pos is None and pending is None and i >= strategy.meta.min_bars and i < n - 1:
            sig = strategy.on_bar(prep, i, symbol)
            if sig is not None:
                pending = sig

    if pos is not None:  # close at the last close so results are fully realized
        trades.append(_close(pos, c[-1], idx[-1].to_pydatetime(), "end_of_data", cost_model, symbol))
        cash += trades[-1].net_pnl
        curve[-1] = cash

    series = pd.Series(curve, index=idx)
    return BacktestResult(symbol, strategy.meta.name, dict(strategy.params), trades, series,
                          compute_metrics(trades, series, capital), skipped)


def _close(pos: _Open, px: float, ts: datetime, reason: str, cm: CostModel, symbol: str) -> Trade:
    s = pos.signal
    gross = (px - pos.entry_price) * pos.qty * s.side.sign
    costs = pos.entry_cost + cm.cost(s.side.opposite, px, pos.qty)
    # risk_amount is the initial risk at entry: abs(entry_price - stop) * qty
    risk_amount = abs(pos.entry_price - pos.stop) * pos.qty if pos.stop is not None else None
    r_multiple = (gross - costs) / risk_amount if risk_amount and risk_amount != 0 else None
    return Trade(symbol, s.strategy, s.side.value, pos.entry_ts, pos.entry_price, ts, px,
                 pos.qty, gross, costs, gross - costs, risk_amount=risk_amount, r_multiple=r_multiple,
                 reason=reason, signal_ts=s.ts)


def _bootstrap_ci_r(trades: list, n_bootstrap: int = 10_000, block_size: int = 30, alpha_pct: float = 5.0) -> tuple[float, float]:
    """Seeded 95% bootstrap CI on expectancy in R, block bootstrap by month.

    Returns (ci_low, ci_high). If not enough trades, returns (nan, nan).
    """
    if not trades or len(trades) < 10:
        return (float("nan"), float("nan"))

    # Use block bootstrap: resample contiguous blocks of `block_size` trades
    # to preserve within-block dependence (e.g., same-day correlation)
    rng = np.random.default_rng(42)  # deterministic seed
    n = len(trades)
    block_size = min(block_size, n)
    n_blocks = n // block_size

    # Collect r_multiple values
    r_vals = np.array([t.r_multiple for t in trades if t.r_multiple is not None])
    if len(r_vals) < 3:
        return (float("nan"), float("nan"))

    # Resample blocks with replacement
    bootstrap_means = []
    for _ in range(n_bootstrap):
        # Reconstruct series from blocks
        resampled = []
        for b in range(n_blocks):
            start = b * block_size
            end = start + block_size
            # pick a random block
            rand_start = rng.integers(0, n_blocks)
            block = r_vals[rand_start * block_size : (rand_start + 1) * block_size]
            resampled.extend(block.tolist())
        # fill remaining
        remaining = n - len(resampled)
        if remaining > 0:
            extra = rng.choice(r_vals, size=remaining, replace=True)
            resampled.extend(extra.tolist())
        bootstrap_means.append(np.mean(resampled) if resampled else 0.0)

    lower = np.percentile(bootstrap_means, alpha_pct / 2)
    upper = np.percentile(bootstrap_means, 100 - alpha_pct / 2)
    return (float(lower), float(upper))


def evaluate_fit(
    strategy: Strategy, df: pd.DataFrame, symbol: str, *, split: float = 0.7, min_trades: int = 30, n_bootstrap: int = 10_000, n_trials: int = 1, **kw
) -> dict:
    """Stability check, NOT proof of edge: one full run, trades split by entry time into an
    early and a late segment. 'candidate' needs enough trades, positive expectancy with
    profit factor > 1.2 in BOTH segments, AND a bootstrap lower bound on expectancy above 0. Everything else has no edge."""
    res = run_backtest(strategy, df, symbol, **kw)
    cut = df.index[int(len(df) * split)]
    early = [t for t in res.trades if t.entry_ts < cut]
    late = [t for t in res.trades if t.entry_ts >= cut]
    m_all, m_early, m_late = res.metrics, compute_metrics(early), compute_metrics(late)

    # Bootstrap CI on expectancy in R. The more settings were tried on this stock, the wider the interval
    # (Bonferroni: 5% / n_trials), so luck across many tries cannot pass as an edge.
    ci_low, ci_high = _bootstrap_ci_r(res.trades, n_bootstrap, alpha_pct=5.0 / max(1, min(n_trials, 20)))

    def ok(m: dict) -> bool:
        pf = m["profit_factor"]
        return m["n_trades"] > 0 and (m["expectancy"] or 0) > 0 and (pf is None or pf > 1.2)

    if m_all["n_trades"] < min_trades:
        verdict = "insufficient_data"
    elif ok(m_early) and ok(m_late) and ci_low > 0:      # NaN (too few R values) fails this, by design
        verdict = "candidate"
    else:
        verdict = "no_edge"
    return {"verdict": verdict, "all": m_all, "early": m_early, "late": m_late,
            "split_at": cut.isoformat(), "skipped": res.skipped, "result": res,
            "ci_low": ci_low, "ci_high": ci_high}
