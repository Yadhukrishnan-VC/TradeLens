"""Paper trading vs the backtest: what 4-8 weeks of paper trading can and cannot tell you.

It CAN tell you whether the live system behaves like its backtest (implementation fidelity):
  * the same signals appear on the same days (or you learn why not: missed runs, stale data, rejections)
  * fills land close to what the backtest assumed (next open + slippage)
  * the equity curve tracks the replay of the same strategies over the same days
  * the daily job actually ran, and nothing critical fired
It CANNOT tell you whether the strategy has an edge: with a few weeks you have a handful of trades, and the
win-rate interval printed below shows how wide "I don't know" is. Judge the edge from the long backtest, and the
live period only for "does reality match the model".
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from ..backtest.metrics import compute_metrics
from ..config import Settings
from ..data.base import DataProvider
from ..models import AlertRow, EquitySnapshotRow, FilterLogRow, JobRunRow, OrderRow, PositionRow, SignalRow
from ..risk.engine import RiskEngine
from . import portfolio as portfolio_svc

THRESHOLDS = {"signal_overlap_min": 0.90, "entry_gap_bps_median_max": 25.0, "job_coverage_min": 0.95,
              "equity_gap_pp_max": 2.0, "edge_min_trades": 30}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% interval for a win rate. For 8 trades and 5 wins it is roughly 31%-86%: that is the honest width."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _bps(a: float, b: float) -> float:
    return (a / b - 1.0) * 10_000


def review(sf: Any, provider: DataProvider, settings: Settings, *, risk: RiskEngine | None = None,
           start: str | None = None, benchmark: str | None = portfolio_svc.DEFAULT_BENCHMARK) -> dict:
    with sf() as s:
        snaps = s.scalars(select(EquitySnapshotRow).order_by(EquitySnapshotRow.day)).all()
        if start:
            snaps = [x for x in snaps if x.day >= pd.Timestamp(start)]
        if len(snaps) < 2:
            raise ValueError("need at least 2 equity snapshots: the daily job writes one per trading day "
                             "(run `tradelens run-daily` after the close, or start the worker)")
        t0, t1 = pd.Timestamp(snaps[0].day), pd.Timestamp(snaps[-1].day)
        live_curve = pd.Series([x.equity for x in snaps], index=pd.DatetimeIndex([x.day for x in snaps]))

        signals = s.scalars(select(SignalRow).where(SignalRow.config_name == "default", SignalRow.ts >= t0,
                                                    SignalRow.ts < t1)).all()
        positions = s.scalars(select(PositionRow).where(PositionRow.signal_bar_ts >= t0)).all()
        orders = s.scalars(select(OrderRow).where(OrderRow.created_at >= t0)).all()
        runs = s.scalars(select(JobRunRow).where(JobRunRow.job == "daily", JobRunRow.started_at >= t0)).all()
        alerts = s.scalars(select(AlertRow).where(AlertRow.ts >= t0)).all()
        flogs = s.scalars(select(FilterLogRow)).all()
        sig_by_id = {x.id: x for x in s.scalars(select(SignalRow)).all()}

    # ---- replay: same strategies, same symbols, same days, same risk engine and costs
    res, extra = portfolio_svc.run_portfolio(provider, capital=settings.starting_capital, start=str(t0.date()),
                                             end=str(t1.date()), benchmark=benchmark, risk=risk or RiskEngine())
    bt_curve = res.equity_curve

    # ---- equity tracking
    common = live_curve.index.intersection(bt_curve.index)
    tracking: dict[str, Any] = {"days_compared": len(common)}
    if len(common) >= 2:
        lv, bv = live_curve[common], bt_curve[common]
        lr, br = (lv / lv.iloc[0] - 1) * 100, (bv / bv.iloc[0] - 1) * 100
        tracking.update(live_return_pct=float(lr.iloc[-1]), backtest_return_pct=float(br.iloc[-1]),
                        gap_pp=float(lr.iloc[-1] - br.iloc[-1]), max_abs_gap_pp=float((lr - br).abs().max()))
        cmp_ = extra["comparison"]
        for key, label in (("vs_benchmark", "benchmark_return_pct"), ("vs_equal_weight_universe", "equal_weight_return_pct")):
            c = cmp_.get(key)
            if c is not None:
                curve = extra["curves"].get("benchmark" if key == "vs_benchmark" else "equal_weight")
                cv = curve[common]
                tracking[label] = float((cv.iloc[-1] / cv.iloc[0] - 1) * 100)

    # ---- signals: did the live system see what the replay saw?
    ok_days = {r.started_at.date() for r in runs if r.status == "ok"}
    trading_days = [d.date() for d in live_curve.index]
    bt_keys = {(strat, sym, ts.date()) for ts, strat, sym in res.signals if t0 <= ts < t1}
    live_rows = {(x.strategy, x.symbol, x.ts.date()): x for x in signals}
    matched = bt_keys & live_rows.keys()
    only_bt = sorted(bt_keys - live_rows.keys(), key=lambda k: k[2])
    only_live = sorted(k for k in live_rows.keys() - bt_keys if live_rows[k].reason != "ALREADY_IN_POSITION")
    sig_report = {
        "backtest_signals": len(bt_keys), "live_signals": len(live_rows), "matched": len(matched),
        "overlap": (len(matched) / len(bt_keys)) if bt_keys else None,
        "missing_live": [{"strategy": k[0], "symbol": k[1], "day": k[2].isoformat(),
                          "likely_cause": "no successful daily run that day" if k[2] not in ok_days else "check data freshness / symbol coverage"}
                         for k in only_bt[:25]],
        "extra_live": [{"strategy": k[0], "symbol": k[1], "day": k[2].isoformat(), "status": live_rows[k].status,
                        "reason": live_rows[k].reason} for k in only_live[:25]],
        "live_outcomes": dict(Counter(f"{x.status}:{x.reason}" if x.reason else x.status for x in live_rows.values())),
    }

    # ---- trades: same signal => same trade?
    closed = [p for p in positions if p.closed_at is not None]
    bt_by_key = {(t.strategy, t.symbol, t.signal_ts.date()): t for t in res.trades if t.signal_ts is not None}
    pairs, gaps = [], []
    for p in positions:
        t = bt_by_key.get((p.strategy, p.symbol, p.signal_bar_ts.date()))
        if t is not None:
            gap = _bps(p.entry_price, t.entry_price)
            gaps.append(gap)
            pairs.append({"strategy": p.strategy, "symbol": p.symbol, "signal_day": p.signal_bar_ts.date().isoformat(),
                          "live_entry": round(p.entry_price, 2), "backtest_entry": round(t.entry_price, 2),
                          "entry_gap_bps": round(gap, 1), "live_qty": p.qty, "backtest_qty": t.qty, "live_exit_reason": p.exit_reason, "backtest_exit_reason": t.reason,
                          "live_pnl": None if p.pnl is None else round(p.pnl, 2), "backtest_pnl": round(t.net_pnl, 2)})
    fill_vs_signal = [_bps(p.entry_price, p.signal_entry) for p in positions if p.signal_entry]
    wins = sum(1 for p in closed if (p.pnl or 0) > 0)
    live_m = compute_metrics([type("T", (), {"net_pnl": p.pnl, "costs": p.entry_costs})() for p in closed])
    bt_window = [t for t in res.trades]
    trade_report = {
        "live_closed": len(closed), "live_open": len(positions) - len(closed), "backtest_trades": len(bt_window),
        "live": {"win_rate": live_m["win_rate"], "win_rate_95ci": wilson(wins, len(closed)), "profit_factor": live_m["profit_factor"],
                 "net_pnl": live_m["net_pnl"]},
        "backtest": {"win_rate": res.metrics["win_rate"], "profit_factor": res.metrics["profit_factor"], "net_pnl": res.metrics["net_pnl"]},
        "matched_pairs": pairs[:30],
        "entry_gap_bps_median": float(np.median(gaps)) if gaps else None,
        "fill_vs_signal_close_bps_median": float(np.median(fill_vs_signal)) if fill_vs_signal else None,
        "orders": dict(Counter(o.status for o in orders)),
        "note": ("entry_gap compares the live fill with the backtest's next-open fill; fill_vs_signal compares it with the signal bar's close. "
                 "Quantities legitimately differ a little: live sizes the order BEFORE the open is known (from the signal close), "
                 "the replay sizes at the actual fill. That alone explains a small equity gap with identical trades."),
    }

    # ---- operations
    ops = {"trading_days": len(trading_days), "days_with_ok_run": len([d for d in trading_days if d in ok_days]),
           "failed_runs": sum(1 for r in runs if r.status == "failed"),
           "critical_alerts": [{"ts": a.ts.isoformat(timespec="minutes"), "title": a.title} for a in alerts if a.level == "critical"][:20]}
    ops["job_coverage"] = ops["days_with_ok_run"] / ops["trading_days"] if ops["trading_days"] else None

    # ---- filters (shadow evidence): did the trades a filter would have vetoed do worse?
    pos_by_key = {(p.strategy, p.symbol, p.signal_bar_ts.date()): p for p in closed}
    filters: dict[str, dict] = {}
    for fl in flogs:
        sg = sig_by_id.get(fl.signal_id)
        if sg is None or not (t0 <= pd.Timestamp(sg.ts) < t1):
            continue
        f = filters.setdefault(fl.filter, {"vetoes": 0, "allows": 0, "enforced": fl.enforced, "vetoed_pnl": [], "allowed_pnl": []})
        f["vetoes" if fl.verdict == "veto" else "allows"] += 1
        pos = pos_by_key.get((sg.strategy, sg.symbol, sg.ts.date()))
        if pos is not None:
            f["vetoed_pnl" if fl.verdict == "veto" else "allowed_pnl"].append(pos.pnl or 0.0)
    for f in filters.values():
        v, a = f.pop("vetoed_pnl"), f.pop("allowed_pnl")
        f.update(vetoed_traded=len(v), allowed_traded=len(a), vetoed_avg_pnl=float(np.mean(v)) if v else None,
                 allowed_avg_pnl=float(np.mean(a)) if a else None,
                 verdict="not enough trades to judge" if len(v) < 10 or len(a) < 10 else "see averages",
                 note="Only enforce a filter once vetoed trades are clearly worse than allowed ones, on far more than a few trades.")

    # ---- verdicts
    th = THRESHOLDS
    checks = []

    def check(name: str, value: Any, ok: bool | None, rule: str) -> None:
        checks.append({"check": name, "value": value, "ok": ok, "rule": rule})

    check("signals match the replay", sig_report["overlap"], None if sig_report["overlap"] is None else sig_report["overlap"] >= th["signal_overlap_min"],
          f">= {th['signal_overlap_min']:.0%} of the replay's signals appear live")
    med = trade_report["entry_gap_bps_median"]
    check("fills match the model", med, None if med is None else abs(med) <= th["entry_gap_bps_median_max"],
          f"median entry gap within {th['entry_gap_bps_median_max']:.0f} bps of the backtest fill")
    check("daily job ran", ops["job_coverage"], None if ops["job_coverage"] is None else ops["job_coverage"] >= th["job_coverage_min"],
          f"a successful run on >= {th['job_coverage_min']:.0%} of days")
    gap = tracking.get("max_abs_gap_pp")
    check("equity tracks the replay", gap, None if gap is None else gap <= th["equity_gap_pp_max"],
          f"never more than {th['equity_gap_pp_max']:.0f} percentage points apart")
    applicable = [c["ok"] for c in checks if c["ok"] is not None]
    n = len(closed)
    return {
        "window": [str(t0.date()), str(t1.date())], "trading_days": len(trading_days),
        "implementation_ok": (all(applicable) if applicable else None), "checks": checks, "thresholds": th,
        "edge_evidence": {"closed_trades": n, "enough_to_judge": n >= th["edge_min_trades"],
                          "statement": ("Too few closed trades to say anything about profitability. This period shows whether the system "
                                        "behaves like its backtest, not whether it makes money.") if n < th["edge_min_trades"] else
                                       "Enough trades for a first look; compare win rate and profit factor with the backtest, intervals included."},
        "tracking": tracking, "signals": sig_report, "trades": trade_report, "operations": ops, "filters": filters,
        "survivorship": extra["survivorship"],
    }
