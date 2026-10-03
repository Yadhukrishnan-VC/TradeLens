"""Walk-forward validation: pick settings on a training slice, judge them on the NEXT, unseen slice.

For each rolling window the best setting from the grid is chosen on the training data only (by the lower
confidence bound of expectancy in R, so a lucky small sample does not win), then run on the following test
window. Only the out-of-sample trades are reported. Every grid point tried is written to the trial registry,
because the more settings you try, the more a "winner" can be luck.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.base import DataProvider
from ..models import TrialsRow
from ..strategies import registry
from .costs import CostModel
from .engine import run_backtest

MIN_BARS = 30           # a slice with fewer bars than this cannot produce a meaningful result


def params_hash(params: dict[str, Any] | None) -> str:
    """Deterministic hash of a settings dict (keys AND values), used to tell trial variants apart."""
    import hashlib
    return hashlib.sha256(repr(sorted((params or {}).items())).encode()).hexdigest()


def _window_bounds(
    history_start: datetime,
    history_end: datetime,
    train_years: float = 3.0,
    test_years: float = 1.0,
    step_years: float = 1.0,
    embargo_years: float | None = None,
) -> list[dict[str, datetime]]:
    """Rolling windows: train [a, b), an embargo gap, then test [c, d). The gap keeps trades that were still
    open at the end of training from leaking into the test slice. The next window starts `step_years` later."""
    start = pd.Timestamp(history_start).tz_localize(None) if pd.Timestamp(history_start).tzinfo else pd.Timestamp(history_start)
    end = pd.Timestamp(history_end).tz_localize(None) if pd.Timestamp(history_end).tzinfo else pd.Timestamp(history_end)
    if step_years <= 0 or train_years <= 0 or test_years <= 0:
        raise ValueError("train_years, test_years and step_years must be positive")
    day = lambda yrs: timedelta(days=int(round(yrs * 365)))  # noqa: E731
    train, test, step, gap = day(train_years), day(test_years), day(step_years), day(embargo_years or 0.0)
    windows: list[dict[str, datetime]] = []
    t0 = start
    while t0 + train + gap + test <= end:
        windows.append({"train_start": t0, "train_end": t0 + train,
                        "test_start": t0 + train + gap, "test_end": t0 + train + gap + test})
        t0 += step
    return windows


def _ci_low_r(r_vals: list[float]) -> float:
    """Approximate 95% lower bound of mean R (mean - 2 standard errors). Too few trades => -inf (never selected)."""
    if len(r_vals) < 5:
        return -float("inf")
    return float(np.mean(r_vals) - 2 * np.std(r_vals, ddof=1) / np.sqrt(len(r_vals)))


def walkforward_validate(
    session: Session,
    provider: DataProvider,
    strategy_name: str,
    symbol: str,
    *,
    timeframe: str = "1d",
    capital: float = 100_000.0,
    param_grid: list[dict[str, Any]] | None = None,
    train_years: float = 3.0,
    test_years: float = 1.0,
    step_years: float = 1.0,
    embargo_years: float | None = 0.0,
    cost_model: CostModel | None = None,
    slippage_bps: float = 5.0,
) -> dict[str, Any]:
    if strategy_name not in registry.discover():
        raise KeyError(f"unknown strategy {strategy_name!r}")
    df = provider.get_bars(symbol, timeframe)
    if len(df) < 365:
        return {"error": f"Insufficient history for walk-forward: {len(df)} bars", "windows": [], "n_windows": 0}
    windows = _window_bounds(df.index[0], df.index[-1], train_years, test_years, step_years, embargo_years)
    if not windows:
        return {"error": "History is too short for this train/test length", "windows": [], "n_windows": 0}

    grid = param_grid or [{}]
    kw = {"capital": capital, "cost_model": cost_model, "slippage_bps": slippage_bps}
    rows: list[dict[str, Any]] = []
    oos_r: list[float] = []
    n_oos = 0
    skipped = 0
    for w in windows:
        train_df = df[(df.index >= w["train_start"]) & (df.index < w["train_end"])]
        test_df = df[(df.index >= w["test_start"]) & (df.index < w["test_end"])]
        if len(train_df) < MIN_BARS or len(test_df) < MIN_BARS:
            skipped += 1
            continue
        best: dict[str, Any] | None = None
        best_lb = -float("inf")
        for params in grid:
            try:
                strat = registry.get(strategy_name, **params)
                if len(train_df) < strat.meta.min_bars + 2:
                    continue
                res = run_backtest(strat, train_df, symbol, **kw)
            except (TypeError, ValueError):     # bad settings for THIS strategy: skip the point, keep going
                continue
            lb = _ci_low_r([t.r_multiple for t in res.trades if t.r_multiple is not None])
            if lb > best_lb:
                best_lb, best = lb, params
        if best is None:                         # nothing earned selection on the training slice: do not trade it
            rows.append({"train_start": w["train_start"].isoformat(), "test_start": w["test_start"].isoformat(),
                         "test_end": w["test_end"].isoformat(), "selected_params": None, "n_oos_trades": 0,
                         "oos_expectancy_r": None, "note": "no setting had enough evidence in training"})
            continue
        strat = registry.get(strategy_name, **best)
        if len(test_df) < strat.meta.min_bars + 2:
            skipped += 1
            continue
        res = run_backtest(strat, test_df, symbol, **kw)
        r = [t.r_multiple for t in res.trades if t.r_multiple is not None]
        oos_r.extend(r)
        n_oos += len(res.trades)
        rows.append({"train_start": w["train_start"].isoformat(), "test_start": w["test_start"].isoformat(),
                     "test_end": w["test_end"].isoformat(), "selected_params": best, "n_oos_trades": len(res.trades),
                     "oos_expectancy_r": float(np.mean(r)) if r else None})

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for params in grid:                          # every point tried counts, even if it never won a window
        _record_trial(session, strategy_name, symbol, params, now)
    session.commit()

    overall: dict[str, Any] = {"n_oos_trades": n_oos}
    if oos_r:
        overall.update(mean_r=float(np.mean(oos_r)), ci_low_r=_ci_low_r(oos_r) if len(oos_r) >= 5 else None)
    return {"windows": rows, "n_windows": len(rows), "windows_skipped": skipped, "settings_tried": len(grid),
            "overall": overall,
            "note": "Out-of-sample only. A positive overall mean with ci_low_r above 0 is the bar; "
                    "a small number of trades is weak evidence either way."}


def _record_trial(session: Session, strategy: str, symbol: str, params: dict[str, Any] | None, run_at: datetime,
                  config_name: str = "default") -> None:
    h = params_hash(params)
    exists = session.scalar(select(TrialsRow.id).where(
        TrialsRow.strategy == strategy, TrialsRow.config_name == config_name, TrialsRow.symbol == symbol,
        TrialsRow.params_hash == h, TrialsRow.run_at == run_at))
    if exists is None:
        session.add(TrialsRow(strategy=strategy, config_name=config_name, symbol=symbol, params_hash=h,
                              run_at=run_at, n_trades=0, expectancy=None))
