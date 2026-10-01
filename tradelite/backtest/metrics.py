from __future__ import annotations

from typing import Any, Sequence

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
