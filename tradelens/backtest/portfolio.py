"""Portfolio backtester: many symbols and strategies share ONE account.

Same honesty rules as the single-symbol engine:
  * signals are decided at a bar's CLOSE and filled at the NEXT bar's open (plus slippage)
  * every fill goes through the SAME RiskEngine, with the real shared account state
    (marked-to-market equity, cost-basis exposure, open positions, prior day's realized P&L)
  * exits use the SAME check_exit() as the live pipeline; costs on both legs

What is new: positions compete for limited slots and capital. When more signals arrive than the
account can take, the more liquid symbol (trailing 20-day traded value, known at the signal close)
goes first, then strategy order, then symbol name. That tie-break uses no future data.

A symbol whose data ends while a position is open is closed at its last close less slippage
(reason "data_end"). That is optimistic for a bankruptcy: treat delisting results with care.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import reduce

import numpy as np
import pandas as pd

from ..data.base import validate_bars
from ..data.universe import Universe
from ..domain import AccountState, Signal
from ..exits import check_exit
from ..risk.engine import RiskConfig, RiskEngine
from ..strategies.base import Strategy
from .costs import DELIVERY_EQUITY, CostModel
from .engine import Trade, _close, _Open
from .metrics import compute_metrics, curve_stats


@dataclass
class PortfolioResult:
    strategies: list[str]
    symbols: list[str]
    trades: list[Trade]
    equity_curve: pd.Series
    exposure: pd.Series              # cost-basis notional / equity, per day
    metrics: dict
    by_strategy: dict[str, dict]
    skipped: dict[str, int]
    capital: float
    start: pd.Timestamp
    end: pd.Timestamp
    notes: list[str] = field(default_factory=list)
    signals: list[tuple[pd.Timestamp, str, str]] = field(default_factory=list)   # every candidate: (bar, strategy, symbol)


@dataclass
class _Sym:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    liq: np.ndarray
    loc: np.ndarray            # calendar position -> bar position in this symbol (-1 = no bar that day)
    last_ts: pd.Timestamp
    preps: list[pd.DataFrame]


def run_portfolio_backtest(
    strategies: list[Strategy],
    frames: dict[str, pd.DataFrame],
    *,
    capital: float = 100_000.0,
    risk: RiskEngine | None = None,
    cost_model: CostModel | None = None,
    slippage_bps: float = 5.0,
    universe: Universe | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> PortfolioResult:
    if not strategies:
        raise ValueError("need at least one strategy")
    if not frames:
        raise ValueError("no price data")
    risk = risk or RiskEngine(RiskConfig())
    cost_model = cost_model or DELIVERY_EQUITY
    slip = slippage_bps / 10_000
    for df in frames.values():
        validate_bars(df)

    calendar = reduce(lambda a, b: a.union(b), (f.index for f in frames.values()))
    if start is not None:
        calendar = calendar[calendar >= pd.Timestamp(start)]
    if end is not None:
        calendar = calendar[calendar <= pd.Timestamp(end)]
    n = len(calendar)
    if n < 2:
        raise ValueError("fewer than two trading days in the requested window")

    D: dict[str, _Sym] = {}
    for sym, df in frames.items():
        # indicators are computed on the symbol's FULL history (warm-up), but trading only happens inside the window
        D[sym] = _Sym(
            *(df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close")),
            liq=(df["close"] * df["volume"]).rolling(20, min_periods=1).mean().to_numpy(dtype=float),
            loc=df.index.get_indexer(calendar), last_ts=df.index[-1],
            preps=[s.prepare(df) for s in strategies])

    realized = capital                      # start capital + net P&L of closed trades
    open_: dict[str, _Open] = {}
    last_close: dict[str, float] = {}
    pnl_by_day: dict = {}
    trades: list[Trade] = []
    pending: list[tuple[str, Signal]] = []
    skipped: dict[str, int] = {}
    curve, expo = np.empty(n), np.zeros(n)
    max_open = 0
    n_signals = 0
    sig_log: list[tuple[pd.Timestamp, str, str]] = []

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def state(prev_pnl: float = 0.0) -> AccountState:
        unreal = sum((last_close[s] - p.entry_price) * p.qty * p.signal.side.sign - p.entry_cost
                     for s, p in open_.items())
        equity = realized + unreal
        exposure = sum(p.qty * p.entry_price for p in open_.values())
        return AccountState(equity=equity, cash=max(0.0, equity - exposure), open_positions=len(open_),
                            exposure=exposure, realized_pnl_today=prev_pnl, open_symbols=frozenset(open_))

    def close_pos(sym: str, px: float, ts: pd.Timestamp, reason: str) -> None:
        nonlocal realized
        t = _close(open_.pop(sym), px, ts.to_pydatetime(), reason, cost_model, sym)
        trades.append(t)
        realized += t.net_pnl
        pnl_by_day[ts.date()] = pnl_by_day.get(ts.date(), 0.0) + t.net_pnl
        last_close.pop(sym, None)

    for t, ts in enumerate(calendar):
        prev_pnl = pnl_by_day.get(calendar[t - 1].date(), 0.0) if t else 0.0

        # 1) fill signals decided at the previous close, at today's open, in priority order
        for sym, sig in pending:
            i = D[sym].loc[t]
            if i < 0:
                skip("NO_BAR")
                continue
            fill = D[sym].o[i] * (1 + slip * sig.side.sign)
            dec = risk.evaluate(replace(sig, entry=fill), state(prev_pnl))
            if not dec.approved:
                skip(dec.reason)
                continue
            open_[sym] = _Open(sig, dec.qty, fill, ts.to_pydatetime(), cost_model.cost(sig.side, fill, dec.qty))
            last_close[sym] = fill
            max_open = max(max_open, len(open_))
        pending = []

        # 2) exits, checked on today's bar (including positions opened at today's open)
        for sym in list(open_):
            sd, i = D[sym], D[sym].loc[t]
            if i < 0:
                continue
            p = open_[sym]
            px, reason = check_exit(p.signal.side, p.signal.stop, p.signal.target, sd.o[i], sd.h[i], sd.l[i], slip)
            if px is None and sd.last_ts == ts and t < n - 1:
                px, reason = sd.c[i] * (1 - slip * p.signal.side.sign), "data_end"
            if px is not None:
                close_pos(sym, px, ts, reason)
            else:
                last_close[sym] = sd.c[i]

        # 3) mark to market
        st = state()
        curve[t] = st.equity
        expo[t] = st.exposure / st.equity if st.equity > 0 else 0.0

        # 4) new signals from today's close, filled tomorrow
        if t < n - 1:
            elig = universe.eligible(ts) if universe is not None else None
            cands = []
            for sym, sd in D.items():
                i = sd.loc[t]
                if i < 0 or sym in open_ or (elig is not None and sym not in elig):
                    continue
                for k, strat in enumerate(strategies):
                    if i < strat.meta.min_bars:
                        continue
                    sig = strat.on_bar(sd.preps[k], i, sym)
                    if sig is not None:
                        cands.append((-sd.liq[i], k, sym, sig))
            n_signals += len(cands)
            sig_log.extend((ts, strategies[k].meta.name, sym) for _, k, sym, _s in cands)
            cands.sort(key=lambda x: x[:3])
            pending = [(sym, sig) for _, _, sym, sig in cands]

    for sym in list(open_):    # realize everything so the result is complete
        i = D[sym].loc[n - 1]
        close_pos(sym, D[sym].c[i] if i >= 0 else last_close[sym], calendar[-1], "end_of_data")
    curve[-1], expo[-1] = realized, 0.0

    series = pd.Series(curve, index=calendar)
    metrics = {**compute_metrics(trades), **curve_stats(series, capital),
               "avg_exposure_pct": float(expo.mean() * 100), "max_open_positions": max_open,
               "n_signals": n_signals, "n_symbols_traded": len({t.symbol for t in trades})}
    names = [s.meta.name for s in strategies]
    return PortfolioResult(
        strategies=names, symbols=sorted(frames), trades=trades, equity_curve=series,
        exposure=pd.Series(expo, index=calendar), metrics=metrics,
        by_strategy={nm: compute_metrics([t for t in trades if t.strategy == nm]) for nm in names},
        skipped=skipped, capital=capital, start=calendar[0], end=calendar[-1], signals=sig_log)
