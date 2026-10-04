from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ..backtest.benchmark import buy_and_hold_curve, compare, equal_weight_curve
from ..backtest.costs import CostModel
from ..backtest.portfolio import PortfolioResult, run_portfolio_backtest
from ..data.base import DataProvider
from ..data.universe import CombinedUniverse, IntervalUniverse, LiquidityUniverse, Universe, survivorship_report
from ..models import PortfolioRunRow
from ..risk.engine import RiskEngine
from ..strategies import registry

DEFAULT_BENCHMARK = "NIFTYBEES"   # Nifty 50 ETF; fetch it first (`tradelens fetch --symbols NIFTYBEES`)


def _clean(x: Any) -> Any:
    """JSON-safe: NaN/inf -> None, numpy scalars -> python."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def resolve_universe_file(data_dir: str, name: str) -> Path:
    """Only plain file names inside DATA_DIR: the API must not read arbitrary paths."""
    base = Path(data_dir).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise ValueError(f"universe file '{name}' not found in {data_dir} (plain file name only)")
    return path


def run_portfolio(
    provider: DataProvider,
    *,
    strategy_names: list[str] | None = None,
    symbols: list[str] | None = None,
    capital: float = 100_000.0,
    start: str | None = None,
    end: str | None = None,
    benchmark: str | None = DEFAULT_BENCHMARK,
    universe_file: Path | None = None,
    liquid_top_n: int | None = None,
    risk: RiskEngine | None = None,
    cost_model: CostModel | None = None,
    slippage_bps: float = 5.0,
    risk_free: float = 0.0,
) -> tuple[PortfolioResult, dict]:
    """Run the backtest and build the comparison + survivorship report. Pure: nothing is stored."""
    names = strategy_names or sorted(registry.discover())
    strategies = [registry.get(n) for n in names]

    interval = IntervalUniverse.from_csv(universe_file) if universe_file else None
    available = set(provider.symbols())
    if symbols:
        requested = sorted({s.strip().upper() for s in symbols if s.strip()})
        frames = {s: provider.get_bars(s) for s in requested}          # explicit list: a missing one is an error
    else:
        requested = sorted(interval.symbols if interval else available - {(benchmark or '').upper()})
        frames = {s: provider.get_bars(s) for s in requested if s in available}
    if benchmark:
        frames.pop(benchmark.upper(), None)                             # never trade the benchmark itself
    if not frames:
        raise FileNotFoundError("no price data for any requested symbol. Fetch history first.")

    parts: list[Universe] = [interval] if interval else []
    if liquid_top_n:
        parts.append(LiquidityUniverse(frames, liquid_top_n))
    universe: Universe | None = parts[0] if len(parts) == 1 else CombinedUniverse(*parts) if parts else None

    res = run_portfolio_backtest(strategies, frames, capital=capital, risk=risk, cost_model=cost_model,
                                 slippage_bps=slippage_bps, universe=universe, start=start, end=end)
    calendar = res.equity_curve.index
    bench_curve = (buy_and_hold_curve(provider.get_bars(benchmark.upper()), calendar, capital, slippage_bps=slippage_bps)
                   if benchmark else None)
    ew_curve = equal_weight_curve(frames, calendar, capital, universe)
    comparison = {
        "benchmark_symbol": benchmark.upper() if benchmark else None,
        "vs_benchmark": compare(res.equity_curve, bench_curve, capital, risk_free) if bench_curve is not None else None,
        "vs_equal_weight_universe": compare(res.equity_curve, ew_curve, capital, risk_free),
        "avg_exposure_pct": res.metrics["avg_exposure_pct"],
        "notes": [
            "The benchmark is one instrument bought on day one with costs. Prices from Yahoo are dividend-adjusted "
            "for ETFs and stocks, but a raw index such as ^NSEI is price-only and would understate the benchmark.",
            "Sharpe uses risk_free=%.2f; the system is invested only part of the time (see avg_exposure_pct), so "
            "compare CAGR together with drawdown and Sharpe, not CAGR alone." % risk_free,
            "Winners of many tried strategy/parameter combinations are partly luck: treat one good run as a "
            "candidate for paper trading, not as proof.",
        ],
    }
    report = survivorship_report(frames, universe, res.start, res.end, requested=requested)
    extra = {"comparison": comparison, "survivorship": report,
             "curves": {k: v for k, v in {"strategy": res.equity_curve, "benchmark": bench_curve,
                                          "equal_weight": ew_curve, "exposure": res.exposure}.items() if v is not None},
             "settings": {"slippage_bps": slippage_bps, "benchmark": benchmark.upper() if benchmark else None, "risk_free": risk_free,
                          "universe": report["universe"], "liquid_top_n": liquid_top_n,
                          "risk_config": asdict(risk.cfg) if risk else None}}
    return res, extra


def run_and_store(session: Session, provider: DataProvider, **kw: Any) -> dict:
    res, extra = run_portfolio(provider, **kw)
    curves = extra["curves"]
    stored_curves = {"dates": [d.date().isoformat() for d in res.equity_curve.index],
                     **{k: [round(float(v), 4) for v in s.to_numpy()] for k, s in curves.items()}}
    trades = [{**asdict(t), "entry_ts": t.entry_ts.isoformat(), "exit_ts": t.exit_ts.isoformat(),
               "signal_ts": t.signal_ts.isoformat() if t.signal_ts else None} for t in res.trades]
    row = PortfolioRunRow(
        strategies=res.strategies, symbols=res.symbols, capital=res.capital,
        start=res.start.to_pydatetime(), end=res.end.to_pydatetime(), settings=_clean(extra["settings"]),
        metrics=_clean(res.metrics), by_strategy=_clean(res.by_strategy), comparison=_clean(extra["comparison"]),
        survivorship=_clean(extra["survivorship"]), skipped=res.skipped, trades=_clean(trades),
        curves=_clean(stored_curves), created_at=datetime.now(timezone.utc).replace(tzinfo=None))
    session.add(row)
    session.commit()
    return {"run_id": row.id, "period": [res.start.date().isoformat(), res.end.date().isoformat()],
            "strategies": res.strategies, "n_symbols": len(res.symbols), "n_trades": len(res.trades),
            "metrics": row.metrics, "by_strategy": row.by_strategy, "comparison": row.comparison,
            "survivorship": row.survivorship, "skipped": res.skipped}
