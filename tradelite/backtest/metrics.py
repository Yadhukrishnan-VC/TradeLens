from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd


def compute_metrics(trades: Sequence[Any], curve: pd.Series | None = None, capital: float | None = None) -> dict:
    net = [t.net_pnl for t in trades]
    n = len(net)
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    m: dict[str, Any] = {
        "n_trades": n,
        "win_rate": (len(wins) / n) if n else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,  # None = no losing trades
        "expectancy": (sum(net) / n) if n else None,
        "avg_win": (gross_profit / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "net_pnl": sum(net),
        "total_costs": sum(t.costs for t in trades),
    }
    if curve is not None and capital:
        m["total_return_pct"] = float(curve.iloc[-1] / capital - 1) * 100
        m["max_drawdown_pct"] = float((curve / curve.cummax() - 1).min()) * 100
    return m


TRADING_DAYS = 252


def curve_returns(curve: pd.Series, initial: float) -> np.ndarray:
    """Daily simple returns of an equity curve, with `initial` as the value before its first point."""
    vals = np.concatenate([[float(initial)], curve.to_numpy(dtype=float)])
    return vals[1:] / vals[:-1] - 1.0


def curve_stats(curve: pd.Series, initial: float, risk_free: float = 0.0) -> dict:
    """Return / risk statistics of an equity curve. `risk_free` is an annual rate (0.06 = 6%).
    With risk_free=0 the Sharpe ratio is flattering for everything, so compare like with like."""
    vals = np.concatenate([[float(initial)], curve.to_numpy(dtype=float)])
    rets = vals[1:] / vals[:-1] - 1.0
    total = vals[-1] / vals[0] - 1.0
    days = max((curve.index[-1] - curve.index[0]).days, 1)
    years = days / 365.25
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1.0 else -1.0
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    sharpe = (float(np.mean(rets)) - risk_free / TRADING_DAYS) / sd * np.sqrt(TRADING_DAYS) if sd > 0 else None
    dd = float((vals / np.maximum.accumulate(vals) - 1.0).min())
    return {
        "total_return_pct": total * 100, "cagr_pct": cagr * 100, "volatility_pct": sd * np.sqrt(TRADING_DAYS) * 100,
        "sharpe": None if sharpe is None else float(sharpe), "max_drawdown_pct": dd * 100,
        "calmar": (cagr / -dd) if dd < 0 else None, "years": years,
    }
