import numpy as np
import pandas as pd
import pytest
from conftest import FixedProvider, fresh_engine, make_df
from fastapi.testclient import TestClient

from tradelite.api.main import create_app
from tradelite.backtest.benchmark import buy_and_hold_curve, compare, equal_weight_curve
from tradelite.backtest.costs import CostModel
from tradelite.backtest.metrics import curve_stats
from tradelite.backtest.portfolio import run_portfolio_backtest
from tradelite.config import Settings
from tradelite.data.universe import IntervalUniverse, LiquidityUniverse, StaticUniverse, survivorship_report
from tradelite.db import make_session_factory
from tradelite.domain import Side, Signal
from tradelite.risk.engine import RiskConfig, RiskEngine
from tradelite.strategies.base import Strategy, StrategyMeta

ZERO_COST = CostModel(0, 0, 0, 0, 0, 0, 0, 0)
LOOSE = RiskEngine(RiskConfig(max_position_pct=1.0, min_rr=0.0))


class Scripted(Strategy):
    """Signals at the close of the bar positions listed per symbol: {"A": [1], "B": [1]}."""
    meta = StrategyMeta("scripted", "test", min_bars=0)
    default_params = {"at": {}, "stop_pct": 0.05, "target_pct": 0.10}

    def prepare(self, df):
        return df.copy()

    def on_bar(self, df, i, symbol):
        if i not in self.params["at"].get(symbol, []):
            return None
        c = float(df["close"].iloc[i])
        return Signal("scripted", symbol, Side.BUY, df.index[i].to_pydatetime(), c,
                      c * (1 - self.params["stop_pct"]), c * (1 + self.params["target_pct"]))


def flat(n=12, px=100.0, **kw):
    return make_df([(px, px + 1, px - 1, px)] * n, **kw)


def run(frames, at, risk=LOOSE, **kw):
    kw.setdefault("cost_model", ZERO_COST)
    kw.setdefault("slippage_bps", 0.0)
    kw.setdefault("capital", 100_000)
    return run_portfolio_backtest([Scripted(at=at)], frames, risk=risk, **kw)


def test_fill_is_next_open_and_curve_reconciles():
    df = make_df([(100, 101, 99, 100)] * 3 + [(102, 103, 101, 102)] * 3 + [(102, 130, 101, 125)] + [(125, 126, 124, 125)] * 3)
    r = run({"A": df}, {"A": [2]}, risk=RiskEngine(RiskConfig(max_position_pct=1.0, min_rr=0.0)))
    t = r.trades[0]
    assert t.entry_price == 102                                  # not the signal close of 100
    assert r.equity_curve.iloc[-1] == pytest.approx(100_000 + sum(x.net_pnl for x in r.trades))
    assert r.exposure.iloc[-1] == 0


def test_positions_compete_for_slots_and_liquid_symbol_goes_first():
    a, b = flat(), flat(volume=5e6)          # B is more liquid
    r = run({"A": a, "B": b}, {"A": [2], "B": [2]}, risk=RiskEngine(RiskConfig(max_position_pct=1.0, max_open_positions=1, min_rr=0.0)))
    assert [t.symbol for t in r.trades] == ["B"]
    assert r.skipped == {"MAX_POSITIONS": 1}


def test_shared_equity_after_a_loss_shrinks_next_position():
    # A loses at its stop, then B is sized from the reduced account
    a = make_df([(100, 101, 99, 100)] * 3 + [(100, 100, 90, 92)] + [(92, 93, 91, 92)] * 8)
    b = flat(volume=1e6)
    r = run({"A": a, "B": b}, {"A": [2], "B": [5]}, risk=RiskEngine(RiskConfig(risk_pct=0.02, max_position_pct=1.0, min_rr=0.0)))
    ta, tb = sorted(r.trades, key=lambda t: t.entry_ts)
    assert ta.net_pnl < 0
    assert tb.qty * 5 <= (100_000 + ta.net_pnl) * 0.02 + 5      # risk budget came from post-loss equity


def test_universe_blocks_signals_from_non_members(tmp_path):
    csv = tmp_path / "u.csv"
    csv.write_text("symbol,start,end\nA,2020-01-01,\nB,2021-06-01,\n")      # B not a member yet
    u = IntervalUniverse.from_csv(csv)
    r = run({"A": flat(), "B": flat()}, {"A": [2], "B": [2]}, universe=u)
    assert {t.symbol for t in r.trades} == {"A"}


def test_interval_universe_boundaries_and_reentry(tmp_path):
    csv = tmp_path / "u.csv"
    csv.write_text("symbol,start,end\nA,2020-01-01,2020-01-10\nA,2020-03-01,\n")
    u = IntervalUniverse.from_csv(csv)
    assert u.eligible(pd.Timestamp("2020-01-10")) == {"A"}         # end inclusive
    assert u.eligible(pd.Timestamp("2020-02-01")) == set()
    assert u.eligible(pd.Timestamp("2020-04-01")) == {"A"}


def test_interval_universe_rejects_bad_rows(tmp_path):
    csv = tmp_path / "u.csv"
    csv.write_text("symbol,start,end\nA,2020-05-01,2020-01-01\n")
    with pytest.raises(ValueError, match="line 2"):
        IntervalUniverse.from_csv(csv)


def test_delisted_symbol_is_closed_at_last_close():
    a = flat(n=6)                          # data stops at bar 5
    b = flat(n=14)                         # keeps the calendar going
    r = run({"A": a, "B": b}, {"A": [2]})
    t = r.trades[0]
    assert t.symbol == "A" and t.reason == "data_end" and t.exit_ts == a.index[-1]


def test_liquidity_universe_uses_only_prior_days():
    idx = pd.bdate_range("2020-01-01", periods=400)
    base = pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1e6}, index=idx)
    hi = base.copy()
    hi["volume"] = 5e6
    u1 = LiquidityUniverse({"A": base, "B": hi}, top_n=1, lookback=20, min_history=50)
    spike = hi.copy()
    spike.loc[idx[300]:, "volume"] = 1.0        # B collapses from day 300 onward
    u2 = LiquidityUniverse({"A": base, "B": spike}, top_n=1, lookback=20, min_history=50)
    assert u1.eligible(idx[299]) == u2.eligible(idx[299]) == {"B"}    # the future cannot change the past
    assert u1.eligible(idx[100]) == {"B"}
    assert u2.eligible(idx[399]) == {"A"}
    assert u1.eligible(idx[10]) == set()                               # not enough history yet


def test_survivorship_report_flags_static_survivor_only_sample():
    frames = {"A": flat(), "B": flat()}
    rep = survivorship_report(frames, StaticUniverse(["A", "B"]), frames["A"].index[0], frames["A"].index[-1])
    assert rep["bias_risk"] == "high" and len(rep["warnings"]) == 2
    rep = survivorship_report(frames, None, frames["A"].index[0], frames["A"].index[-1])
    assert rep["bias_risk"] == "high"


def test_survivorship_report_with_membership_lists_missing_price_data(tmp_path):
    csv = tmp_path / "u.csv"
    csv.write_text("symbol,start,end\nA,2020-01-01,\nGONE,2020-01-01,2020-01-08\n")
    u = IntervalUniverse.from_csv(csv)
    frames = {"A": flat()}
    rep = survivorship_report(frames, u, frames["A"].index[0], frames["A"].index[-1])
    assert rep["missing_price_data"] == ["GONE"] and rep["bias_risk"] == "moderate"


def test_curve_stats_basic():
    idx = pd.bdate_range("2020-01-01", periods=4)
    s = curve_stats(pd.Series([100.0, 110.0, 99.0, 121.0], index=idx), 100.0)
    assert s["total_return_pct"] == pytest.approx(21.0)
    assert s["max_drawdown_pct"] == pytest.approx(-10.0)


def test_buy_and_hold_pays_costs_and_tracks_price():
    df = make_df([(100, 100, 100, 100)] * 3 + [(100, 200, 100, 200)] * 3)
    cal = df.index
    free = buy_and_hold_curve(df, cal, 10_000, cost_model=ZERO_COST, slippage_bps=0)
    assert free.iloc[0] == pytest.approx(10_000) and free.iloc[-1] == pytest.approx(20_000)
    costly = buy_and_hold_curve(df, cal, 10_000, slippage_bps=10)
    assert costly.iloc[-1] < free.iloc[-1]


def test_buy_and_hold_rejects_benchmark_that_starts_late():
    late = flat(n=5, start="2020-06-01")
    with pytest.raises(ValueError, match="starts"):
        buy_and_hold_curve(late, pd.bdate_range("2020-01-01", periods=200), 10_000)


def test_compare_identical_curves_has_beta_one_zero_alpha_and_underperforms_on_ties():
    idx = pd.bdate_range("2020-01-01", periods=250)
    curve = pd.Series(100_000 * np.cumprod(1 + np.random.default_rng(1).normal(0.0004, 0.01, 250)), index=idx)
    c = compare(curve, curve, 100_000)
    assert c["beta"] == pytest.approx(1.0) and c["alpha_annual_pct"] == pytest.approx(0.0, abs=1e-6)
    assert c["excess_cagr_pct"] == pytest.approx(0.0) and c["verdict"] == "underperforms"


def test_compare_verdicts():
    idx = pd.bdate_range("2020-01-01", periods=250)
    rng = np.random.default_rng(2)
    bench = pd.Series(100_000 * np.cumprod(1 + rng.normal(0.0003, 0.012, 250)), index=idx)
    better = pd.Series(100_000 * np.cumprod(1 + rng.normal(0.0012, 0.006, 250)), index=idx)
    assert compare(better, bench, 100_000)["verdict"] == "beats_benchmark"
    smooth_low = pd.Series(100_000 * np.cumprod(1 + np.full(250, 0.0001)), index=idx)
    v = compare(smooth_low, bench, 100_000)
    assert v["verdict"] in {"better_risk_adjusted_only", "beats_benchmark"}


def test_equal_weight_curve_respects_universe():
    a = make_df([(100, 100, 100, 100)] * 2 + [(100, 200, 100, 200)] * 2)       # doubles
    b = make_df([(100, 100, 100, 100)] * 4)                                    # flat
    cal = a.index
    both = equal_weight_curve({"A": a, "B": b}, cal, 1000)
    only_b = equal_weight_curve({"A": a, "B": b}, cal, 1000, StaticUniverse(["B"]))
    assert both.iloc[-1] == pytest.approx(1500) and only_b.iloc[-1] == pytest.approx(1000)


def test_multi_strategy_same_symbol_opens_one_position():
    class Twin(Scripted):
        meta = StrategyMeta("twin", "test", min_bars=0)
    r = run_portfolio_backtest([Scripted(at={"A": [2]}), Twin(at={"A": [2]})], {"A": flat()}, risk=LOOSE,
                               cost_model=ZERO_COST, slippage_bps=0.0)
    assert len(r.trades) == 1 and r.skipped == {"ALREADY_IN_POSITION": 1}


# ---- API ---------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    settings = Settings("sqlite://", str(tmp_path), False, 100_000.0, "semi_auto", "paper")
    rng = np.random.default_rng(5)

    def frame(seed):
        r = np.random.default_rng(seed).normal(0.0006, 0.013, 900)
        close = 100 * np.cumprod(1 + r)
        open_ = np.concatenate([[100.0], close[:-1]])
        idx = pd.bdate_range("2019-01-01", periods=900)
        return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.005,
                             "low": np.minimum(open_, close) * 0.995, "close": close, "volume": 1e6}, index=idx)
    provider = FixedProvider({"AAA": frame(1), "BBB": frame(2), "CCC": frame(3), "NIFTYBEES": frame(4)})
    (tmp_path / "u.csv").write_text("symbol,start,end\nAAA,2019-01-01,\nBBB,2019-01-01,\nGHOST,2019-01-01,2020-06-01\n")
    app = create_app(settings, provider=provider, session_factory=make_session_factory(fresh_engine()))
    return TestClient(app)


def test_api_portfolio_run_is_stored_and_excludes_the_benchmark(client):
    r = client.post("/portfolio-backtests", json={"strategies": ["ema_cross", "donchian_breakout"], "start": "2020-06-01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "NIFTYBEES" not in client.get(f"/portfolio-backtests/{body['run_id']}").json()["symbols"]
    assert body["n_symbols"] == 3 and body["comparison"]["benchmark_symbol"] == "NIFTYBEES"
    assert body["comparison"]["vs_benchmark"]["verdict"] in {"beats_benchmark", "better_risk_adjusted_only", "underperforms"}
    assert body["survivorship"]["bias_risk"] == "high"
    listed = client.get("/portfolio-backtests").json()
    assert len(listed) == 1 and "trades" not in listed[0] and "curves" not in listed[0]
    full = client.get(f"/portfolio-backtests/{body['run_id']}").json()
    assert len(full["curves"]["dates"]) == len(full["curves"]["strategy"]) == len(full["curves"]["benchmark"])


def test_api_universe_file_limits_symbols_and_reports_missing_ghost(client):
    r = client.post("/portfolio-backtests", json={"strategies": ["ema_cross"], "universe_file": "u.csv"})
    assert r.status_code == 200, r.text
    sv = r.json()["survivorship"]
    assert sv["missing_price_data"] == ["GHOST"] and sv["bias_risk"] == "moderate"
    assert r.json()["n_symbols"] == 2


def test_api_rejects_path_tricks_and_unknown_input(client):
    assert client.post("/portfolio-backtests", json={"universe_file": "../etc/passwd"}).status_code == 400
    assert client.post("/portfolio-backtests", json={"strategies": ["nope"]}).status_code == 400
    assert client.post("/portfolio-backtests", json={"benchmark": "MISSING"}).status_code == 400


def test_api_missing_run_is_404(client):
    assert client.get("/portfolio-backtests/999").status_code == 404
