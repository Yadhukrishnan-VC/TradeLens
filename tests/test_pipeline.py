import pandas as pd
import pytest
from sqlalchemy import select

from conftest import FixedProvider, make_df
from tradelens.domain import Mode
from tradelens.models import OrderRow, PositionRow, SignalRow
from tradelens.risk.engine import RiskConfig, RiskEngine
from tradelens.strategies import registry


def scan_for_signal(pipeline_factory, mode):
    p = pipeline_factory(mode)
    rows = p.scan()
    assert rows, "demo data should produce at least one live-edge signal for some symbol/strategy"
    return p, rows


@pytest.fixture
def edge_provider():
    """A provider whose LAST bar triggers ema_cross/donchian for some symbol: search demo data by
    truncating each DEMO series until the final bar fires a signal."""
    from tradelens.data.synthetic import make_bars
    frames = {}
    for sym in ("DEMO1", "DEMO2", "DEMO3"):
        df = make_bars(sym)
        for name in ("donchian_breakout", "ema_cross"):
            strat = registry.get(name)
            prep = strat.prepare(df)
            for i in range(len(df) - 1, strat.meta.min_bars + 1, -1):
                if strat.on_bar(prep, i, sym) is not None:
                    frames[sym] = df.iloc[: i + 1]
                    break
            if sym in frames:
                break
    assert frames, "could not build an edge-signal fixture"
    return FixedProvider(frames)


def test_signal_only_creates_no_orders(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.SIGNAL_ONLY, provider=edge_provider)
    rows = p.scan()
    assert rows and all(r.status in ("signal", "rejected") for r in rows)
    assert session.scalars(select(OrderRow)).all() == []


def test_semi_auto_waits_then_approval_opens_position(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.SEMI_AUTO, provider=edge_provider)
    p.scan()
    pending = session.scalars(select(OrderRow).where(OrderRow.status == "PENDING_APPROVAL")).all()
    assert pending, "semi-auto should create pending orders"
    assert session.scalars(select(PositionRow)).all() == []      # nothing executed without a human
    order = p.approve(pending[0].id)
    assert order.status == "FILLED" and order.price
    pos = session.scalars(select(PositionRow)).all()
    assert len(pos) == 1 and pos[0].closed_at is None and pos[0].qty == order.qty


def test_auto_executes_immediately(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.AUTO, provider=edge_provider)
    p.scan()
    assert session.scalars(select(PositionRow)).all()
    assert all(o.status == "FILLED" for o in session.scalars(select(OrderRow)).all())


def test_rescan_is_idempotent(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.AUTO, provider=edge_provider)
    p.scan()
    n_sig, n_pos = len(session.scalars(select(SignalRow)).all()), len(session.scalars(select(PositionRow)).all())
    assert p.scan() == []
    assert len(session.scalars(select(SignalRow)).all()) == n_sig
    assert len(session.scalars(select(PositionRow)).all()) == n_pos


def test_kill_switch_blocks_everything(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.AUTO, provider=edge_provider)
    p.set_kill_switch(True)
    rows = p.scan()
    assert rows and all(r.status == "rejected" and r.reason == "KILL_SWITCH" for r in rows)
    assert session.scalars(select(PositionRow)).all() == []


def test_approval_rejected_if_bar_is_stale(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.SEMI_AUTO, provider=edge_provider)
    p.scan()
    order = session.scalars(select(OrderRow).where(OrderRow.status == "PENDING_APPROVAL")).first()
    sym = order.symbol
    df = edge_provider.frames[sym]
    nxt = df.iloc[[-1]].copy()
    nxt.index = nxt.index + pd.tseries.offsets.BDay(1)
    edge_provider.frames[sym] = pd.concat([df, nxt])            # a newer bar appears before approval
    out = p.approve(order.id)
    assert out.status == "REJECTED" and out.reason == "STALE_SIGNAL"


def test_user_reject_cancels(pipeline_factory, edge_provider, session):
    p = pipeline_factory(Mode.SEMI_AUTO, provider=edge_provider)
    p.scan()
    order = session.scalars(select(OrderRow).where(OrderRow.status == "PENDING_APPROVAL")).first()
    assert p.reject(order.id).status == "CANCELLED"
    with pytest.raises(ValueError):
        p.approve(order.id)


def test_exit_closes_on_stop_and_pnl_includes_costs(pipeline_factory, session):
    rows = [(100, 101, 99, 100)] * 3
    df = make_df(rows)
    prov = FixedProvider({"X": df})
    p = pipeline_factory(Mode.AUTO, provider=prov)
    pos = PositionRow(symbol="X", strategy="t", side="BUY", qty=10, entry_price=100.0, stop=95.0, target=110.0,
                      entry_costs=5.0, signal_bar_ts=df.index[0].to_pydatetime(),
                      opened_at=p.clock())
    session.add(pos); session.commit()
    prov.frames["X"] = make_df(rows + [(100, 101, 90, 92)])      # bar 4 trades through the stop
    closed = p.check_exits()
    assert len(closed) == 1 and closed[0].exit_reason == "stop"
    assert closed[0].pnl < (95 - 100) * 10 - 5.0 + 1e-9          # loss + entry cost + exit cost + slippage
    assert p.account_state().open_positions == 0


def test_daily_loss_limit_trips_after_realized_loss(pipeline_factory, session):
    p = pipeline_factory(Mode.AUTO, provider=FixedProvider({"X": make_df([(100, 101, 99, 100)] * 3)}),
                         capital=100_000)
    session.add(PositionRow(symbol="Y", strategy="t", side="BUY", qty=100, entry_price=100.0, stop=90.0,
                            target=None, entry_costs=0.0, signal_bar_ts=p.clock(), opened_at=p.clock(),
                            closed_at=p.clock(), exit_price=60.0, exit_reason="stop", pnl=-4_000.0))
    session.commit()
    st = p.account_state()
    assert st.realized_pnl_today == -4_000 and st.equity == 96_000
    from conftest import buy_signal
    assert RiskEngine(RiskConfig()).evaluate(buy_signal(), st).reason == "DAILY_LOSS_LIMIT"
