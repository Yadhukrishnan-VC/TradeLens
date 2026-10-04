"""Live screener: provisional matches on today's forming bar, fading, alerts, API, batch 1:5 backtests."""
from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import FixedProvider, fresh_engine
from test_features import _edge_frame, _mark_proven
from tradelens.api.main import create_app
from tradelens.broker.paper import PaperBroker
from tradelens.config import Settings
from tradelens.data.live import YahooLive, aggregate_today
from tradelens.data.synthetic import make_bars
from tradelens.db import make_session_factory
from tradelens.models import LiveMatchRow, StrategyConfigRow, StrategyFitRow
from tradelens.risk.engine import RiskEngine
from tradelens.services import screener
from tradelens.services.pipeline import Pipeline
from tradelens.strategies import registry


class FakeLive:
    name = "fake"

    def __init__(self, bars=None, fail=False):
        self.bars, self.fail = bars or {}, fail

    def snapshot(self, symbols, today):
        if self.fail:
            raise RuntimeError("yahoo is down")
        return {s: b for s, b in self.bars.items() if s in symbols}


from tradelens.services.alerts import RecordingNotifier as Recorder   # the app's real alert type: Alert objects


def _bar_of(frame, i):
    r = frame.iloc[i]
    return {"date": frame.index[i].date(), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}


@pytest.fixture
def env():
    """History up to yesterday + today's forming bar for which macd_cross (default settings) fires."""
    frame = _edge_frame("macd_cross", "DEMO1")
    i = len(frame) - 1
    sf = make_session_factory(fresh_engine())
    provider = FixedProvider({"AAA": frame.iloc[:i]})           # stored history: up to yesterday
    risk = RiskEngine()
    state_fn = lambda s: Pipeline(s, provider, PaperBroker(), risk=risk, starting_capital=100_000.0).account_state()
    now = datetime.combine(frame.index[i].date(), datetime.min.time()).replace(hour=11, minute=0)
    return dict(frame=frame, i=i, sf=sf, provider=provider, risk=risk, state_fn=state_fn, now=now,
                bar=_bar_of(frame, i))


def _cycle(env, source, notifier=None, now=None):
    return screener.run_cycle(env["sf"], env["provider"], source, env["risk"], env["state_fn"],
                              now=now or env["now"], notifier=notifier)


def test_market_hours():
    assert screener.market_open(datetime(2026, 9, 30, 9, 15)) and screener.market_open(datetime(2026, 9, 30, 15, 30))
    assert not screener.market_open(datetime(2026, 9, 30, 9, 14)) and not screener.market_open(datetime(2026, 9, 30, 15, 31))
    assert not screener.market_open(datetime(2026, 10, 3, 11, 0))            # Saturday


def test_live_match_is_what_the_daily_scan_would_produce(env):
    """Same strategy code, same bar: the provisional signal equals the daily on_bar() result."""
    out = _cycle(env, FakeLive({"AAA": env["bar"]}))
    assert out["priced"] == 1 and out["errors"] == []
    strat = registry.get("macd_cross")
    expected = strat.on_bar(strat.prepare(env["frame"]), env["i"], "AAA")
    with env["sf"]() as s:
        m = s.scalar(select(LiveMatchRow).where(LiveMatchRow.strategy == "macd_cross"))
    assert (m.entry, m.stop, m.target, m.status, m.side) == (expected.entry, expected.stop, expected.target, "live", "BUY")
    assert m.rr == pytest.approx(2.0) and m.trading_day == env["now"].date()
    assert m.rank == "unproven" and m.fit != ""


def test_alert_once_then_fade_when_the_bar_stops_matching(env):
    rec = Recorder()
    src = FakeLive({"AAA": env["bar"]})
    _cycle(env, src, rec)
    _cycle(env, src, rec, now=env["now"].replace(minute=5))                   # same match again
    macd_alerts = [t for t in rec.sent if "macd_cross" in t.title]
    assert len(macd_alerts) == 1 and "Provisional" in macd_alerts[0].body           # no repeat spam
    last_close = float(env["frame"]["close"].iloc[env["i"] - 1])
    src.bars["AAA"] = {**env["bar"], "open": last_close, "high": last_close, "low": last_close, "close": last_close}
    out = _cycle(env, src, rec, now=env["now"].replace(minute=10))
    with env["sf"]() as s:
        rows = s.scalars(select(LiveMatchRow).where(LiveMatchRow.strategy == "macd_cross")).all()
        assert len(rows) == 1 and rows[0].status == "faded"
    assert out["faded"] >= 1


def test_proven_pair_is_ranked_proven_live(env):
    with env["sf"]() as s:
        _mark_proven(s, "macd_cross", "default", "AAA", pf=2.3)
    rec = Recorder()
    _cycle(env, FakeLive({"AAA": env["bar"]}), rec)
    with env["sf"]() as s:
        m = s.scalar(select(LiveMatchRow).where(LiveMatchRow.strategy == "macd_cross"))
    assert (m.rank, m.rank_score) == ("proven", 2.3)
    assert "PROVEN" in [t for t in rec.sent if "macd_cross" in t.title][0].title


def test_saved_presets_are_screened_too(env):
    with env["sf"]() as s:
        s.add(StrategyConfigRow(strategy="macd_cross", name="rr5", params={"rr": 5.0}, created_at=datetime.now()))
        s.commit()
    _cycle(env, FakeLive({"AAA": env["bar"]}))
    with env["sf"]() as s:
        m = s.scalar(select(LiveMatchRow).where(LiveMatchRow.config_name == "rr5"))
    assert m is not None and m.rr == pytest.approx(5.0)
    assert (m.target - m.entry) == pytest.approx(5 * (m.entry - m.stop))       # a true 1:5 target


def test_stale_or_missing_or_broken_data_never_crashes(env):
    old = {**env["bar"], "date": date(2020, 1, 1)}                            # yesterday's bar, not today's
    assert _cycle(env, FakeLive({"AAA": old}))["priced"] == 0
    assert _cycle(env, FakeLive({}))["priced"] == 0
    out = _cycle(env, FakeLive(fail=True))
    assert out["errors"] and "yahoo is down" in out["errors"][0]
    with env["sf"]() as s:
        assert s.scalars(select(LiveMatchRow)).all() == []


def test_failing_notifier_is_reported_not_fatal(env):
    class Boom:
        def send(self, alert):
            raise RuntimeError("telegram down")
    out = _cycle(env, FakeLive({"AAA": env["bar"]}), Boom())
    assert any("alert failed" in e for e in out["errors"]) and out["new"] >= 1


def test_stored_bar_for_today_is_replaced_not_duplicated(env):
    full = env["frame"]                                                       # includes today's (final) bar
    df = screener.with_live_bar(full, {**env["bar"], "close": env["bar"]["close"]}, env["now"].date())
    assert len(df) == len(full) and df.index.is_unique


# ---------- free intraday source ----------
def test_aggregate_five_minute_bars_into_one_daily_bar():
    idx = pd.date_range("2026-09-30 09:15", periods=4, freq="5min", tz="Asia/Kolkata")
    raw = pd.DataFrame({"Open": [100, 101, 102, 101.5], "High": [101, 103, 102.5, 102], "Low": [99.5, 100.5, 101, 100.8],
                        "Close": [100.8, 102, 101.6, 101.9], "Volume": [10, 20, 30, 40]}, index=idx)
    bar = aggregate_today(raw, date(2026, 9, 30))
    assert (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (100, 103, 99.5, 101.9, 100)
    assert aggregate_today(raw, date(2026, 9, 29)) is None                    # no bars for that day


def test_yahoo_live_maps_tickers_back_to_symbols():
    idx = pd.date_range("2026-09-30 09:15", periods=2, freq="5min", tz="Asia/Kolkata")
    frame = pd.DataFrame({"Open": [1, 2], "High": [2, 3], "Low": [1, 1], "Close": [2, 3], "Volume": [5, 5]}, index=idx)
    seen = []
    src = YahooLive(downloader=lambda tickers: seen.append(tickers) or {"TCS.NS": frame})
    out = src.snapshot(["TCS", "INFY"], date(2026, 9, 30))
    assert seen == [["TCS.NS", "INFY.NS"]] and list(out) == ["TCS"] and out["TCS"]["close"] == 3


# ---------- API ----------
@pytest.fixture
def client(env):
    settings = Settings("sqlite://", "data", False, 100_000.0, "semi_auto", "paper")
    app = create_app(settings, provider=env["provider"], session_factory=env["sf"], live_source=FakeLive({"AAA": env["bar"]}),
                     live_clock=lambda: env["now"], start_live=False)
    return TestClient(app)


def test_screener_page_lists_every_strategy_with_its_matches(client):
    assert client.post("/screener/run").json()["ran"] is True                  # 11:00 on a weekday
    data = client.get("/screener").json()
    names = [s["name"] for s in data["strategies"]]
    assert {"ema_cross", "macd_cross", "rsi_reversion", "bollinger_breakout"} <= set(names)
    macd = next(s for s in data["strategies"] if s["name"] == "macd_cross")
    m = macd["matches"][0]
    assert m["symbol"] == "AAA" and m["status"] == "live" and m["confirmed"] is False
    assert m["risk_per_share"] == pytest.approx(m["entry"] - m["stop"])
    assert data["status"]["market_open"] is True and data["status"]["last_result"]["priced"] == 1


def test_run_is_refused_when_the_market_is_closed_unless_forced(env):
    settings = Settings("sqlite://", "data", False, 100_000.0, "semi_auto", "paper")
    saturday = datetime(2026, 10, 3, 11, 0)
    c = TestClient(create_app(settings, provider=env["provider"], session_factory=env["sf"],
                              live_source=FakeLive({}), live_clock=lambda: saturday, start_live=False))
    r = c.post("/screener/run").json()
    assert r["ran"] is False and "closed" in r["reason"]
    assert c.post("/screener/run", params={"force": "true"}).json()["ran"] is True


def test_no_live_source_in_demo_mode():
    settings = Settings("sqlite://", "data", True, 100_000.0, "semi_auto", "paper")
    c = TestClient(create_app(settings, session_factory=make_session_factory(fresh_engine()), start_live=False))
    assert c.post("/screener/run", params={"force": "true"}).json()["ran"] is False
    assert c.get("/screener/status").json()["enabled"] is False


def test_batch_backtest_at_one_to_five_creates_a_separate_track_record():
    settings = Settings("sqlite://", "data", True, 100_000.0, "semi_auto", "paper")
    c = TestClient(create_app(settings, session_factory=make_session_factory(fresh_engine()), start_live=False))
    r = c.post("/backtests/batch", json={"strategies": ["macd_cross", "ema_pullback"], "symbols": ["DEMO1", "DEMO2"], "rr": 5}).json()
    assert r["config_name"] == "rr5" and r["ran"] == 4 and r["skipped"] == 0
    presets = c.get("/strategy-configs").json()
    assert sorted((p["strategy"], p["name"], p["params"]["rr"]) for p in presets) == [("ema_pullback", "rr5", 5.0), ("macd_cross", "rr5", 5.0)]
    assert {f["config_name"] for f in c.get("/fits").json()} == {"rr5"}       # not mixed into the default record
    assert c.post("/backtests/batch", json={"strategies": ["nope"]}).status_code == 400
    assert c.post("/backtests/batch", json={"rr": 0}).status_code == 422


def test_background_loop_finds_matches_by_itself(env):
    """Start the app for real (lifespan on): the polling thread runs a check with nobody clicking anything."""
    import time
    settings = Settings("sqlite://", "data", False, 100_000.0, "semi_auto", "paper", live_interval=1)
    rec = Recorder()
    app = create_app(settings, provider=env["provider"], session_factory=env["sf"], live_source=FakeLive({"AAA": env["bar"]}),
                     live_clock=lambda: env["now"], notifier=rec)
    with TestClient(app) as c:
        deadline = time.time() + 15
        while time.time() < deadline and not c.get("/screener/status").json()["last_run"]:
            time.sleep(0.2)
        status = c.get("/screener/status").json()
        assert status["last_run"] and status["last_result"]["priced"] == 1
        macd = next(s for s in c.get("/screener").json()["strategies"] if s["name"] == "macd_cross")
        assert [m["symbol"] for m in macd["matches"]] == ["AAA"]
    assert any("macd_cross" in t.title for t in rec.sent)                           # the alert went out too
