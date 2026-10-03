from tradelens.domain import AccountState, Side, Signal
from tradelens.risk.engine import RiskConfig, RiskEngine, position_size
from conftest import buy_signal


def state(**kw) -> AccountState:
    base = dict(equity=100_000, cash=100_000, open_positions=0, exposure=0.0, realized_pnl_today=0.0)
    base.update(kw)
    return AccountState(**base)


def test_position_size_math():
    # equity 2000, 1% risk = Rs 20, stop distance 10 -> 2 units
    assert position_size(2000, 0.01, 500, 490, max_notional=2000) == 2
    # risk budget allows 200 units but capital only affords 4
    assert position_size(100_000, 0.01, 500, 495, max_notional=2000) == 4
    assert position_size(2000, 0.01, 500, 500, max_notional=2000) == 0  # zero risk distance


def test_small_account_wide_stop_is_rejected_with_reason():
    eng = RiskEngine(RiskConfig(max_position_pct=1.0))
    d = eng.evaluate(buy_signal(entry=500, stop=450, target=600), state(equity=2000, cash=2000))
    assert not d.approved and d.reason == "SIZE_ZERO_RISK_BUDGET"   # Rs20 budget < Rs50 stop distance


def test_cannot_afford_one_unit():
    eng = RiskEngine(RiskConfig(risk_pct=0.5, max_position_pct=1.0))
    d = eng.evaluate(buy_signal(entry=3000, stop=2990, target=3030), state(equity=2000, cash=2000))
    assert not d.approved and d.reason == "SIZE_ZERO_CAPITAL"


def test_approves_and_sizes():
    d = RiskEngine().evaluate(buy_signal(entry=100, stop=95, target=110), state())
    assert d.approved and d.qty == 200          # 1% of 100k = 1000 / 5 = 200; notional 20k <= 25% of equity


def test_gates():
    eng = RiskEngine()
    assert eng.evaluate(buy_signal(), state(kill_switch=True)).reason == "KILL_SWITCH"
    assert eng.evaluate(buy_signal(), state(open_symbols=frozenset({"X"}))).reason == "ALREADY_IN_POSITION"
    assert eng.evaluate(buy_signal(), state(realized_pnl_today=-3500)).reason == "DAILY_LOSS_LIMIT"
    assert eng.evaluate(buy_signal(), state(open_positions=5)).reason == "MAX_POSITIONS"
    assert eng.evaluate(buy_signal(target=101), state()).reason == "RR_TOO_LOW"
    bad_stop = Signal("t", "X", Side.BUY, buy_signal().ts, 100, 105, 120)
    assert eng.evaluate(bad_stop, state()).reason == "INVALID_STOP"


def test_exposure_cap_limits_size():
    eng = RiskEngine(RiskConfig(max_exposure_pct=0.30))
    d = eng.evaluate(buy_signal(entry=100, stop=99, target=102), state(exposure=29_000))
    assert d.approved and d.qty == 10           # only Rs 1000 of exposure headroom left
