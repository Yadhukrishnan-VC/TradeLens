from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtest.costs import CostModel
from ..backtest.engine import evaluate_fit
from ..data.base import DataProvider
from ..models import BacktestRunRow, TrialsRow, StrategyFitRow
from ..risk.engine import RiskEngine
from ..strategies import registry
from . import lifecycle, presets


def _params_hash(params: dict[str, Any] | None) -> str:
    """Deterministic hash of the settings (keys and values) for the trial registry."""
    from ..backtest.walkforward import params_hash
    return params_hash(params)


def run_and_store(
    session: Session,
    provider: DataProvider,
    strategy_name: str,
    symbol: str,
    *,
    timeframe: str = "1d",
    capital: float = 100_000.0,
    params: dict[str, Any] | None = None,
    risk: RiskEngine | None = None,
    cost_model: CostModel | None = None,
    slippage_bps: float = 5.0,
    config_name: str = "default",
) -> dict:
    """Backtest one strategy on one symbol, persist the run and update the strategy<->symbol map.

    Also records a row in the trials table for multiple-testing correction.
    """
    if config_name != "default":
        params = {**presets.resolve(session, strategy_name, config_name), **(params or {})}
    elif params:
        raise ValueError("custom parameters must be saved as a preset (POST /strategy-configs) and run by name, "
                         "so that its track record stays separate from the default parameters")
    strat = registry.get(strategy_name, **(params or {}))
    if timeframe not in strat.meta.timeframes:
        raise ValueError(f"{strategy_name} does not support timeframe {timeframe}")
    df = provider.get_bars(symbol, timeframe)
    fit = evaluate_fit(strat, df, symbol, capital=capital, risk=risk, cost_model=cost_model,
                       slippage_bps=slippage_bps)
    res = fit["result"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    trades = [
        {**asdict(t), "entry_ts": t.entry_ts.isoformat(), "exit_ts": t.exit_ts.isoformat()}
        for t in res.trades
    ]
    detail = {k: fit[k] for k in ("all", "early", "late", "split_at", "skipped")}
    wins = [t.exit_ts for t in res.trades if t.net_pnl > 0]
    detail["first_win"] = min(wins).isoformat() if wins else None   # when it actually worked
    detail["last_win"] = max(wins).isoformat() if wins else None
    run = BacktestRunRow(strategy=strategy_name, config_name=config_name, symbol=symbol, timeframe=timeframe,
                         params=res.params, metrics={**res.metrics, "verdict": fit["verdict"]},
                         trades=trades, created_at=now)
    session.add(run)

    # --- record trial ---
    params_hash = _params_hash(params)
    trial = session.scalar(select(TrialsRow).where(
        TrialsRow.strategy == strategy_name,
        TrialsRow.config_name == config_name,
        TrialsRow.symbol == symbol,
        TrialsRow.params_hash == params_hash,
        TrialsRow.run_at == now,
    ))
    if trial is None:
        trial = TrialsRow(
            strategy=strategy_name,
            config_name=config_name,
            symbol=symbol,
            params_hash=params_hash,
            run_at=now,
            n_trades=res.metrics["n_trades"],
            expectancy=res.metrics["expectancy"],
        )
        session.add(trial)
    else:
        # increment n_trades and update expectancy (accumulate)
        trial.n_trades += res.metrics["n_trades"]
        if trial.expectancy is not None and res.metrics["expectancy"] is not None:
            trial.expectancy = (trial.expectancy + res.metrics["expectancy"]) / 2
        elif res.metrics["expectancy"] is not None:
            trial.expectancy = res.metrics["expectancy"]
    # ---------------------------------------

    row = session.scalar(select(StrategyFitRow).where(
        StrategyFitRow.strategy == strategy_name, StrategyFitRow.config_name == config_name,
        StrategyFitRow.symbol == symbol,
        StrategyFitRow.timeframe == timeframe))
    if row is None:
        row = StrategyFitRow(strategy=strategy_name, config_name=config_name, symbol=symbol, timeframe=timeframe)
        session.add(row)
    row.start, row.end = df.index[0].to_pydatetime(), df.index[-1].to_pydatetime()
    row.n_bars = len(df)
    row.avg_volume = float(df["volume"].mean())
    row.n_trades = res.metrics["n_trades"]
    row.profit_factor = res.metrics["profit_factor"]
    row.expectancy = res.metrics["expectancy"]
    row.verdict = fit["verdict"]
    row.detail = detail
    row.updated_at = now
    session.commit()
    lifecycle.record_evidence(session, strategy_name, config_name)   # a passing test promotes a draft to validated
    return {"run_id": run.id, "config_name": config_name, "verdict": fit["verdict"], "metrics": res.metrics,
            "early": fit["early"], "late": fit["late"], "skipped": res.skipped,
            "n_trades": len(trades)}