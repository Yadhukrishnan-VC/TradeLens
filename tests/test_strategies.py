import pytest

from tradelite.data.synthetic import make_bars
from tradelite.strategies import registry

NAMES = sorted(registry.discover())


def test_registry_finds_strategy_files():
    assert {"ema_cross", "donchian_breakout"} <= set(NAMES)


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        registry.get("ema_cross", not_a_param=1)


@pytest.mark.parametrize("name", NAMES)
def test_no_lookahead(name):
    """A signal at bar i must be identical whether or not future bars exist. This is the test
    that catches the classic bug that makes backtests look profitable and live trading fail."""
    df = make_bars("DEMO2", n=1500)
    strat = registry.get(name)
    full = strat.prepare(df)
    checked = 0
    for cut in (600, 900, 1300):
        part = strat.prepare(df.iloc[:cut])
        for i in range(strat.meta.min_bars, cut):
            a, b = strat.on_bar(full, i, "S"), strat.on_bar(part, i, "S")
            assert a == b, f"{name}: signal at bar {i} changed when future bars were removed"
            checked += a is not None
    assert checked > 0, f"{name} produced no signals; the causality check proved nothing"


@pytest.mark.parametrize("name", NAMES)
def test_signals_are_well_formed(name):
    strat = registry.get(name)
    df = make_bars("DEMO2")
    prep = strat.prepare(df)
    sigs = [s for i in range(strat.meta.min_bars, len(prep)) if (s := strat.on_bar(prep, i, "S"))]
    assert sigs
    assert all(s.stop_is_valid() and s.target and s.target > s.entry for s in sigs)
