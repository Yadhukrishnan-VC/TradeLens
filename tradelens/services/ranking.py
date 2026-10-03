from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import StrategyFitRow, TrialsRow


def n_trials(session: Session, strategy: str, symbol: str) -> int:
    """Number of distinct strategy+config variants tried on this symbol."""
    return int(session.scalar(select(func.count()).select_from(TrialsRow).where(
        TrialsRow.strategy == strategy, TrialsRow.symbol == symbol)) or 0)


def rank_for(session: Session, strategy: str, config_name: str, symbol: str,
             timeframe: str = "1d") -> tuple[str, float, bool, str]:
    """Returns (rank, score, adjusted, reason).

    rank is 'proven' iff this exact strategy+config passed the stability check ('candidate') on this
    symbol; everything else is 'unproven' (the stored verdict stays visible on the Backtests page).
    Only these two values are ever returned: the rank columns are String(10) and the UI knows two badges.
    score is the stored profit factor (a proven pair without one scores 99).
    adjusted/reason: True when several settings were tried on this stock, so a pass is weaker evidence.
    """
    fit = session.scalar(select(StrategyFitRow).where(
        StrategyFitRow.strategy == strategy, StrategyFitRow.config_name == config_name,
        StrategyFitRow.symbol == symbol, StrategyFitRow.timeframe == timeframe))
    if fit is None or fit.verdict != "candidate":
        return "unproven", 0.0, False, ""
    score = float(fit.profit_factor) if fit.profit_factor is not None else 99.0
    n = n_trials(session, strategy, symbol)
    if n > 1:
        return "proven", score, True, f"{n} settings tried for this stock, so the bar is higher"
    return "proven", score, False, ""
