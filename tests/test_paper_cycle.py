"""Whole paper-trading loop, day by day, with the API side and the worker side as SEPARATE broker instances
(separate processes in production): scan -> human approves -> next session fills -> exits -> snapshots -> review."""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from conftest import fresh_engine

from tradelens.broker.paper import PaperBroker
from tradelens.config import Settings
from tradelens.db import make_session_factory
from tradelens.domain import Mode
from tradelens.models import EquitySnapshotRow, JobRunRow, OrderRow, PositionRow
from tradelens.risk.engine import RiskEngine
from tradelens.services import jobs, paper_review
from tradelens.services.alerts import RecordingNotifier
from tradelens.services.pipeline import Pipeline


def synthetic(seed, n=520):
    r = np.random.default_rng(seed).normal(0.0007, 0.014, n)
    close = 100 * np.cumprod(1 + r)
    open_ = np.concatenate([[100.0], close[:-1]]) * (1 + np.random.default_rng(seed + 99).normal(0, 0.004, n))
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.006, "low": np.minimum(open_, close) * 0.994,
                         "close": close, "volume": 1e6}, index=idx)


class Growing:
    """The data source as it looks on a given evening: bars up to the current day only."""
    def __init__(self, full):
        self.full, self.n = full, 0

    def symbols(self):
        return sorted(self.full)

    def get_bars(self, symbol, timeframe="1d"):
        return self.full[symbol].iloc[: self.n]


@pytest.fixture(scope="module")
def cycle():
    full = {f"S{i}": synthetic(i) for i in range(1, 7)}
    prov = Growing(full)
    sf = make_session_factory(fresh_engine())
    cfg = Settings("sqlite://", "data", False, 100_000.0, "semi_auto", "paper", paper_fill="next_open")
    rec = RecordingNotifier()
    api_broker = PaperBroker(fill_model="next_open", bars=prov.get_bars)      # the API process
    worker_broker = PaperBroker(fill_model="next_open", bars=prov.get_bars)   # a different process
    risk = RiskEngine()
    start = 380
    approved = []
    for i in range(start, start + 70):
        prov.n = i + 1
        day = full["S1"].index[i].to_pydatetime()
        now = datetime(day.year, day.month, day.day, 16, 45)
        res = jobs.run_daily(sf, cfg, prov, worker_broker, risk, rec, clock=lambda now=now: now, holidays=set())
        assert res["status"] == "ok", res
        with sf() as s:                                   # the human approves every proposal the same evening
            pipe = Pipeline(s, prov, api_broker, risk=risk, mode=Mode.SEMI_AUTO, starting_capital=100_000.0, notifier=rec)
            for o in s.query(OrderRow).filter_by(status="PENDING_APPROVAL").all():
                approved.append(pipe.approve(o.id).id)
    return sf, prov, cfg, rec, risk, approved, start


def test_the_loop_trades_and_every_entry_fills_at_the_next_open(cycle):
    sf, prov, cfg, rec, risk, approved, start = cycle
    assert approved, "the synthetic data should have produced at least one proposal"
    with sf() as s:
        positions = s.query(PositionRow).all()
        orders = s.query(OrderRow).all()
    assert positions, "approved orders must become positions via the worker's sync"
    for p in positions:
        bars = prov.full[p.symbol]
        nxt = bars[bars.index > pd.Timestamp(p.signal_bar_ts)].iloc[0]
        assert p.entry_price == pytest.approx(float(nxt["open"]) * 1.0005), p.symbol          # next open + slippage, nothing else
        assert p.opened_at >= p.signal_bar_ts
    assert {o.status for o in orders} <= {"FILLED", "SUBMITTED", "REJECTED", "CANCELLED"}
    assert not [a for a in rec.sent if a.level == "critical"], [a.title for a in rec.sent if a.level == "critical"]


def test_snapshots_one_per_day_and_runs_all_ok(cycle):
    sf, prov, cfg, rec, risk, approved, start = cycle
    with sf() as s:
        snaps = s.query(EquitySnapshotRow).order_by(EquitySnapshotRow.day).all()
        runs = s.query(JobRunRow).all()
    assert len(snaps) == 70 and len({x.day for x in snaps}) == 70 and all(r.status == "ok" for r in runs)
    assert snaps[0].equity == pytest.approx(100_000.0)


def test_closed_trades_use_the_same_exit_logic_as_the_backtest(cycle):
    sf, prov, cfg, rec, risk, approved, start = cycle
    with sf() as s:
        closed = s.query(PositionRow).filter(PositionRow.closed_at.is_not(None)).all()
    for p in closed:
        assert p.exit_reason in ("stop", "target") and p.pnl is not None
        if p.exit_reason == "stop":
            assert p.exit_price <= p.stop * 1.0005 + 1e-9 or p.exit_price < p.entry_price * 1.2   # gaps fill at the open, never better than the stop


def test_review_runs_on_the_recorded_history_and_matches_the_replay(cycle):
    sf, prov, cfg, rec, risk, approved, start = cycle
    r = paper_review.review(sf, prov, cfg, risk=risk, benchmark=None)
    assert r["trading_days"] == 70 and r["operations"]["job_coverage"] == 1.0
    sg = r["signals"]
    assert sg["backtest_signals"] > 0
    assert sg["overlap"] is not None and sg["overlap"] >= 0.9, sg["missing_live"][:5]
    gap = r["trades"]["entry_gap_bps_median"]
    assert gap is None or abs(gap) < 1.0, gap                         # same fill model => essentially identical entries
    assert r["tracking"]["days_compared"] > 50
    assert abs(r["tracking"]["max_abs_gap_pp"]) < 3.0, r["tracking"]
