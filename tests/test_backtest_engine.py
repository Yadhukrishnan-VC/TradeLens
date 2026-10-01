import pandas as pd
import pytest

from tradelite.backtest.costs import CostModel
from tradelite.backtest.engine import run_backtest
from tradelite.domain import Side, Signal
from tradelite.exits import check_exit
from tradelite.risk.engine import RiskConfig, RiskEngine
from tradelite.strategies.base import Strategy, StrategyMeta
from conftest import make_df

ZERO_COST = CostModel(0, 0, 0, 0, 0, 0, 0, 0)


class OneShot(Strategy):
    """Signals once, at the close of bar `at`, with fixed absolute stop/target."""
    meta = StrategyMeta("oneshot", "test", min_bars=0)
    default_params = {"at": 1, "stop": 95.0, "target": 110.0}

    def prepare(self, df):
        return df.copy()

    def on_bar(self, df, i, symbol):
        if i != self.params["at"]:
            return None
        return Signal("oneshot", symbol, Side.BUY, df.index[i].to_pydatetime(), float(df["close"].iloc[i]),
                      self.params["stop"], self.params["target"])


def bt(rows, **kw):
    return run_backtest(OneShot(**kw.pop("params", {})), make_df(rows), "X", capital=100_000,
                        cost_model=kw.pop("cost_model", ZERO_COST), slippage_bps=0.0,
                        risk=RiskEngine(RiskConfig(max_position_pct=1.0)))


def test_fills_at_next_open_not_signal_close():
    # signal at bar1 close=100; bar2 opens at 102 -> entry must be 102 (no look-ahead to 100)
    r = bt([(100, 101, 99, 100), (100, 101, 99, 100), (102, 103, 101, 102), (102, 103, 101, 102)],
           params={"target": 130.0})
    assert r.trades[0].entry_price == 102


def test_gap_up_that_ruins_reward_to_risk_skips_the_trade():
    # same setup but target 110: after the 102 open, R:R is 8/7 < 1.5, so the shared risk engine refuses
    r = bt([(100, 101, 99, 100), (100, 101, 99, 100), (102, 103, 101, 102), (102, 103, 101, 102)])
    assert not r.trades and r.skipped == {"RR_TOO_LOW": 1}


def test_target_hit():
    r = bt([(100, 101, 99, 100), (100, 101, 99, 100), (100, 112, 99, 111)])
    t = r.trades[0]
    assert t.reason == "target" and t.exit_price == 110 and t.gross_pnl > 0


def test_stop_wins_when_both_levels_inside_bar():
    r = bt([(100, 101, 99, 100), (100, 101, 99, 100), (100, 115, 90, 100)])
    assert r.trades[0].reason == "stop" and r.trades[0].exit_price == 95


def test_gap_through_stop_fills_at_open_not_stop():
    r = bt([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (90, 91, 88, 89)])
    t = r.trades[0]
    assert t.reason == "stop_gap" and t.exit_price == 90 and t.net_pnl < 0


def test_costs_reduce_pnl():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 112, 99, 111)]
    free = bt(rows).trades[0].net_pnl
    paid = bt(rows, cost_model=CostModel()).trades[0]
    assert paid.costs > 0 and paid.net_pnl < free


def test_position_closed_at_end_of_data():
    r = bt([(100, 101, 99, 100), (100, 101, 99, 100), (100, 102, 98, 101), (101, 102, 99, 100)])
    assert r.trades[-1].reason == "end_of_data"


def test_risk_reject_is_recorded_not_silent():
    # stop 40 away on Rs2000 account with 1% risk budget -> cannot size -> must appear in `skipped`
    r = run_backtest(OneShot(stop=60.0, target=200.0),
                     make_df([(100, 101, 99, 100)] * 5), "X", capital=2000, cost_model=ZERO_COST,
                     slippage_bps=0.0, risk=RiskEngine(RiskConfig(max_position_pct=1.0)))
    assert not r.trades and r.skipped.get("SIZE_ZERO_RISK_BUDGET") == 1


@pytest.mark.parametrize("side,o,h,l,expect", [
    (Side.BUY, 100, 101, 99, (None, None)),
    (Side.SELL, 100, 101, 99, (None, None)),
    (Side.SELL, 100, 106, 99, (105, "stop")),
    (Side.SELL, 100, 101, 89, (90, "target")),
])
def test_check_exit_short_and_long(side, o, h, l, expect):
    stop, target = (95, 110) if side is Side.BUY else (105, 90)
    assert check_exit(side, stop, target, o, h, l) == expect
