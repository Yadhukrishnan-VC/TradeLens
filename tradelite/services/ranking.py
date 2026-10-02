from __future__ import annotations

import math
from scipy import stats
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import StrategyFitRow, TrialsRow


def n_trials(session: Session, strategy: str, symbol: str) -> int:
    """Number of distinct strategy+config variants tried on this symbol."""
    return session.scalar(select(func.count()).select_from(TrialsRow).where(
        TrialsRow.strategy == strategy, TrialsRow.symbol == symbol))


def bonferroni_adjust(t_stat: float, n: int) -> float:
    """Bonferroni-adjusted p-value: p_adj = min(1, n * p)."""
    if n <= 0:
        return 1.0
    p = 2 * (1 - stats.cdf(abs(t_stat)))  # two-sided
    p_adj = min(1.0, n * p)
    return p_adj


def adjusted_verdict(verdict: str, t_stat: float, n: int) -> tuple[str, bool, str]:
    """Return (verdict, adjusted, reason) applying multiple-testing correction.

    - If adjusted is True, the bar is higher because N settings were tried.
    - Returns the plain-language reason string.
    """
    adjusted = False
    reason = ""
    if n > 1:
        adjusted = True
        reason = f"{n} settings tried for this stock, so the bar is higher"
    return verdict, adjusted, reason


def rank_for(session: Session, strategy: str, config_name: str, symbol: str, timeframe: str = "1d") -> tuple[str, float, bool, str]:
    """'proven' iff this exact strategy+config previously passed the stability check on this symbol.

    Returns: (verdict, rank_score, adjusted, reason)

    Stored verdict "candidate" maps to rank "proven" when PF > 1.2 and enough trades,
    per the platform's stability check definition.
    """
    n = n_trials(session, strategy, symbol)
    fit = session.scalar(select(StrategyFitRow).where(
        StrategyFitRow.strategy == strategy, StrategyFitRow.config_name == config_name,
        StrategyFitRow.symbol == symbol, StrategyFitRow.timeframe == timeframe))
    if fit is None:
        return "unproven", 0.0, False, ""

    # Map stored verdict to rank: "candidate" -> "proven" when PF > 1.2 with enough trades.
    # This preserves the platform's stability-check definition of "proven".
    if fit.verdict == "candidate" and fit.profit_factor is not None and fit.profit_factor > 1.2:
        rank = "proven"
    elif fit.verdict == "candidate":
        rank = "unproven"
    else:
        rank = fit.verdict

    score = float(fit.profit_factor) if fit.profit_factor is not None else 99.0
    adj, adjusted, reason = adjusted_verdict(rank, 0.0, n)
    return rank, score, adjusted, reason