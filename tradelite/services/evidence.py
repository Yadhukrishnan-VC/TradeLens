from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import PositionRow, StrategyFitRow, BacktestRunRow


DEFAULT_SHRINKAGE_K = 20


def _month_key(dt: datetime) -> int:
    """Convert datetime to year*12 + month for grouping."""
    return dt.year * 12 + dt.month


def compute_pooled_evidence(
    session: Session,
    strategy: str,
    config_name: str = "default",
) -> dict[str, Any]:
    """Compute pooled expectancy in R across all symbols for a strategy/config.

    Returns dict with pooled_mean, pooled_ci_low, pooled_ci_high, n_pairs, n_trades,
    and per-pair shrunk estimates.
    """
    # Get all closed positions for this strategy/config
    positions = session.scalars(
        select(PositionRow).where(
            PositionRow.strategy == strategy,
            PositionRow.closed_at.is_not(None),
        )
    ).all()

    if not positions:
        return {
            "pooled_mean": None,
            "pooled_ci_low": None,
            "pooled_ci_high": None,
            "n_pairs": 0,
            "n_trades": 0,
            "pairs": [],
        }

    # Group by symbol to compute per-pair R-multiples
    pairs: dict[str, list[float]] = {}
    for pos in positions:
        if pos.pnl is None or pos.entry_price is None or pos.stop is None:
            continue
        risk_amount = abs(pos.entry_price - pos.stop) * pos.qty
        if risk_amount <= 0:
            continue
        r_multiple = pos.pnl / risk_amount
        pairs.setdefault(pos.symbol, []).append(r_multiple)

    if not pairs:
        return {
            "pooled_mean": None,
            "pooled_ci_low": None,
            "pooled_ci_high": None,
            "n_pairs": 0,
            "n_trades": 0,
            "pairs": [],
        }

    # Cluster bootstrap by calendar month
    # Collect all (month, r_multiple) tuples
    month_r_pairs: list[tuple[int, float]] = []
    for pos in positions:
        if pos.pnl is None or pos.entry_price is None or pos.stop is None:
            continue
        risk_amount = abs(pos.entry_price - pos.stop) * pos.qty
        if risk_amount <= 0:
            continue
        r_multiple = pos.pnl / risk_amount
        month = _month_key(pos.closed_at or pos.opened_at)
        month_r_pairs.append((month, r_multiple))

    if not month_r_pairs:
        return {
            "pooled_mean": None,
            "pooled_ci_low": None,
            "pooled_ci_high": None,
            "n_pairs": 0,
            "n_trades": 0,
            "pairs": [],
        }

    # Pool all R-multiples
    all_r = [r for _, r in month_r_pairs]
    pooled_mean = float(np.mean(all_r))

    # Block bootstrap by month (resample months with replacement)
    months = sorted(set(m for m, _ in month_r_pairs))
    n_months = len(months)
    rng = np.random.default_rng(42)
    n_bootstrap = 10_000
    bootstrap_means = []

    for _ in range(n_bootstrap):
        sampled_months = rng.choice(months, size=n_months, replace=True)
        resampled_r = []
        for m in sampled_months:
            resampled_r.extend(r for mm, r in month_r_pairs if mm == m)
        if resampled_r:
            bootstrap_means.append(np.mean(resampled_r))

    ci_low = float(np.percentile(bootstrap_means, 2.5)) if bootstrap_means else None
    ci_high = float(np.percentile(bootstrap_means, 97.5)) if bootstrap_means else None

    # Per-pair estimates with shrinkage
    k = DEFAULT_SHRINKAGE_K
    pair_results = []
    for symbol, r_vals in pairs.items():
        n = len(r_vals)
        if n == 0:
            continue
        pair_mean = float(np.mean(r_vals))
        pair_ci_low, pair_ci_high = _pair_bootstrap_ci(r_vals)
        # Shrinkage: (n * pair_mean + k * pooled_mean) / (n + k)
        shrunk_mean = (n * pair_mean + k * pooled_mean) / (n + k) if pooled_mean is not None else pair_mean
        # Shrunk CI: shrink the lower bound toward pooled CI low
        shrunk_ci_low = (n * pair_ci_low + k * ci_low) / (n + k) if pair_ci_low is not None and ci_low is not None else pair_ci_low

        pair_results.append({
            "symbol": symbol,
            "n_trades": n,
            "pair_mean": pair_mean,
            "pair_ci_low": pair_ci_low,
            "pair_ci_high": pair_ci_high,
            "shrunk_mean": shrunk_mean,
            "shrunk_ci_low": shrunk_ci_low,
            "rank_score": shrunk_ci_low if shrunk_ci_low is not None else 0.0,
        })

    # Sort by shrunk lower CI bound (rank score)
    pair_results.sort(key=lambda p: -(p["rank_score"] or 0.0))

    return {
        "pooled_mean": pooled_mean,
        "pooled_ci_low": ci_low,
        "pooled_ci_high": ci_high,
        "n_pairs": len(pair_results),
        "n_trades": sum(p["n_trades"] for p in pair_results),
        "pairs": pair_results,
    }


def _pair_bootstrap_ci(r_vals: list[float], n_bootstrap: int = 10_000) -> tuple[float | None, float | None]:
    """Bootstrap CI for a single pair's expectancy in R."""
    if len(r_vals) < 3:
        return (None, None)
    rng = np.random.default_rng(42)
    n = len(r_vals)
    bootstrap_means = []
    for _ in range(n_bootstrap):
        sample = rng.choice(r_vals, size=n, replace=True)
        bootstrap_means.append(np.mean(sample))
    lower = float(np.percentile(bootstrap_means, 2.5))
    upper = float(np.percentile(bootstrap_means, 97.5))
    return (lower, upper)


def compute_strategy_level_evidence(session: Session) -> list[dict[str, Any]]:
    """Compute pooled evidence for all strategies across the universe.

    Returns a list of strategy-level results, each with:
    strategy, config_name, n_pairs, n_trades, pooled_mean, pooled_ci, pairs.
    """
    # Get all unique strategy/config combinations that have closed positions
    strategy_configs = session.scalars(
        select(PositionRow.strategy, PositionRow.config_name if hasattr(PositionRow, "config_name") else None)
        .where(PositionRow.closed_at.is_not(None))
        .distinct()
    ).all()

    # Note: PositionRow doesn't have config_name - need to check how strategies are tracked
    # For now, use StrategyFitRow to find strategy/configs with trades
    fits = session.scalars(select(StrategyFitRow).where(StrategyFitRow.n_trades > 0)).all()
    strategy_configs = [(f.strategy, f.config_name) for f in fits]

    results = []
    for strategy, config_name in strategy_configs:
        ev = compute_pooled_evidence(session, strategy, config_name)
        ev["strategy"] = strategy
        ev["config_name"] = config_name
        results.append(ev)

    results.sort(key=lambda r: -(r["pooled_mean"] or 0.0))
    return results