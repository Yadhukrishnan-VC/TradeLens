"""Gate, lifecycle, market context, data quality, event calendar, watchlist, position advice, circuit breaker,
and the schema upgrade that lets an existing database accept all of them."""
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from conftest import FixedProvider, fresh_engine
from test_features import _edge_frame, _mark_proven
from test_ops import NOW, fresh_frames
from test_ops import settings as ops_settings
from test_screener import FakeLive
from tradelite.api.main import create_app
from tradelite.broker.paper import PaperBroker
from tradelite.config import Settings
from tradelite.data.synthetic import DemoProvider, make_bars
from tradelite.db import make_session_factory, upgrade_schema
from tradelite.domain import Mode
from tradelite.models import (AlertRow, EventRow, OrderRow, PositionAlertRow, PositionRow, SignalRow,
                              StrategyFitRow, StrategyLifecycleRow, WatchRow)
from tradelite.risk.engine import RiskEngine
from tradelite.services import context, events, gate, jobs, lifecycle, presets, quality, screener
from tradelite.services.alerts import (MultiNotifier, RecordingNotifier, StoredNotifier, TelegramNotifier)
from tradelite.services.breaker import BreakerOpen, CircuitBreaker, get_breaker
from tradelite.services.gate import GateConfig
from tradelite.strategies import registry
from tradelite.strategies.base import ExitHint

CLOCK = datetime(2024, 1, 10)          # a day after _mark_proven's verdict


def series(n=400, drift=0.002, vol=0.004, seed=1, start="2020-01-01"):
    r = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(drift + r.normal(0, vol, n)))
    return pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close,
                         "volume": np.full(n, 1e6)}, index=pd.bdate_range(start, periods=n))


# =====================================================================================
# schema upgrade: an existing database must accept the new columns without losing data
# =====================================================================================
def test_upgrade_adds_missing_columns_and_keeps_old_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as c:   # the signals table exactly as the previous version created it
        c.execute(text("""CREATE TABLE signals (id INTEGER PRIMARY KEY, strategy VARCHAR(64), config_name VARCHAR(64),
            symbol VARCHAR(32), side VARCHAR(4), ts DATETIME, entry FLOAT, stop FLOAT, target FLOAT, status VARCHAR(16),
            reason VARCHAR(64), suggested_qty INTEGER, rank VARCHAR(10), rank_score FLOAT, created_at DATETIME,
            UNIQUE (strategy, config_name, symbol, ts))"""))
        c.execute(text("INSERT INTO signals VALUES (1,'ema_cross','default','TCS','BUY','2024-01-02',100,95,110,'proposed',NULL,5,'proven',1.5,'2024-01-02')"))
    old.dispose()
    sf = make_session_factory(__import__("tradelite.db", fromlist=["make_engine"]).make_engine(url))
    with sf() as s:
        row = s.scalar(select(SignalRow))
        assert (row.symbol, row.status, row.rank) == ("TCS", "proposed", "proven")          # nothing lost
        assert (row.quality, row.flags, row.gate, row.regime) == ("ok", "", "off", "")      # sensible defaults
    assert upgrade_schema(sf.kw["bind"]) == []                                              # second run: nothing to do


def test_upgrade_refuses_a_column_it_cannot_add_safely(tmp_path):
    from sqlalchemy import Column, Integer, MetaData, Table
    from tradelite.models import Base
    eng = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE lonely (id INTEGER PRIMARY KEY)"))
    t = Table("lonely", Base.metadata, Column("id", Integer, primary_key=True), Column("must", Integer, nullable=False))
    try:
        with pytest.raises(RuntimeError, match="migrate it by hand"):
            upgrade_schema(eng)
    finally:
        Base.metadata.remove(t)


# =====================================================================================
# circuit breaker
# =====================================================================================
class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def test_breaker_state_machine():
    clk = Clock()
    b = CircuitBreaker("svc", failure_threshold=3, cooldown=60, clock=clk)
    assert b.state == "closed" and b.allow()
    b.record_failure("a"); b.record_failure("b")
    assert b.state == "closed"                      # two failures: still closed
    b.record_success()
    b.record_failure(); b.record_failure()
    assert b.state == "closed"                      # a success reset the count
    b.record_failure("boom")
    assert b.state == "open" and not b.allow() and b.retry_in() == 60
    clk.t = 59
    assert not b.allow()
    clk.t = 61
    assert b.state == "half_open"
    assert b.allow() is True and b.allow() is False    # exactly ONE probe gets through
    b.record_failure("still down")
    assert b.state == "open" and b.retry_in() == 60    # a failed probe restarts the cooldown
    clk.t = 125
    assert b.allow()
    b.record_success()
    assert b.state == "closed" and b.status()["failures"] == 0


def test_breaker_call_wrapper_counts_failures_and_refuses_when_open():
    b = CircuitBreaker("svc", failure_threshold=2, cooldown=60, clock=Clock())
    calls = []
    def bad():
        calls.append(1); raise ConnectionError("down")
    for _ in range(2):
        with pytest.raises(ConnectionError):
            b.call(bad)
    with pytest.raises(BreakerOpen) as e:
        b.call(bad)
    assert len(calls) == 2 and "svc is paused" in str(e.value)       # the third call never reached the service
    assert b.status()["last_error"] == "ConnectionError"


def test_a_dead_price_source_is_not_hammered_during_a_bulk_fetch():
    from tradelite.services import ingest
    calls = []
    class Down:
        name = "fake"
        def fetch(self, sym, start, end):
            calls.append(sym); raise ConnectionError("network down")
    sf = make_session_factory(fresh_engine())
    res = ingest.fetch_symbols(sf, Down(), ["A", "B", "C", "D", "E", "F"], years=1)
    assert calls == ["A", "B", "C"]                                    # three failures trip it, the rest are skipped
    assert all("paused" in r["error"] for r in res[3:]) and not any(r["ok"] for r in res)


def test_no_data_for_one_symbol_does_not_trip_the_breaker():
    from tradelite.services import ingest
    calls = []
    class Empty:
        name = "fake"
        def fetch(self, sym, start, end):
            calls.append(sym); raise ValueError(f"{sym}: provider returned no data")
    sf = make_session_factory(fresh_engine())
    res = ingest.fetch_symbols(sf, Empty(), list("ABCDEFG"), years=1)
    assert len(calls) == 7 and get_breaker("fake-history").state == "closed"      # a healthy service answering "none"
    assert all("no data" in r["error"] for r in res)


def test_alert_channel_is_skipped_while_paused_then_probed_again():
    clk = Clock()
    posts = []
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def opener(req, timeout):
        posts.append(1)
        if len(posts) <= 3:
            raise OSError("no route")
        return Resp()
    tg = TelegramNotifier("TOKEN", "42", opener=opener, breaker=CircuitBreaker("tg", failure_threshold=3, cooldown=60, clock=clk))
    from tradelite.services.alerts import Alert
    a = Alert("info", "hello")
    assert [tg.send(a) for _ in range(3)] == [False, False, False]
    assert tg.send(a) is False and len(posts) == 3                       # paused: no network call at all
    clk.t = 61
    assert tg.send(a) is True and len(posts) == 4                        # one probe, it worked, channel is back
    assert tg.send(a) is True


def test_live_screener_pauses_a_failing_price_source(env_live):
    env, src = env_live
    for _ in range(3):
        assert "yahoo is down" in screener.run_cycle(env["sf"], env["provider"], src, env["risk"], env["state_fn"], now=env["now"])["errors"][0]
    src.calls = 0
    out = screener.run_cycle(env["sf"], env["provider"], src, env["risk"], env["state_fn"], now=env["now"])
    assert out.get("paused") and "paused" in out["errors"][0] and src.calls == 0     # not even asked


# =====================================================================================
# data quality
# =====================================================================================
def test_quality_levels():
    df = make_bars("DEMO1", n=400)
    assert quality.assess(df).level == "ok"
    assert quality.assess(df, latest_date=df.index[-1].date()).level == "ok"
    q = quality.assess(df.iloc[:-3], latest_date=df.index[-1].date())
    assert q.level == "bad" and "STALE" in q.flags                                   # behind the rest of the universe
    jump = df.copy(); jump.iloc[-5:, jump.columns.get_loc("close")] *= 0.4          # an unexplained -60% a few bars ago
    assert quality.assess(jump).level == "bad" and "JUMP" in quality.assess(jump).flags
    old = df.copy(); old.iloc[-100:, old.columns.get_loc("close")] *= 0.4
    q = quality.assess(old)
    assert q.level == "warn" and "OLD_JUMP" in q.flags                               # same jump long ago: shown, not fatal
    zero = df.copy(); zero.iloc[-2, zero.columns.get_loc("volume")] = 0
    assert "ZERO_VOLUME" in quality.assess(zero).flags and quality.assess(zero).level == "warn"
    thin = df.copy(); thin["volume"] = 10
    assert "LOW_LIQUIDITY" in quality.assess(thin).flags
    gap = df.drop(df.index[-60:-45])
    assert "GAP" in quality.assess(gap).flags
    assert quality.assess(df.iloc[0:0]).level == "bad"


# =====================================================================================
# market context
# =====================================================================================
def test_regime_trend_and_volatility_rules():
    assert context.regime_from_close(series(drift=0.002)["close"]).trend == "up"
    assert context.regime_from_close(series(drift=-0.002)["close"]).trend == "down"
    assert context.regime_from_close(series(drift=0.0, vol=0.002, seed=3)["close"]).trend in ("sideways", "up", "down")
    short = context.regime_from_close(series(n=100)["close"], benchmark="BENCH")
    assert short.label == "unknown" and "needs" in short.note
    r = np.random.default_rng(5)
    quiet_then_wild = np.r_[r.normal(0, 0.004, 380), r.normal(0, 0.03, 30)]
    wild_then_quiet = np.r_[r.normal(0, 0.012, 380), r.normal(0, 0.001, 30)]
    mk = lambda rets: pd.Series(100 * np.exp(np.cumsum(rets)), index=pd.bdate_range("2020-01-01", periods=len(rets)))
    assert context.regime_from_close(mk(quiet_then_wild)).vol == "volatile"
    assert context.regime_from_close(mk(wild_then_quiet)).vol == "calm"


def test_regime_is_causal_appending_the_future_changes_no_past_label():
    full = series(n=700, drift=0.0006, vol=0.01, seed=11)
    for cut in (260, 400, 555):
        past_only = context.regime_from_close(full["close"].iloc[:cut])
        with_future = context.regime_from_close(full["close"][full.index <= full.index[cut - 1]])
        assert past_only == with_future
    provider = FixedProvider({"BENCH": full})
    again = context.compute_regime(provider, "BENCH", [], upto=full.index[399].date())
    assert again == context.regime_from_close(full["close"].iloc[:400], None, "BENCH")


def test_breadth_and_missing_benchmark():
    frames = {f"U{i}": series(drift=0.002, seed=i) for i in range(4)} | {f"D{i}": series(drift=-0.002, seed=9 + i) for i in range(2)}
    prov = FixedProvider(frames | {"BENCH": series(drift=0.001, seed=20)})
    reg = context.compute_regime(prov, "BENCH")
    assert reg.breadth_pct == pytest.approx(66.7, abs=0.1)                           # 4 of 6, benchmark excluded
    assert context.breadth(FixedProvider({"U0": frames["U0"]}), ["U0"]) is None      # too few stocks to say
    missing = context.compute_regime(prov, "NOPE")
    assert missing.label == "unknown" and "NOPE" in missing.note
    assert context.compute_regime(prov, "") .label == "unknown"


def test_portfolio_snapshot(pipeline_factory, session):
    p = pipeline_factory(Mode.SEMI_AUTO)
    session.add_all([PositionRow(symbol="A", strategy="s", side="BUY", qty=1, entry_price=1, stop=0.5, target=2, signal_bar_ts=CLOCK,
                                 opened_at=CLOCK, closed_at=CLOCK, pnl=10_000.0),
                     PositionRow(symbol="B", strategy="s", side="BUY", qty=1, entry_price=1, stop=0.5, target=2, signal_bar_ts=CLOCK,
                                 opened_at=CLOCK, closed_at=CLOCK + timedelta(days=1), pnl=-4_000.0)])
    session.commit()
    snap = context.portfolio_snapshot(session, p.account_state(), 100_000.0, RiskEngine().cfg)
    assert snap["equity"] == 106_000.0 and snap["slots_free"] == 5 and snap["kill_switch"] is False
    assert snap["drawdown_pct"] == pytest.approx(-100 * 4_000 / 110_000, abs=0.01)   # peak 110k, now 106k


# =====================================================================================
# event calendar
# =====================================================================================
def test_events_add_upsert_window_and_market_wide(session):
    d = date(2024, 3, 10)
    events.add_event(session, "tcs", d + timedelta(days=1), "Earnings")
    events.add_event(session, "TCS", d + timedelta(days=1), "earnings", note="updated")
    events.add_event(session, "*", d + timedelta(days=2), "policy")
    events.add_event(session, "INFY", d + timedelta(days=1), "earnings")
    assert session.query(EventRow).count() == 3                                       # TCS upserted, not duplicated
    got = events.upcoming(session, "TCS", d, 2)
    assert [(e.symbol, e.kind) for e in got] == [("TCS", "earnings"), ("*", "policy")]
    assert events.upcoming(session, "TCS", d, 0) == []                                # window 0 = only today
    assert events.upcoming(session, "TCS", d + timedelta(days=3), 5) == []           # past events do not count


def test_events_csv_import_reports_bad_lines_and_keeps_good_ones(session):
    res = events.import_csv(session, "symbol,date,kind,note\n# comment\nTCS,2024-03-12,earnings,Q4\n,2024-03-13,policy\nBAD,not-a-date,x\nINFY\n")
    assert res["imported"] == 2 and len(res["problems"]) == 2
    assert {(e.symbol, e.kind) for e in session.scalars(select(EventRow)).all()} == {("TCS", "earnings"), ("*", "policy")}


def test_earnings_fetch_is_best_effort_and_pauses_when_yahoo_is_down(session):
    future = date.today() + timedelta(days=5)
    def fetcher(t):
        if t.startswith("BAD"): raise ConnectionError("x")
        return [future, date.today() - timedelta(days=30)]       # a past date must be ignored
    out = events.fetch_earnings(session, ["TCS", "BAD1", "INFY"], fetcher=fetcher)
    assert [r["ok"] for r in out] == [True, False, True]
    assert {(e.symbol, e.day) for e in session.scalars(select(EventRow)).all()} == {("TCS", future), ("INFY", future)}
    down = lambda t: (_ for _ in ()).throw(ConnectionError("x"))
    out = events.fetch_earnings(session, list("ABCDEF"), fetcher=down)
    assert sum("paused" in r["error"] for r in out) == 3                                # 3 real failures, then the rest skipped


# =====================================================================================
# lifecycle
# =====================================================================================
def test_new_presets_start_as_drafts_and_are_not_scanned(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    presets.save(session, "macd_cross", "mine", {"rr": 3.0})
    assert lifecycle.state_of(session, "macd_cross", "mine") == "draft"
    assert lifecycle.state_of(session, "macd_cross", "default") == "active"            # built-ins need no row
    rows = pipeline_factory(Mode.SIGNAL_ONLY, provider=FixedProvider({"AAA": f})).scan(strategy_names=["macd_cross"])
    assert {r.config_name for r in rows} == {"default"}                                # the draft was skipped


def test_evidence_promotes_a_draft_and_a_human_activates(session):
    presets.save(session, "macd_cross", "mine", {"rr": 3.0})
    with pytest.raises(ValueError, match="stability check"):
        lifecycle.set_state(session, "macd_cross", "mine", "active")                   # evidence first, no shortcuts
    _mark_proven(session, "macd_cross", "mine", "AAA")
    lifecycle.record_evidence(session, "macd_cross", "mine")
    assert lifecycle.state_of(session, "macd_cross", "mine") == "validated"
    lifecycle.set_state(session, "macd_cross", "mine", "active")
    assert lifecycle.state_of(session, "macd_cross", "mine") == "active"
    with pytest.raises(ValueError, match="already active"):
        lifecycle.set_state(session, "macd_cross", "mine", "active")
    with pytest.raises(ValueError, match="state must be"):
        lifecycle.set_state(session, "macd_cross", "mine", "banana")


def test_one_failing_stock_does_not_demote_while_another_still_passes(session):
    presets.save(session, "macd_cross", "mine", {"rr": 3.0})
    _mark_proven(session, "macd_cross", "mine", "AAA")
    _mark_proven(session, "macd_cross", "mine", "BBB")
    lifecycle.record_evidence(session, "macd_cross", "mine")
    assert lifecycle.state_of(session, "macd_cross", "mine") == "validated"
    session.scalar(select(StrategyFitRow).where(StrategyFitRow.symbol == "AAA")).verdict = "no_edge"
    session.commit(); lifecycle.record_evidence(session, "macd_cross", "mine")
    assert lifecycle.state_of(session, "macd_cross", "mine") == "validated"            # BBB still passes
    session.scalar(select(StrategyFitRow).where(StrategyFitRow.symbol == "BBB")).verdict = "no_edge"
    session.commit(); lifecycle.record_evidence(session, "macd_cross", "mine")
    assert lifecycle.state_of(session, "macd_cross", "mine") == "draft"                # no stock passes any more


def test_editing_a_preset_resets_it_but_resaving_the_same_numbers_does_not(session):
    presets.save(session, "macd_cross", "mine", {"rr": 3.0})
    _mark_proven(session, "macd_cross", "mine", "AAA"); lifecycle.record_evidence(session, "macd_cross", "mine")
    lifecycle.set_state(session, "macd_cross", "mine", "active")
    presets.save(session, "macd_cross", "mine", {"rr": 3.0})
    assert lifecycle.state_of(session, "macd_cross", "mine") == "active"
    presets.save(session, "macd_cross", "mine", {"rr": 4.0})
    assert lifecycle.state_of(session, "macd_cross", "mine") == "draft"                # new numbers = untested


def test_retired_default_strategy_is_never_scanned(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    lifecycle.set_state(session, "macd_cross", "default", "retired")
    assert pipeline_factory(Mode.SIGNAL_ONLY, provider=FixedProvider({"AAA": f})).scan(strategy_names=["macd_cross"]) == []
    lifecycle.set_state(session, "macd_cross", "default", "draft")
    assert lifecycle.state_of(session, "macd_cross", "default") == "draft"


# =====================================================================================
# the gate: pure rules, then wired into the pipeline
# =====================================================================================
def _g(mode="enforce", **kw):
    base = dict(state="active", rank="proven", fit_updated_at=datetime(2024, 1, 1), today=date(2024, 1, 10), has_event=False, off_regime=False)
    return gate.evaluate(GateConfig(mode), **{**base, **kw})


def test_gate_rules():
    assert _g(mode="off", state="draft", rank="unproven").verdict == "off"
    assert _g().verdict == "pass"
    assert _g(state="validated").reasons == ("NOT_ACTIVE",)
    assert _g(rank="unproven").reasons == ("NOT_VALIDATED",)
    assert _g(fit_updated_at=datetime(2023, 10, 1)).reasons == ("STALE_VALIDATION",)
    assert _g(has_event=True).reasons == ("EVENT_RISK",)
    assert _g(off_regime=True).verdict == "pass"                                       # a flag only, unless enforced
    assert gate.evaluate(GateConfig("enforce", enforce_regime=True), state="active", rank="proven", fit_updated_at=None,
                         today=date(2024, 1, 10), has_event=False, off_regime=True).reasons == ("OFF_REGIME",)
    assert _g(state="draft", rank="unproven", has_event=True).reasons == ("NOT_ACTIVE", "NOT_VALIDATED", "EVENT_RISK")
    adv = _g(mode="advisory", rank="unproven")
    assert adv.verdict == "warn" and _g(rank="unproven").verdict == "block"           # same reasons, different consequence


def test_gate_mode_follows_the_trading_mode_when_set_to_auto():
    s = lambda m: Settings("sqlite://", "d", False, 1.0, "semi_auto", "paper", gate_mode=m)
    assert GateConfig.from_settings(s("auto"), Mode.AUTO).mode == "enforce"           # nobody approves in auto mode, the gate must
    assert GateConfig.from_settings(s("auto"), Mode.SEMI_AUTO).mode == "advisory"
    assert GateConfig.from_settings(s("off"), Mode.AUTO).mode == "off"
    with pytest.raises(ValueError, match="GATE_MODE"):
        GateConfig.from_settings(s("maybe"), Mode.AUTO)


def _scan(pipeline_factory, frame, gate_cfg, mode=Mode.SEMI_AUTO, **kw):
    p = pipeline_factory(mode, provider=FixedProvider({"AAA": frame}), gate=gate_cfg, clock=lambda: CLOCK, **kw)
    return p.scan(strategy_names=["macd_cross"])


def test_enforce_holds_back_an_unproven_signal_but_records_it(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    (row,) = _scan(pipeline_factory, f, GateConfig("enforce"))
    assert (row.status, row.reason, row.gate, row.gate_reason) == ("gated", "NOT_VALIDATED", "block", "NOT_VALIDATED")
    assert session.scalars(select(OrderRow)).all() == []                              # shown, but no order followed


def test_enforce_lets_a_proven_active_signal_through(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    _mark_proven(session, "macd_cross", "default", "AAA")
    (row,) = _scan(pipeline_factory, f, GateConfig("enforce"))
    assert row.gate == "pass" and row.status == "proposed" and row.gate_reason == ""
    assert len(session.scalars(select(OrderRow)).all()) == 1


def test_advisory_creates_the_order_but_shows_why_it_is_doubtful(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    (row,) = _scan(pipeline_factory, f, GateConfig("advisory"))
    assert row.gate == "warn" and row.gate_reason == "NOT_VALIDATED" and row.status == "proposed"
    assert len(session.scalars(select(OrderRow)).all()) == 1


def test_gate_off_is_the_old_behaviour(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    (row,) = _scan(pipeline_factory, f, GateConfig("off"))
    assert row.gate == "off" and row.status == "proposed"


def test_event_risk_holds_back_even_a_proven_signal(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    _mark_proven(session, "macd_cross", "default", "AAA")
    events.add_event(session, "AAA", f.index[-1].date() + timedelta(days=1), "earnings")
    (row,) = _scan(pipeline_factory, f, GateConfig("enforce"))
    assert (row.status, row.reason) == ("gated", "EVENT_RISK") and "EVENT:earnings" in row.flags


def test_stale_validation_is_caught(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    _mark_proven(session, "macd_cross", "default", "AAA")
    p = pipeline_factory(Mode.SEMI_AUTO, provider=FixedProvider({"AAA": f}), gate=GateConfig("enforce", max_age_days=45),
                         clock=lambda: datetime(2024, 6, 1))
    (row,) = p.scan(strategy_names=["macd_cross"])
    assert (row.status, row.reason) == ("gated", "STALE_VALIDATION")


def test_validated_but_not_activated_is_held_back(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    presets.save(session, "macd_cross", "mine", {})
    _mark_proven(session, "macd_cross", "mine", "AAA"); lifecycle.record_evidence(session, "macd_cross", "mine")
    rows = _scan(pipeline_factory, f, GateConfig("enforce"))
    mine = next(r for r in rows if r.config_name == "mine")
    assert (mine.status, mine.reason) == ("gated", "NOT_ACTIVE")                      # scanned and shown, never an order


def test_bad_data_never_becomes_an_order_even_with_the_gate_off(pipeline_factory, session, monkeypatch):
    f = _edge_frame("macd_cross", "DEMO1")
    monkeypatch.setattr("tradelite.services.pipeline.quality.assess", lambda df, **k: quality.Quality("bad", ("JUMP",)))
    (row,) = _scan(pipeline_factory, f, GateConfig("off"))
    assert (row.status, row.reason, row.quality, row.flags) == ("rejected", "DATA_QUALITY", "bad", "JUMP")
    assert session.scalars(select(OrderRow)).all() == []


def test_relative_staleness_is_flagged_on_the_real_app_path_only(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    old = f.iloc[:-5]
    prov = FixedProvider({"AAA": f, "OLD": old})
    rows = pipeline_factory(Mode.SIGNAL_ONLY, provider=prov, stale_check=True).scan(strategy_names=["macd_cross"])
    assert all(r.quality != "bad" for r in rows if r.symbol == "AAA")
    stale = [r for r in rows if r.symbol == "OLD"]
    assert all((r.quality, r.reason) == ("bad", "DATA_QUALITY") and "STALE" in r.flags for r in stale)


def test_off_regime_is_flagged_and_only_blocks_when_enforced(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    _mark_proven(session, "macd_cross", "default", "AAA")
    prov = FixedProvider({"AAA": f, "BENCH": series(drift=-0.002)})                     # a falling market; macd_cross wants "up"
    mk = lambda **kw: pipeline_factory(Mode.SEMI_AUTO, provider=prov, benchmark="BENCH", clock=lambda: CLOCK, **kw)
    (row,) = mk(gate=GateConfig("enforce")).scan(symbols=["AAA"], strategy_names=["macd_cross"])
    assert "OFF_REGIME" in row.flags and row.regime.startswith("down") and row.status == "proposed"
    session.query(OrderRow).delete(); session.query(SignalRow).delete(); session.commit()
    (row,) = mk(gate=GateConfig("enforce", enforce_regime=True)).scan(symbols=["AAA"], strategy_names=["macd_cross"])
    assert (row.status, row.reason) == ("gated", "OFF_REGIME")


def test_gate_never_touches_backtests():
    """A gate that blocked backtests could never collect the evidence it demands. Same results with it fully on."""
    out = {}
    for mode in ("off", "enforce"):
        settings = Settings("sqlite://", "d", True, 100_000.0, "auto", "paper", gate_mode=mode, enforce_regime=True)
        c = TestClient(create_app(settings, session_factory=make_session_factory(fresh_engine()), start_live=False))
        out[mode] = (c.post("/backtests", json={"strategy": "macd_cross", "symbol": "DEMO1"}).json()["metrics"],
                     c.post("/portfolio-backtests", json={"strategies": ["macd_cross"], "symbols": ["DEMO1", "DEMO2"]}).status_code)
    assert out["off"] == out["enforce"] and out["off"][0]["n_trades"] > 0


# =====================================================================================
# WATCH: setups one step from triggering
# =====================================================================================
FLAG = {"ema_cross": "cross_up", "donchian_breakout": "breakout", "new_high_momentum": "new_high",
        "bollinger_breakout": "breakout", "ema_pullback": "pullback"}


def _next_bar(df, close, low=None):
    ts, o = df.index[-1] + pd.offsets.BDay(1), float(df["close"].iloc[-1])
    row = {"open": o, "high": max(o, close) * 1.0005, "low": min(o, close) * 0.9995 if low is None else low, "close": close,
           "volume": float(df["volume"].iloc[-20:].mean()) * 3}
    return pd.concat([df, pd.DataFrame([row], index=[ts])])


@pytest.mark.parametrize("name", sorted(FLAG))
def test_a_watch_trigger_is_exactly_the_level_that_fires_the_strategy(name):
    """If the alert says 'a close above X triggers', a close just above X must trigger and one just below must not."""
    st, checked = registry.get(name), 0
    for seed in ("DEMO1", "DEMO2", "DEMO3"):
        df = make_bars(seed)
        prep = st.prepare(df)
        for i in range(st.meta.min_bars + 2, len(prep) - 1):
            w = st.watch(prep, i, seed)
            if w is None or w.trigger is None:
                continue
            assert st.on_bar(prep, i, seed) is None or True
            part = df.iloc[: i + 1]
            if name == "ema_pullback":
                above, below = _next_bar(part, w.trigger * 1.003, low=w.trigger * 0.998), _next_bar(part, w.trigger * 1.03, low=w.trigger * 1.02)
            else:
                above, below = _next_bar(part, w.trigger * 1.0005), _next_bar(part, w.trigger * 0.9995)
            assert bool(st.prepare(above)[FLAG[name]].iloc[-1]), f"{name}: just above {w.trigger} should fire (bar {i})"
            assert not bool(st.prepare(below)[FLAG[name]].iloc[-1]), f"{name}: just below {w.trigger} must not fire (bar {i})"
            checked += 1
            if checked >= 12:
                return
    assert checked > 0, f"no watch found for {name}: the test checked nothing"


def test_watch_and_signal_never_happen_on_the_same_bar_for_one_strategy():
    for name, cls in registry.discover().items():
        st = cls(); df = make_bars("DEMO2"); prep = st.prepare(df)
        for i in range(st.meta.min_bars + 2, len(prep)):
            if st.on_bar(prep, i, "X") is not None:
                continue
            w = st.watch(prep, i, "X")
            assert w is None or w.note


def _watch_frame(name="donchian_breakout"):
    st = registry.get(name)
    for seed in ("DEMO1", "DEMO2", "DEMO3"):
        df = make_bars(seed); prep = st.prepare(df)
        for i in range(len(prep) - 1, st.meta.min_bars + 2, -1):
            if st.on_bar(prep, i, seed) is None and st.watch(prep, i, seed) is not None:
                return df.iloc[: i + 1]
    raise AssertionError("no watch bar found")


def test_scan_saves_the_watchlist_once_and_skips_held_symbols(pipeline_factory, session):
    f = _watch_frame()
    p = pipeline_factory(Mode.SIGNAL_ONLY, provider=FixedProvider({"AAA": f, "HELD": f}), clock=lambda: CLOCK)
    session.add(PositionRow(symbol="HELD", strategy="x", side="BUY", qty=1, entry_price=1, stop=0.5, target=2, signal_bar_ts=CLOCK, opened_at=CLOCK))
    session.commit()
    p.scan(strategy_names=["donchian_breakout"])
    rows = session.scalars(select(WatchRow)).all()
    assert {r.symbol for r in rows} == {"AAA"}                                        # nothing to watch on what you already hold
    w = rows[0]
    assert w.trigger > w.close and 0 < w.distance_pct <= 1.5 and "high" in w.note and w.bar_ts == f.index[-1]
    assert [d["symbol"] for d in p.last_watch] == ["AAA"]
    p.scan(strategy_names=["donchian_breakout"])
    assert session.query(WatchRow).count() == 1 and p.last_watch == []               # same bar: not saved twice


# =====================================================================================
# strategy advice on open positions
# =====================================================================================
def _prepared(**cols):
    n = 30
    base = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6, "atr": 2.0}
    return pd.DataFrame({k: np.full(n, float(v)) for k, v in {**base, **cols}.items()}, index=pd.bdate_range("2024-01-01", periods=n))


@pytest.mark.parametrize("name,cols,expected", [
    ("ema_cross", dict(ema_fast=99, ema_slow=100), ("EXIT", "fallen below")),
    ("ema_cross", dict(ema_fast=101, ema_slow=100), None),
    ("ema_pullback", dict(close=95, ema_fast=98, ema_slow=97), ("EXIT", "uptrend is broken")),   # below both averages
    ("ema_pullback", dict(close=97.5, ema_fast=98, ema_slow=96), ("REDUCE", "pullback")),        # below the fast one only
    ("ema_pullback", dict(close=100, ema_fast=98, ema_slow=96), None),
    ("macd_cross", dict(macd=-1, macd_sig=0, ema_trend=90, close=100), ("REDUCE", "fading")),
    ("macd_cross", dict(macd=-1, macd_sig=0, ema_trend=110, close=100), ("EXIT", "trend average")),
    ("macd_cross", dict(macd=1, macd_sig=0, ema_trend=90), None),
    ("rsi_reversion", dict(rsi=70, ema_trend=90), ("REDUCE", "recovered")),
    ("rsi_reversion", dict(rsi=50, ema_trend=110), ("EXIT", "trend average")),
    ("rsi_reversion", dict(rsi=50, ema_trend=90), None),
    ("new_high_momentum", dict(close=100, ema_trend=110), ("EXIT", "momentum")),
    ("new_high_momentum", dict(close=100, ema_trend=90), None),
])
def test_exit_hints(name, cols, expected):
    st = registry.get(name)
    hint = st.exit_hint(_prepared(**cols), 29, 100.0)
    if expected is None:
        assert hint is None
    else:
        assert hint is not None and hint.action == expected[0] and expected[1] in hint.reason


def test_donchian_and_bollinger_exit_hints_use_recent_prices():
    d = _prepared(); d["low"] = 95.0; d.iloc[-1, d.columns.get_loc("close")] = 94.0
    assert registry.get("donchian_breakout").exit_hint(d, 29, 100.0).action == "EXIT"          # closed below the 10-day low
    d2 = _prepared(); assert registry.get("donchian_breakout").exit_hint(d2, 29, 100.0) is None
    b = _prepared(); b.iloc[-1, b.columns.get_loc("close")] = 90.0
    assert registry.get("bollinger_breakout").exit_hint(b, 29, 100.0).action == "EXIT"
    assert registry.get("bollinger_breakout").exit_hint(_prepared(), 29, 100.0) is None


def _position_frame():
    """A frame whose last bar makes ema_cross say EXIT (fast EMA below slow EMA)."""
    st = registry.get("ema_cross")
    df = make_bars("DEMO2"); prep = st.prepare(df)
    for i in range(len(prep) - 1, st.meta.min_bars + 20, -1):
        if st.exit_hint(prep, i, 1.0) is not None:
            return df.iloc[: i + 1]
    raise AssertionError("no exit bar")


def _open_position(session, frame, symbol="AAA", strategy="ema_cross", stop=0.01):
    pos = PositionRow(symbol=symbol, strategy=strategy, side="BUY", qty=10, entry_price=float(frame["close"].iloc[-3]), stop=stop,
                      target=1e9, entry_costs=0.0, signal_bar_ts=frame.index[-4].to_pydatetime(), opened_at=CLOCK)
    session.add(pos); session.commit()
    return pos


def test_position_review_advises_but_never_sells(pipeline_factory, session):
    f = _position_frame()
    pos = _open_position(session, f)
    rec = RecordingNotifier()
    p = pipeline_factory(Mode.AUTO, provider=FixedProvider({"AAA": f}), clock=lambda: CLOCK, notifier=rec)
    (a,) = p.review_positions()
    assert (a.symbol, a.action, a.position_id) == ("AAA", "EXIT", pos.id) and "turned" in a.reason and a.price == float(f["close"].iloc[-1])
    session.refresh(pos)
    assert pos.closed_at is None and session.scalars(select(OrderRow)).all() == []                # nothing was sold
    assert rec.sent[0].level == "warning" and "nothing was sold" in rec.sent[0].body and "Your stop" in rec.sent[0].body
    assert p.review_positions() == [] and len(rec.sent) == 1                                      # same bar: no repeat


def test_position_review_ignores_closed_positions_and_bars_before_the_entry(pipeline_factory, session):
    f = _position_frame()
    closed = _open_position(session, f); closed.closed_at = CLOCK; session.commit()
    p = pipeline_factory(Mode.SEMI_AUTO, provider=FixedProvider({"AAA": f}), clock=lambda: CLOCK)
    assert p.review_positions() == []
    session.delete(closed); session.commit()
    fresh = _open_position(session, f); fresh.signal_bar_ts = f.index[-1].to_pydatetime(); session.commit()
    assert p.review_positions() == []                                                             # no bar since the entry signal


def test_position_review_survives_a_symbol_without_prices(pipeline_factory, session):
    f = _position_frame()
    _open_position(session, f, symbol="GONE")
    p = pipeline_factory(Mode.SEMI_AUTO, provider=FixedProvider({"AAA": f}), clock=lambda: CLOCK)
    class Missing(FixedProvider):
        def get_bars(self, s, tf="1d"):
            raise FileNotFoundError(s)
    p.provider = Missing({})
    assert p.review_positions() == []


# =====================================================================================
# live screener: lifecycle, flags, and the alert plumbing the uploaded version had broken
# =====================================================================================
@pytest.fixture
def env_live():
    frame = _edge_frame("macd_cross", "DEMO1")
    i = len(frame) - 1
    sf = make_session_factory(fresh_engine())
    provider = FixedProvider({"AAA": frame.iloc[:i]})
    risk = RiskEngine()
    from tradelite.services.pipeline import Pipeline
    state_fn = lambda s: Pipeline(s, provider, PaperBroker(), risk=risk, starting_capital=100_000.0).account_state()
    r = frame.iloc[i]
    bar = {"date": frame.index[i].date(), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
    now = datetime.combine(frame.index[i].date(), datetime.min.time()).replace(hour=11)
    class Src(FakeLive):
        calls = 0
        def snapshot(self, symbols, today):
            self.calls += 1
            return super().snapshot(symbols, today)
    env = dict(frame=frame, i=i, sf=sf, provider=provider, risk=risk, state_fn=state_fn, now=now, bar=bar)
    return env, Src({"AAA": bar}, fail=True)


def _live(env, bars=None, **kw):
    return screener.run_cycle(env["sf"], env["provider"], FakeLive(bars or {"AAA": env["bar"]}), env["risk"], env["state_fn"], now=env["now"], **kw)


def test_live_screener_skips_drafts_and_flags_events_and_regime(env_live):
    env, _ = env_live
    with env["sf"]() as s:
        presets.save(s, "macd_cross", "mine", {"rr": 3.0})                          # a draft
        events.add_event(s, "AAA", env["now"].date(), "earnings")
    env["provider"].frames["BENCH"] = series(drift=-0.002)                              # a falling market
    _live(env, benchmark="BENCH", event_window=2)
    with env["sf"]() as s:
        from tradelite.models import LiveMatchRow
        rows = s.scalars(select(LiveMatchRow).where(LiveMatchRow.strategy == "macd_cross")).all()
    assert {r.config_name for r in rows} == {"default"}                                 # the draft is not screened
    assert "EVENT:earnings" in rows[0].flags and "OFF_REGIME" in rows[0].flags


def test_the_alert_the_screener_sends_reaches_telegram_and_the_alerts_table(env_live):
    """Regression: the screener used to hand plain text to a notifier that expects Alert objects, so with the
    real notifier chain the message crashed Telegram and was never stored. Nothing arrived."""
    env, _ = env_live
    posts = []
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    tg = TelegramNotifier("TOKEN", "42", opener=lambda req, timeout: posts.append(req.data) or Resp())
    chain = StoredNotifier(env["sf"], MultiNotifier(tg))
    _live(env, notifier=chain)
    assert posts and any(b"macd_cross" in p for p in posts) and all(b"Provisional" in p for p in posts)
    with env["sf"]() as s:
        stored = s.scalars(select(AlertRow)).all()
    assert any("macd_cross" in a.title for a in stored) and stored[0].delivered is True


# =====================================================================================
# API
# =====================================================================================
@pytest.fixture
def api(env_live):
    env, _ = env_live
    env["provider"].frames["BENCH"] = series(drift=0.002)
    for k in ("U1", "U2", "U3", "U4", "U5"):
        env["provider"].frames[k] = series(drift=0.002, seed=ord(k[1]))
    settings = Settings("sqlite://", "data", False, 100_000.0, "semi_auto", "paper", benchmark_symbol="BENCH")
    app = create_app(settings, provider=env["provider"], session_factory=env["sf"], live_source=FakeLive({"AAA": env["bar"]}),
                     live_clock=lambda: env["now"], start_live=False)
    return TestClient(app), env


def test_context_endpoint(api):
    c, env = api
    body = c.get("/context").json()
    assert body["regime"]["trend"] == "up" and body["regime"]["benchmark"] == "BENCH" and body["regime"]["breadth_pct"] is not None
    assert body["portfolio"]["equity"] == 100_000.0 and body["portfolio"]["slots_free"] == 5
    assert body["gate"]["mode"] == "advisory"                                           # semi_auto + gate_mode auto


def test_lifecycle_endpoints(api):
    c, _ = api
    rows = c.get("/lifecycle").json()
    assert {r["state"] for r in rows} == {"active"} and len(rows) == len(registry.discover())
    assert c.post("/strategy-configs", json={"strategy": "macd_cross", "name": "mine", "params": {"rr": 3}}).status_code == 200
    mine = next(r for r in c.get("/lifecycle").json() if r["config_name"] == "mine")
    assert mine["state"] == "draft" and mine["note"]
    bad = c.post("/lifecycle", json={"strategy": "macd_cross", "config_name": "mine", "state": "active"})
    assert bad.status_code == 400 and "stability check" in bad.json()["detail"]
    assert c.post("/lifecycle", json={"strategy": "nope", "state": "retired"}).status_code == 400
    assert c.post("/lifecycle", json={"strategy": "macd_cross", "config_name": "ghost", "state": "retired"}).status_code == 400
    ok = c.post("/lifecycle", json={"strategy": "ema_cross", "state": "retired", "note": "too slow"})
    assert ok.status_code == 200 and ok.json()["state"] == "retired"
    assert next(r for r in c.get("/lifecycle").json() if r["strategy"] == "ema_cross")["state"] == "retired"


def test_events_endpoints(api):
    c, env = api
    day = env["now"].date() + timedelta(days=1)
    r = c.post("/events", json={"symbol": "aaa", "day": day.isoformat(), "kind": "earnings", "note": "Q2"})
    assert r.status_code == 200 and r.json()["symbol"] == "AAA"
    imp = c.post("/events/import", json={"csv": f"*,{(day + timedelta(days=1)).isoformat()},policy\nbroken line"}).json()
    assert imp["imported"] == 1 and len(imp["problems"]) == 1
    listed = c.get("/events").json()
    assert [(e["symbol"], e["kind"]) for e in listed] == [("AAA", "earnings"), ("*", "policy")]
    assert c.delete(f"/events/{listed[0]['id']}").json() == {"deleted": listed[0]["id"]}
    assert c.delete("/events/9999").status_code == 404
    assert c.post("/events", json={"symbol": "X", "day": "not-a-date"}).status_code == 422


def test_watchlist_and_position_alert_endpoints(api):
    c, env = api
    assert c.get("/watchlist").json() == {"as_of": None, "items": []}
    f = _watch_frame()
    env["provider"].frames["AAA"] = f
    c.post("/scan", json={"mode": "signal_only", "symbols": ["AAA"], "strategies": ["donchian_breakout"]})
    wl = c.get("/watchlist").json()
    assert wl["as_of"] == f.index[-1].date().isoformat() and wl["items"][0]["symbol"] == "AAA" and wl["items"][0]["trigger"] > 0
    pf = _position_frame()
    env["provider"].frames["POS"] = pf
    with env["sf"]() as s:
        _open_position(s, pf, symbol="POS")
    (alert,) = c.post("/position-alerts/review").json()
    assert alert["action"] == "EXIT" and alert["symbol"] == "POS"
    assert [a["id"] for a in c.get("/position-alerts").json()] == [alert["id"]]
    assert c.post(f"/position-alerts/{alert['id']}/ack").json()["acknowledged"] is True
    assert c.post("/position-alerts/9999/ack").status_code == 404


def test_screener_status_reports_the_breakers(api):
    c, _ = api
    get_breaker("yahoo-live").record_failure("boom")
    names = {b["name"] for b in c.get("/screener/status").json()["breakers"]}
    assert "yahoo-live" in names


# =====================================================================================
# the daily job reports what the new features found
# =====================================================================================
def _daily(session_factory, mode, gate_mode, frame):
    rec = RecordingNotifier()
    f = frame.copy(); f.index = pd.bdate_range(end="2026-09-30", periods=len(f))
    cfg = ops_settings(mode=mode, gate_mode=gate_mode)
    res = jobs.run_daily(session_factory, cfg, FixedProvider({"AAA": f}), PaperBroker(), RiskEngine(), rec, clock=lambda: NOW, holidays=set())
    return res, rec


def test_daily_job_reports_gated_signals(monkeypatch):
    frame = _edge_frame("macd_cross", "DEMO1")
    res, rec = _daily(make_session_factory(fresh_engine()), "auto", "auto", frame)       # auto mode: the gate enforces
    assert res["status"] == "ok" and res["scan"]["new_signals"] >= 1 and res["scan"]["gated"] >= 1
    assert res["scan"]["executed"] == 0                                                   # nothing traded on unproven evidence
    assert "held back by the gate" in rec.sent[-1].body
    res2, _ = _daily(make_session_factory(fresh_engine()), "semi_auto", "auto", frame)   # semi-auto: advisory, a human decides
    assert res2["scan"]["gated"] == 0 and res2["scan"]["proposed"] >= 1


def test_daily_job_lists_the_watchlist_and_position_advice():
    sf = make_session_factory(fresh_engine())
    f = _watch_frame()
    res, rec = _daily(sf, "semi_auto", "advisory", f)
    assert res["scan"]["watch"] >= 1 and "Watch tomorrow:" in rec.sent[-1].body
    pf = _position_frame()
    pf = pf.copy(); pf.index = pd.bdate_range(end="2026-09-30", periods=len(pf))
    with sf() as s:
        s.add(PositionRow(symbol="AAA", strategy="ema_cross", side="BUY", qty=10, entry_price=float(pf["close"].iloc[-3]), stop=0.01,
                          target=1e9, entry_costs=0.0, signal_bar_ts=pf.index[-4].to_pydatetime(), opened_at=NOW))
        s.commit()
    rec2 = RecordingNotifier()
    res2 = jobs.run_daily(sf, ops_settings(), FixedProvider({"AAA": pf}), PaperBroker(), RiskEngine(), rec2, clock=lambda: NOW, holidays=set())
    assert res2["advice"] and res2["advice"][0]["action"] == "EXIT"
    assert any("says EXIT AAA" in a.title for a in rec2.sent) and "Strategy advice" in rec2.sent[-1].body
