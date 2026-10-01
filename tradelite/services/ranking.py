from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import StrategyFitRow


def rank_for(session: Session, strategy: str, config_name: str, symbol: str, timeframe: str = "1d") -> tuple[str, float]:
    """'proven' iff this exact strategy+config previously passed the stability check on this symbol."""
    fit = session.scalar(select(StrategyFitRow).where(
        StrategyFitRow.strategy == strategy, StrategyFitRow.config_name == config_name,
        StrategyFitRow.symbol == symbol, StrategyFitRow.timeframe == timeframe))
    if fit is None or fit.verdict != "candidate":
        return "unproven", 0.0
    return "proven", float(fit.profit_factor) if fit.profit_factor is not None else 99.0
