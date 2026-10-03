"""Presets, data ingestion into the database, and proven-first ranking."""
from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import FixedProvider, fresh_engine
from tradelite.api.main import create_app
from tradelite.config import Settings
from tradelite.data.db_provider import DbProvider
from tradelite.data.sources import YahooSource, clean_bars
from tradelite.data.synthetic import DemoProvider, make_bars
from tradelite.db import make_session_factory, normalize_url
from tradelite.domain import Mode
from tradelite.models import OrderRow, SignalRow, StrategyFitRow
from tradelite.risk.engine import RiskConfig, RiskEngine
from tradelite.services import ingest, lifecycle, presets
from tradelite.strategies import registry


@pytest.fixture
def client():
    settings = Settings("sqlite://", "data", True, 100_000.0, "semi_auto", "paper")
    app = create_app(settings, provider=DemoProvider(), session_factory=make_session_factory(fresh_engine()))
    return TestClient(app)


# ---------- strategy library ----------
def test_library_has_the_standard_strategies():
    assert {"ema_cross", "donchian_breakout", "rsi_reversion", "macd_cross", "ema_pullback",
            "bollinger_breakout", "new_high_momentum"} <= set(registry.discover())


# ---------- presets ----------
def test_preset_validation(client):
    ok = client.post("/strategy-configs", json={"strategy": "ema_cross", "name": "fast", "params": {"fast": 10, "rr": 2.5}})
    assert ok.status_code == 200 and ok.json()["params"] == {"fast": 10, "rr": 2.5}
    bad = [
        {"strategy": "ema_cross", "name": "default", "params": {"fast": 10}},      # reserved
        {"strategy": "ema_cross", "name": "x", "params": {"nope": 1}},             # unknown param
        {"strategy": "ema_cross", "name": "x", "params": {"fast": "ten"}},         # not a number
        {"strategy": "ema_cross", "name": "x", "params": {"fast": -1}},            # negative
        {"strategy": "nope", "name": "x", "params": {}},                           # unknown strategy
        {"strategy": "ema_cross", "name": "bad/name", "params": {}},               # bad name
    ]
    for body in bad:
        assert client.post("/strategy-configs", json=body).status_code == 400, body


def test_preset_crud_and_separate_track_record(client):
    client.post("/strategy-configs", json={"strategy": "macd_cross", "name": "slow", "params": {"slow": 40}})
    preset = client.get("/strategy-configs", params={"strategy": "macd_cross"}).json()
    assert [p["name"] for p in preset] == ["slow"]
    client.post("/backtests", json={"strategy": "macd_cross", "symbol": "DEMO1"})
    r = client.post("/backtests", json={"strategy": "macd_cross", "symbol": "DEMO1", "config_name": "slow"})
    assert r.status_code == 200 and r.json()["config_name"] == "slow"
    fits = client.get("/fits").json()
    assert sorted(f["config_name"] for f in fits) == ["default", "slow"]          # tracked separately
    assert client.delete(f"/strategy-configs/{preset[0]['id']}").json() == {"deleted": preset[0]["id"]}
    assert client.get("/strategy-configs").json() == []


def test_backtest_rejects_unsaved_params_and_unknown_preset(client):
    r = client.post("/backtests", json={"strategy": "macd_cross", "symbol": "DEMO1", "params": {"fast": 8}})
    assert r.status_code == 400 and "preset" in r.json()["detail"]
    assert client.post("/backtests", json={"strategy": "macd_cross", "symbol": "DEMO1", "config_name": "ghost"}).status_code == 400


# ---------- ranking ----------
def _edge_frame(name: str, seed: str) -> pd.DataFrame:
    """Demo history truncated so the LAST bar fires `name`."""
    df = make_bars(seed)
    strat = registry.get(name)
    prep = strat.prepare(df)
    for i in range(len(df) - 1, strat.meta.min_bars + 1, -1):
        if strat.on_bar(prep, i, seed) is not None:
            return df.iloc[: i + 1]
    raise AssertionError("no edge signal found")


def _mark_proven(session, strategy, config, symbol, pf=1.8):
    session.add(StrategyFitRow(strategy=strategy, config_name=config, symbol=symbol, timeframe="1d",
                               start=datetime(2020, 1, 1), end=datetime(2024, 1, 1), n_bars=1000, avg_volume=1e6,
                               n_trades=40, profit_factor=pf, expectancy=10.0, verdict="candidate", detail={},
                               updated_at=datetime(2024, 1, 2)))
    session.commit()


def test_signal_matching_a_proven_pair_is_ranked_proven(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    _mark_proven(session, "macd_cross", "default", "AAA")
    p = pipeline_factory(Mode.SEMI_AUTO, provider=FixedProvider({"AAA": f, "BBB": f}))
    rows = p.scan(strategy_names=["macd_cross"])
    by_symbol = {r.symbol: r for r in rows}
    assert by_symbol["AAA"].rank == "proven" and by_symbol["AAA"].rank_score == 1.8
    assert by_symbol["BBB"].rank == "unproven"                       # same strategy, different stock
    orders = {o.symbol: o for o in session.scalars(select(OrderRow)).all()}
    assert orders["AAA"].rank == "proven" and orders["BBB"].rank == "unproven"


def test_proven_signal_wins_the_last_position_slot(pipeline_factory, session):
    f = _edge_frame("macd_cross", "DEMO1")
    _mark_proven(session, "macd_cross", "default", "ZZZ")             # alphabetically LAST
    risk = RiskEngine(RiskConfig(max_open_positions=1))
    p = pipeline_factory(Mode.AUTO, provider=FixedProvider({"AAA": f, "ZZZ": f}), risk=risk)
    rows = {r.symbol: r for r in p.scan(strategy_names=["macd_cross"])}
    assert rows["ZZZ"].status == "executed"
    assert rows["AAA"].status == "rejected" and rows["AAA"].reason == "MAX_POSITIONS"


def test_rank_matches_the_exact_config_only(pipeline_factory, session):
    _mark_proven(session, "macd_cross", "default", "AAA")
    p = pipeline_factory(Mode.SIGNAL_ONLY)
    verdict, score, adjusted, reason = p.rank_for("macd_cross", "default", "AAA")
    assert verdict == "proven" and score == 1.8
    assert p.rank_for("macd_cross", "slow", "AAA")[0] == "unproven"    # a preset must earn its own record
    assert p.rank_for("macd_cross", "default", "BBB")[0] == "unproven"


def test_scan_runs_saved_presets_and_records_config(pipeline_factory, session):
    presets.save(session, "macd_cross", "quick", {"fast": 8, "slow": 21, "signal": 5})
    _mark_proven(session, "macd_cross", "quick", "DEMO1")
    lifecycle.record_evidence(session, "macd_cross", "quick")          # a passing test promotes the draft
    frames = {"DEMO1": make_bars("DEMO1")}
    strat = registry.get("macd_cross", fast=8, slow=21, signal=5)
    prep = strat.prepare(frames["DEMO1"])
    i = next(i for i in range(len(prep) - 1, strat.meta.min_bars, -1) if strat.on_bar(prep, i, "DEMO1"))
    p = pipeline_factory(Mode.SIGNAL_ONLY, provider=FixedProvider({"DEMO1": frames["DEMO1"].iloc[: i + 1]}))
    rows = p.scan(strategy_names=["macd_cross"])
    assert "quick" in {r.config_name for r in rows}
    assert p.scan(strategy_names=["macd_cross"]) == []                # re-scan never duplicates


def test_lists_show_proven_first_and_track_record_view(client):
    client.post("/backtests", json={"strategy": "donchian_breakout", "symbol": "DEMO2"})
    tr = client.get("/track-record").json()
    assert set(tr) == {"proven", "not_proven"}
    row = (tr["proven"] + tr["not_proven"])[0]
    assert {"symbol", "strategy", "config_name", "tested_from", "tested_to", "first_win", "last_win", "verified_at"} <= set(row)


# ---------- data providers + database ----------
def _fake_yahoo(ticker, start, end):
    df = make_bars("DEMO3", n=300)
    df.columns = [c.title() for c in df.columns]
    df.index = df.index.tz_localize("Asia/Kolkata") + pd.Timedelta(hours=9)
    if ticker.startswith("BAD"):
        return pd.DataFrame()
    return df


def test_yahoo_source_maps_nse_symbols_and_cleans():
    src = YahooSource(downloader=_fake_yahoo)
    assert src.ticker("TCS") == "TCS.NS" and src.ticker("^NSEI") == "^NSEI" and src.ticker("X.BO") == "X.BO"
    df = src.fetch("TCS", datetime(2020, 1, 1).date(), datetime(2021, 1, 1).date())
    assert df.index.tz is None and (df.index == df.index.normalize()).all() and len(df) == 300


def test_clean_bars_repairs_rounding_and_drops_junk():
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    raw = pd.DataFrame({"Open": [10, 10, 0], "High": [10.0, 9.99, 5], "Low": [9, 9, 4], "Close": [10.01, 10, 4], "Volume": [1, 1, 1]}, index=idx)
    out = clean_bars(raw)
    assert len(out) == 2 and out["high"].iloc[0] == 10.01            # high raised to bound close; zero-price row dropped


def test_ingest_is_idempotent_and_reports_failures():
    sf = make_session_factory(fresh_engine())
    src = YahooSource(downloader=_fake_yahoo)
    res = ingest.fetch_symbols(sf, src, ["TCS", "BADCO", "infy"], years=2)
    assert [r["ok"] for r in res] == [True, False, True] and "no data" in res[1]["error"]
    ingest.fetch_symbols(sf, src, ["TCS"], years=2)                   # again: no duplicates
    prov = DbProvider(sf)
    assert prov.symbols() == ["INFY", "TCS"]
    df = prov.get_bars("TCS")
    assert len(df) == 300 and df.index.is_unique
    with sf() as s:
        cov = ingest.coverage(s)
    assert {c["symbol"]: c["bars"] for c in cov} == {"INFY": 300, "TCS": 300}
    with pytest.raises(FileNotFoundError):
        prov.get_bars("NOPE")


def test_backtest_runs_on_database_prices(tmp_path):
    sf = make_session_factory(fresh_engine())
    ingest.fetch_symbols(sf, YahooSource(downloader=lambda *a: make_bars("DEMO2", n=1500).rename(columns=str.title)), ["ABC"])
    settings = Settings("sqlite://", "data", False, 100_000.0, "semi_auto", "paper")
    c = TestClient(create_app(settings, session_factory=sf))
    assert c.get("/symbols").json() == ["ABC"]
    assert c.get("/health").json()["data_source"] == "db"
    assert c.post("/backtests", json={"strategy": "ema_pullback", "symbol": "ABC"}).status_code == 200


def test_csv_import(tmp_path):
    make_bars("DEMO1", n=200).to_csv(tmp_path / "AAA.csv", index_label="date")
    sf = make_session_factory(fresh_engine())
    assert ingest.import_csv_dir(sf, str(tmp_path))[0]["bars"] == 200
    assert DbProvider(sf).symbols() == ["AAA"]


def test_database_url_normalised():
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"
