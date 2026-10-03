import logging
import urllib.error
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FixedProvider, fresh_engine, make_df
from fastapi.testclient import TestClient

from tradelens.api.auth import MIN_TOKEN_LEN, is_public
from tradelens.api.main import create_app
from tradelens.broker.paper import PaperBroker
from tradelens.config import Settings
from tradelens.data.synthetic import DemoProvider, make_bars
from tradelens.db import make_session_factory
from tradelens.domain import AccountState, Side
from tradelens.models import AlertRow, JobRunRow, PositionRow
from tradelens.risk.engine import RiskEngine
from tradelens.services import jobs
from tradelens.services.alerts import (Alert, MultiNotifier, RecordingNotifier, StoredNotifier, TelegramNotifier,
                                       WebhookNotifier, build_notifier)
from tradelens.services.pipeline import Pipeline
from tradelens.services.schedule import (due, expected_last_bar, is_trading_day, load_holidays, parse_hhmm,
                                         previous_trading_day)

TOKEN = "t" * 32
NOW = datetime(2026, 9, 30, 16, 45)   # a Wednesday, after the close


def settings(**kw) -> Settings:
    base = dict(database_url="sqlite://", data_dir="data", demo=False, starting_capital=100_000.0,
                mode="semi_auto", broker="paper")
    base.update(kw)
    return Settings(**base)


def position(symbol="X", side="BUY", qty=100, entry=100.0, costs=10.0, **kw) -> PositionRow:
    return PositionRow(symbol=symbol, strategy="t", side=side, qty=qty, entry_price=entry, stop=entry * 0.8,
                       target=None, entry_costs=costs, signal_bar_ts=datetime(2020, 1, 1), opened_at=datetime(2020, 1, 2), **kw)


# ---------------------------------------------------------------- unrealized P&L in equity

def test_equity_includes_open_losses(session):
    df = make_df([(100, 101, 99, 100)] * 5 + [(90, 91, 89, 90)])            # last close 90
    p = Pipeline(session, FixedProvider({"X": df}), PaperBroker())
    session.add(position())
    session.flush()
    st = p.account_state()
    assert st.unrealized_pnl == pytest.approx(-1000.0)
    assert st.equity == pytest.approx(100_000 - 1000 - 10)                  # was 99_990 before the fix
    assert st.unpriced_positions == 0


def test_short_positions_gain_when_price_falls(session):
    df = make_df([(100, 101, 99, 100)] * 5 + [(90, 91, 89, 90)])
    p = Pipeline(session, FixedProvider({"X": df}), PaperBroker())
    session.add(position(side="SELL"))
    session.flush()
    assert p.account_state().unrealized_pnl == pytest.approx(1000.0)


def test_position_without_a_price_is_marked_at_entry_and_flagged(session):
    p = Pipeline(session, FixedProvider({}), PaperBroker())
    session.add(position(symbol="GONE"))
    session.flush()
    st = p.account_state()
    assert st.unrealized_pnl == 0.0 and st.unpriced_positions == 1
    assert st.equity == pytest.approx(100_000 - 10)


def test_open_loss_shrinks_the_next_position_size(session):
    from conftest import buy_signal
    df = make_df([(100, 101, 99, 100)] * 5 + [(50, 51, 49, 50)])            # open position lost half
    p = Pipeline(session, FixedProvider({"X": df}), PaperBroker())
    before = RiskEngine().evaluate(buy_signal(symbol="Y"), p.account_state()).qty
    session.add(position(qty=1000, entry=100.0))
    session.flush()
    after = RiskEngine().evaluate(buy_signal(symbol="Y"), p.account_state())
    assert after.qty < before or not after.approved


# ---------------------------------------------------------------- alerts

class FakeResp:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_telegram_posts_to_the_bot_api_and_truncates():
    seen = {}
    def opener(req, timeout):
        seen["url"], seen["body"] = req.full_url, req.data
        return FakeResp()
    n = TelegramNotifier("123:ABC", "42", opener=opener)
    assert n.send(Alert("critical", "big", "x" * 10_000)) is True
    assert seen["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    import json
    body = json.loads(seen["body"])
    assert body["chat_id"] == "42" and len(body["text"]) <= 3900 and body["text"].startswith("🚨")


def test_delivery_failure_never_raises_and_never_logs_the_token(caplog):
    def opener(req, timeout):
        raise urllib.error.URLError(f"cannot reach {req.full_url}")
    n = TelegramNotifier("SECRET:TOKEN", "42", opener=opener)
    with caplog.at_level(logging.ERROR):
        assert n.send(Alert("info", "hi")) is False
    assert "SECRET" not in caplog.text and "URLError" in caplog.text


def test_webhook_accepts_only_http_urls():
    with pytest.raises(ValueError):
        WebhookNotifier("file:///etc/passwd")
    with pytest.raises(ValueError):
        WebhookNotifier("ftp://example.com/x")
    WebhookNotifier("https://hooks.example.com/abc")


def test_bad_alert_level_is_rejected():
    with pytest.raises(ValueError):
        Alert("fatal", "x")


def test_multi_notifier_survives_a_crashing_channel_and_reports_delivery():
    class Boom:
        def send(self, a): raise RuntimeError("down")
    rec = RecordingNotifier()
    assert MultiNotifier(Boom(), rec).send(Alert("info", "x")) is True
    assert len(rec.sent) == 1
    assert MultiNotifier(Boom()).send(Alert("info", "x")) is False


def test_stored_notifier_records_every_alert_even_when_delivery_fails():
    sf = make_session_factory(fresh_engine())
    class Down:
        def send(self, a): return False
    StoredNotifier(sf, Down(), clock=lambda: NOW).send(Alert("warning", "t", "b"))
    StoredNotifier(sf, RecordingNotifier(), clock=lambda: NOW).send(Alert("info", "ok"))
    with sf() as s:
        rows = s.query(AlertRow).order_by(AlertRow.id).all()
    assert [(r.level, r.delivered) for r in rows] == [("warning", False), ("info", True)]


def test_secrets_do_not_appear_in_settings_repr():
    s = settings(api_token="supersecret" * 4, telegram_bot_token="botsecret", alert_webhook_url="https://x/secret")
    assert "supersecret" not in repr(s) and "botsecret" not in repr(s) and "/secret" not in repr(s)


def test_build_notifier_without_channels_only_logs():
    sf = make_session_factory(fresh_engine())
    assert build_notifier(settings(), sf).send(Alert("info", "x")) is False


def test_failed_exit_order_raises_a_critical_alert(session):
    class Rejecting(PaperBroker):
        def place_order(self, **kw):
            fill = super().place_order(**kw)
            return type(fill)(fill.order_id, fill.symbol, fill.side, fill.qty, fill.price, "REJECTED")
    df = make_df([(100, 101, 99, 100)] * 3 + [(100, 101, 70, 72)])          # stop at 80 is breached
    rec = RecordingNotifier()
    p = Pipeline(session, FixedProvider({"X": df}), Rejecting(), notifier=rec)
    session.add(position())
    session.flush()
    assert p.check_exits() == []                                             # position stays open
    assert rec.sent and rec.sent[0].level == "critical" and "EXIT ORDER FAILED" in rec.sent[0].title


# ---------------------------------------------------------------- auth

def make_client(token=TOKEN, tmp_path=None):
    app = create_app(settings(api_token=token), provider=DemoProvider(), notifier=RecordingNotifier(),
                     session_factory=make_session_factory(fresh_engine()),
                     frontend_dist=tmp_path)
    return TestClient(app)


def test_api_requires_the_token():
    c = make_client()
    assert c.get("/account").status_code == 401
    assert c.get("/account").headers["www-authenticate"] == "Bearer"
    assert c.get("/account", headers={"Authorization": "Bearer wrong" + "t" * 30}).status_code == 401
    assert c.get("/account", headers={"Authorization": TOKEN}).status_code == 401           # scheme required
    assert c.get("/account", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_state_changing_routes_are_protected_too():
    c = make_client()
    for method, path in (("post", "/kill-switch"), ("post", "/scan"), ("post", "/orders/1/approve"),
                         ("post", "/data/fetch"), ("get", "/alerts"), ("get", "/jobs"), ("get", "/docs")):
        r = c.get(path) if method == "get" else c.post(path, json={})
        assert r.status_code == 401, path


def test_health_and_dashboard_files_stay_public(tmp_path):
    (tmp_path / "index.html").write_text("<html>ui</html>")
    c = make_client(tmp_path=tmp_path)
    h = c.get("/health")
    assert h.status_code == 200 and h.json()["auth_required"] is True
    assert c.get("/ui/").status_code == 200
    assert c.get("/", follow_redirects=False).status_code in (302, 307)
    assert is_public("/ui/assets/x.js") and not is_public("/uix") and not is_public("/account")


def test_cors_preflight_needs_no_token():
    c = make_client()
    r = c.options("/account", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200


def test_short_token_is_refused_at_startup():
    with pytest.raises(RuntimeError, match=str(MIN_TOKEN_LEN)):
        make_client(token="short")


def test_no_token_means_auth_off_and_health_says_so():
    c = make_client(token="")
    assert c.get("/account").status_code == 200 and c.get("/health").json()["auth_required"] is False


def test_kill_switch_change_sends_an_alert():
    rec = RecordingNotifier()
    app = create_app(settings(api_token=TOKEN), provider=DemoProvider(), notifier=rec,
                     session_factory=make_session_factory(fresh_engine()))
    c = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    assert c.post("/kill-switch", json={"active": True}).status_code == 200
    assert rec.sent[0].level == "warning" and "ON" in rec.sent[0].title
    assert c.get("/account").json()["unrealized_pnl"] == 0.0


# ---------------------------------------------------------------- schedule

def test_trading_days_and_holidays(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text("# comment\n2026-10-02  # Gandhi Jayanti\n\n")
    hol = load_holidays(f)
    assert hol == {date(2026, 10, 2)}
    assert not is_trading_day(date(2026, 10, 3), hol) and not is_trading_day(date(2026, 10, 2), hol)
    assert previous_trading_day(date(2026, 10, 5), hol) == date(2026, 10, 1)
    assert load_holidays(None) == set() and load_holidays("") == set()


def test_bad_holiday_line_names_the_line(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text("2026-10-02\nnot a date\n")
    with pytest.raises(ValueError, match="line 2"):
        load_holidays(f)


def test_expected_last_bar():
    assert expected_last_bar(datetime(2026, 9, 30, 16, 45), set()) == date(2026, 9, 30)
    assert expected_last_bar(datetime(2026, 9, 30, 10, 0), set()) == date(2026, 9, 29)
    assert expected_last_bar(datetime(2026, 10, 3, 12, 0), set()) == date(2026, 10, 2)          # Saturday
    assert expected_last_bar(datetime(2026, 9, 30, 16, 45), {date(2026, 9, 30)}) == date(2026, 9, 29)


def test_parse_hhmm():
    assert parse_hhmm("16:30") == time(16, 30)
    with pytest.raises(ValueError):
        parse_hhmm("4pm")


def test_due_rules():
    run_at = time(16, 30)
    hol: set = set()
    assert due(NOW, run_at, [], hol)
    assert not due(datetime(2026, 9, 30, 16, 29), run_at, [], hol)                       # too early
    assert not due(datetime(2026, 10, 3, 17, 0), run_at, [], hol)                        # Saturday
    assert not due(NOW, run_at, [], {NOW.date()})                                        # holiday
    assert not due(NOW, run_at, [("ok", NOW - timedelta(minutes=5))], hol)               # already done
    assert not due(NOW, run_at, [("failed", NOW - timedelta(minutes=5))], hol)           # too soon to retry
    assert due(NOW, run_at, [("failed", NOW - timedelta(minutes=20))], hol)
    three = [("failed", NOW - timedelta(minutes=m)) for m in (90, 60, 40)]
    assert not due(NOW, run_at, three, hol)                                              # gave up
    assert not due(NOW, run_at, [("running", NOW - timedelta(minutes=5))], hol)          # someone is on it
    assert due(NOW, run_at, [("running", NOW - timedelta(minutes=45))], hol)             # crashed run: stale


# ---------------------------------------------------------------- daily job

def fresh_frames(*names, end="2026-09-30"):
    out = {}
    for n in names:
        df = make_bars(n, n=400)
        df.index = pd.bdate_range(end=end, periods=len(df))
        out[n] = df
    return out


class FakePipeline:
    """Stands in for Pipeline so the job's wiring (order, alerts, records) is tested in isolation."""
    log: list = []
    last_watch: list = []
    def __init__(self, signals=()):
        self.signals = list(signals)
    def review_positions(self):
        return []
    def check_exits(self):
        FakePipeline.log.append("exits")
        return [SimpleNamespace(symbol="AAA", exit_reason="stop", pnl=-1234.0, strategy="ema_cross", side="BUY", qty=10,
                                entry_price=100.0, exit_price=90.0)]
    def scan(self, symbols=None, **kw):
        FakePipeline.log.append(("scan", tuple(symbols)))
        return self.signals
    def account_state(self):
        return AccountState(equity=98_000.0, cash=50_000.0, open_positions=1, exposure=48_000.0, realized_pnl_today=-3_500.0,
                            unrealized_pnl=-500.0)


def proposed(sym="AAA"):
    return SimpleNamespace(id=1, strategy="ema_cross", symbol=sym, status="proposed", reason=None, suggested_qty=25,
                           entry=250.0, stop=240.0, target=270.0, rank="proven")


@pytest.fixture
def env(monkeypatch):
    FakePipeline.log = []
    monkeypatch.setattr(jobs, "build_pipeline", lambda *a, **k: FakePipeline([proposed(), proposed("BBB")]))
    sf = make_session_factory(fresh_engine())
    return sf, RecordingNotifier()


def go(env, provider, cfg=None, ping=None):
    sf, rec = env
    return jobs.run_daily(sf, cfg or settings(), provider, PaperBroker(), RiskEngine(), rec, clock=lambda: NOW,
                          holidays=set(), ping=ping)


def test_daily_run_order_alerts_and_record(env):
    pings = []
    res = go(env, FixedProvider(fresh_frames("AAA", "BBB")), settings(healthcheck_ping_url="https://hc.example/x"), pings.append)
    sf, rec = env
    assert res["status"] == "ok" and pings == ["https://hc.example/x"]
    assert FakePipeline.log[0] == "exits" and FakePipeline.log[1] == ("scan", ("AAA", "BBB"))     # exits before scan
    titles = [a.title for a in rec.sent]
    assert any(t.startswith("AAA closed at stop") for t in titles)
    assert titles.count("Approve? BUY 25 AAA @ ~250.00") == 1 and any("BBB" in t for t in titles)
    assert any(t == "Daily loss limit reached" for t in titles)                     # -3500 vs 3% of 98_000
    assert titles[-1] == "Daily run complete"
    with sf() as s:
        row = s.query(JobRunRow).one()
    assert row.status == "ok" and row.finished_at == NOW and row.detail["scan"]["proposed"] == 2


def test_stale_symbols_are_skipped_but_the_rest_are_scanned(env):
    frames = fresh_frames("AAA") | fresh_frames("OLD", end="2026-09-25")
    res = go(env, FixedProvider(frames))
    assert res["status"] == "ok" and res["stale"] == ["OLD"]
    assert FakePipeline.log[1] == ("scan", ("AAA",))
    assert env[1].sent[-1].level == "warning"


def test_all_stale_fails_loudly_and_does_not_scan_or_ping(env):
    pings = []
    res = go(env, FixedProvider(fresh_frames("AAA", end="2026-09-25")), settings(healthcheck_ping_url="https://hc.example/x"), pings.append)
    assert res["status"] == "failed" and "Nothing was scanned" in res["error"] and pings == []
    assert FakePipeline.log == []
    assert env[1].sent[-1].level == "critical" and env[1].sent[-1].title == "Daily run FAILED"
    with env[0]() as s:
        assert s.query(JobRunRow).one().status == "failed"


def test_no_data_at_all_fails_with_a_clear_message(env):
    res = go(env, FixedProvider({}))
    assert res["status"] == "failed" and "fetch history first" in res["error"]


def test_a_crash_inside_the_job_is_recorded_not_raised(env, monkeypatch):
    class Boom(FakePipeline):
        def scan(self, *a, **k): raise RuntimeError("provider exploded")
    monkeypatch.setattr(jobs, "build_pipeline", lambda *a, **k: Boom())
    res = go(env, FixedProvider(fresh_frames("AAA")))
    assert res["status"] == "failed" and "provider exploded" in res["error"]


def test_refresh_uses_the_full_history_window(env):
    calls = []
    class Src:
        name = "fake"
        def fetch(self, symbol, start, end):
            calls.append((symbol, (end - start).days))
            return fresh_frames(symbol)[symbol]
    sf, rec = env
    res = jobs.run_daily(sf, settings(refresh_years=5), FixedProvider(fresh_frames("AAA")), PaperBroker(), RiskEngine(), rec,
                         source=Src(), clock=lambda: NOW, holidays=set())
    assert res["fetch"]["ok"] == 1 and calls[0][1] > 5 * 360           # adjusted prices need the whole history, not a tail


def test_worker_runs_once_per_day_and_announces_itself(env):
    sf, rec = env
    jobs.worker_loop(sf, settings(), FixedProvider(fresh_frames("AAA")), PaperBroker(), RiskEngine(), rec, None,
                     clock=lambda: NOW, sleep=lambda s: None, max_ticks=3, poll_seconds=1)
    with sf() as s:
        assert s.query(JobRunRow).count() == 1                          # ticks 2 and 3 saw today's ok run
    assert rec.sent[0].title == "Worker started"


def test_worker_gives_up_after_three_failures_and_says_so(env):
    sf, rec = env
    t = {"now": NOW}
    def clock(): return t["now"]
    def sleep(_): t["now"] += timedelta(seconds=0)                      # time moves only when we say so
    for k in range(4):
        t["now"] = NOW + timedelta(minutes=20 * k)
        jobs.worker_loop(sf, settings(), FixedProvider({}), PaperBroker(), RiskEngine(), rec, None,
                         clock=clock, sleep=sleep, max_ticks=1, poll_seconds=1)
    with sf() as s:
        assert s.query(JobRunRow).filter_by(status="failed").count() == 3      # the 4th tick did not run
    assert [a.title for a in rec.sent].count("Daily run gave up after 3 attempts") == 1


def test_alerts_and_jobs_endpoints(env):
    sf, rec = env
    app = create_app(settings(api_token=TOKEN), provider=FixedProvider({}), notifier=StoredNotifier(sf, rec, clock=lambda: NOW),
                     session_factory=sf)
    c = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    go(env, FixedProvider({}))
    assert c.get("/jobs").json()[0]["status"] == "failed"
    c.post("/kill-switch", json={"active": False})
    assert c.get("/alerts").json()[0]["title"].startswith("Kill switch")
