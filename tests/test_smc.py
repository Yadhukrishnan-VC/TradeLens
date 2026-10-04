"""The SMC rules, pinned on hand-built candles whose answer is known by construction (see tradelens/smc.py for the definitions)."""
import numpy as np
import pandas as pd
import pytest
from conftest import FixedProvider, make_df

from tradelens.backtest.portfolio import run_portfolio_backtest
from tradelens.data.synthetic import make_bars
from tradelens.smc import analyze, first_touch
from tradelens.strategies import registry

# (open, high, low, close). Swing high 110 at bar 4 (known at 6), swing low 96 at bar 8 (known at 10),
# close 112 above 110 at bar 12 = BOS up. Order block = bar 8 (lowest low since the swing high). FVG at bar 12: gap [102, 104].
BASE = [(100, 101, 99, 100), (100, 102, 100, 101), (101, 104, 101, 103), (103, 107, 103, 106), (106, 110, 105, 108),
        (108, 109, 104, 105), (105, 106, 101, 102), (102, 103, 99, 100), (100, 101, 96, 97), (97, 100, 97, 99),
        (99, 102, 98, 101), (101, 106, 100, 105), (105, 113, 104, 112), (112, 114, 108, 110), (110, 111, 103, 104)]
P = dict(swing_left=2, swing_right=2)


def scenario(*tail, base=BASE):
    return make_df(list(base) + list(tail))


def signals(name, df, **params):
    st = registry.get(name, **{**P, **params})
    pr = st.prepare(df)
    return {i: st.on_bar(pr, i, "S") for i in range(len(pr)) if st.on_bar(pr, i, "S") is not None}, pr


# ------------------------------------------------------------------ swings and structure

def test_a_swing_is_only_known_right_bars_after_it_formed():
    df = scenario((100.5, 104, 100, 103))
    fr = analyze(df, left=2, right=2)
    assert fr.swing_highs[0] == (4, 110.0, 6) and fr.swing_lows == [(8, 96.0, 10)]
    assert analyze(df.iloc[:6], left=2, right=2).swing_highs == []                       # bars 0..5: not yet known
    assert analyze(df.iloc[:7], left=2, right=2).swing_highs[0] == (4, 110.0, 6)         # bar 6 confirms it


def test_bos_up_fires_on_the_close_that_breaks_the_swing_high_not_before():
    fr = analyze(scenario(), left=2, right=2)
    assert list(np.nonzero(fr.bos_up)[0]) == [12] and not fr.choch_up.any()
    assert list(fr.trend[:12]) == [0] * 12 and set(fr.trend[12:]) == {1}
    assert make_df(BASE[:12]).pipe(analyze, left=2, right=2).bos_up.sum() == 0           # bar 11 closed at 105 < 110


def test_a_wick_through_the_swing_high_is_not_a_break():
    rows = list(BASE)
    rows[12] = (105, 113, 104, 109)                                                      # high above 110, close below
    assert not analyze(make_df(rows), left=2, right=2).bos_up.any()


def test_choch_down_then_choch_up_after_a_bos():
    tail = [(100.5, 104, 100, 103), (103, 106, 102, 105), (104, 104, 97, 98), (98, 99, 93, 94),      # 18 closes under 96
            (94, 100, 93, 99), (99, 106, 98, 105), (105, 112, 104, 111), (111, 118, 110, 116)]       # 22 closes over 114
    fr = analyze(scenario(*tail), left=2, right=2)
    assert list(np.nonzero(fr.bos_up)[0]) == [12]
    assert list(np.nonzero(fr.choch_down)[0]) == [18] and not fr.bos_down.any()          # trend was +1, so a down-break is a CHoCH
    assert list(np.nonzero(fr.choch_up)[0]) == [22]                                      # trend was -1, so an up-break is a CHoCH
    assert [int(x) for x in fr.trend[17:23]] == [1, -1, -1, -1, -1, 1]
    assert [z.label for z in fr.obs] == ["bos", "choch"]


# ------------------------------------------------------------------ zones

def test_order_block_is_the_origin_candle_of_the_leg_that_broke_structure():
    fr = analyze(scenario(), left=2, right=2)
    ob = fr.obs[0]
    assert (ob.origin, ob.lo, ob.hi, ob.born, ob.created, ob.label) == (8, 96.0, 101.0, 13, 12, "bos")
    assert ob.size_atr == pytest.approx((113 - 96) / fr.atr[12])
    body = analyze(scenario(), left=2, right=2, ob_body=True).obs[0]
    assert (body.lo, body.hi) == (97.0, 100.0)


def test_fvg_is_the_gap_between_candle_j_minus_2_high_and_candle_j_low():
    fr = analyze(scenario(), left=2, right=2)
    z = [f for f in fr.fvgs if f.created == 12][0]
    assert (z.lo, z.hi, z.origin, z.born, z.label, z.trend_at_birth) == (102.0, 104.0, 10, 13, "bull", 1)
    assert z.size_atr == pytest.approx(2 / fr.atr[12]) and z.disp_atr == pytest.approx(6 / fr.atr[12])
    early = [f for f in fr.fvgs if f.created in (3, 4)]
    assert early and all(f.trend_at_birth == 0 for f in early)                           # formed before any structure break


def test_overlapping_candles_make_no_gap():
    df = make_df([(100, 101, 99, 100), (100, 103, 99, 102), (102, 104, 100, 103)])        # low[2]=100 < high[0]=101
    assert analyze(df, left=1, right=1).fvgs == []


def test_first_touch_is_the_first_bar_whose_low_enters_the_zone():
    fr = analyze(scenario((100.5, 104, 100, 103)), left=2, right=2)
    low = scenario((100.5, 104, 100, 103))["low"].to_numpy()
    assert first_touch(low, fr.obs[0], max_age=30) == 15 and first_touch(low, fr.obs[0], max_age=1) is None


# ------------------------------------------------------------------ order block strategy

def test_ob_touch_entry_signals_on_the_touch_bar_with_the_stop_below_the_zone():
    df = scenario((100.5, 104, 100, 103))
    sigs, pr = signals("smc_order_block", df, entry_mode="touch")
    assert list(sigs) == [15]
    s, atr = sigs[15], pr["atr"].iloc[15]
    assert s.entry == 103 and s.stop == pytest.approx(96 - 0.1 * atr) and s.target == pytest.approx(103 + 2 * (103 - s.stop))
    assert pr["smc_zlo"].iloc[15] == 96 and pr["smc_zhi"].iloc[15] == 101


def test_ob_confirm_entry_waits_for_the_close_back_above_the_zone():
    df = scenario((100.5, 102, 98, 100), (100, 105, 99.5, 104))                          # 15 dips in and closes inside, 16 closes above 101
    sigs, pr = signals("smc_order_block", df, entry_mode="confirm")
    assert list(sigs) == [16]
    assert sigs[16].stop == pytest.approx(min(96, 98) - 0.1 * pr["atr"].iloc[16])        # below the lower of zone bottom / lowest low since touch
    assert signals("smc_order_block", df, entry_mode="confirm", confirm_bars=0)[0] == {}  # no time allowed: nothing
    assert signals("smc_order_block", df, entry_mode="touch")[0] == {}                    # touch bar was red: needs rejection
    assert list(signals("smc_order_block", df, entry_mode="touch", require_rejection=False)[0]) == [15]


def test_a_close_through_the_zone_spends_it_for_good():
    df = scenario((100, 101, 94, 95), (95, 104, 95, 103))                                # 15 closes under 96, 16 rallies back
    for mode in ("confirm", "touch"):    # require_bias=False so that ONLY the spent-zone rule can be what blocks the trade
        assert signals("smc_order_block", df, entry_mode=mode, require_rejection=False, require_bias=False)[0] == {}


def test_the_stop_goes_under_a_wick_that_pierced_the_zone_and_closed_back_above_it():
    df = scenario((100.5, 102, 94.5, 100), (100, 105, 99.5, 104))                         # 15 wicks to 94.5 (< zone bottom 96), closes inside
    sigs, pr = signals("smc_order_block", df, entry_mode="confirm")
    assert list(sigs) == [16] and sigs[16].stop == pytest.approx(94.5 - 0.1 * pr["atr"].iloc[16])


def test_a_zone_is_traded_once():
    df = scenario((100.5, 104, 100, 103), (103, 106, 102, 105), (105, 106, 100, 104))    # 17 touches the zone again
    assert list(signals("smc_order_block", df, entry_mode="touch")[0]) == [15]


def test_ob_is_skipped_when_structure_turned_bearish_before_the_retest():
    # bar 14 left a higher swing low at 103 (known at bar 16); bar 17 closes at 97 under it = CHoCH down before the zone is retested
    df = scenario((110, 111, 107, 108), (108, 109, 105, 106), (106, 107, 96, 97), (97, 103, 96.5, 102))
    fr = analyze(df, left=2, right=2)
    assert list(np.nonzero(fr.choch_down)[0]) == [17] and int(fr.trend[18]) == -1
    assert signals("smc_order_block", df, entry_mode="confirm")[0] == {}                           # default: bias required
    sigs, _ = signals("smc_order_block", df, entry_mode="confirm", require_bias=False)
    assert list(sigs) == [18]                                                                       # touch at 17, closes back above 101 at 18


def test_ob_mode_filter_and_validation():
    df = scenario((100.5, 104, 100, 103))
    assert signals("smc_order_block", df, entry_mode="touch", mode="choch")[0] == {}      # this OB came from a BOS
    assert list(signals("smc_order_block", df, entry_mode="touch", mode="any")[0]) == [15]
    with pytest.raises(ValueError, match="mode"):
        registry.get("smc_order_block", mode="nope").prepare(df)
    with pytest.raises(ValueError, match="entry_mode"):
        registry.get("smc_order_block", entry_mode="nope", **P).prepare(df)


def test_min_leg_filters_weak_impulses():
    df = scenario((100.5, 104, 100, 103))
    assert signals("smc_order_block", df, entry_mode="touch", min_leg_atr=10.0)[0] == {}


def test_require_fvg_needs_a_gap_left_by_the_impulse():
    df = scenario((100.5, 104, 100, 103))
    assert list(signals("smc_order_block", df, entry_mode="touch", require_fvg=True)[0]) == [15]
    rows = list(BASE)
    rows[12] = (105, 113, 101.5, 112)                                                    # same break, but no gap this time
    nogap = scenario((100.5, 104, 100, 103), base=rows)
    assert not [f for f in analyze(nogap, left=2, right=2).fvgs if f.created == 12]
    assert signals("smc_order_block", nogap, entry_mode="touch", require_fvg=True)[0] == {}
    assert list(signals("smc_order_block", nogap, entry_mode="touch")[0]) == [15]


def test_min_risk_widens_a_stop_that_is_too_tight():
    df = scenario((100.5, 104, 100, 103))
    s, pr = signals("smc_order_block", df, entry_mode="touch", min_risk_atr=3.0)
    assert s[15].stop == pytest.approx(103 - 3.0 * pr["atr"].iloc[15])


# ------------------------------------------------------------------ FVG strategy

def test_fvg_in_bullish_structure_is_traded_but_a_pre_structure_gap_is_not():
    df = scenario()
    with_bias, _ = signals("smc_fvg", df, entry_mode="touch", require_rejection=False)
    assert list(with_bias) == [14]                                                       # the 102-104 gap, first touched on bar 14
    s = with_bias[14]
    assert s.entry == 104 and s.stop < 102
    no_bias, _ = signals("smc_fvg", df, entry_mode="touch", require_rejection=False, require_bias=False)
    assert min(no_bias) < 12 and 14 in no_bias                                           # the two early gaps (no structure yet) show up


def test_fvg_size_and_displacement_filters():
    df = scenario()
    assert signals("smc_fvg", df, entry_mode="touch", require_rejection=False, min_gap_atr=5.0)[0] == {}
    assert signals("smc_fvg", df, entry_mode="touch", require_rejection=False, min_disp_atr=9.0)[0] == {}


# ------------------------------------------------------------------ sweep strategy

def test_swing_low_sweep_pierces_closes_back_above_with_a_wick():
    df = scenario((100.5, 104, 100, 103), (101, 102, 94, 100))                           # 16 stabs 94 under the 96 low, closes 100
    sigs, pr = signals("smc_sweep", df, pool="swing")
    assert list(sigs) == [16]
    assert sigs[16].entry == 100 and sigs[16].stop == pytest.approx(94 - 0.1 * pr["atr"].iloc[16])


@pytest.mark.parametrize("last, why", [((101, 102, 94, 95), "closes under the level"),
                                       ((101, 102, 96.5, 100), "never pierces the level"),
                                       ((101, 102, 94, 96.5), "pierces and reclaims but the lower wick is tiny vs the range")])
def test_not_a_sweep(last, why):
    df = scenario((100.5, 104, 100, 103), last)
    assert signals("smc_sweep", df, pool="swing", wick_min=0.5)[0] == {}, why


def test_a_level_that_was_already_traded_through_is_not_liquidity_any_more():
    once = scenario((100.5, 104, 100, 103), (101, 102, 94, 100))
    assert list(signals("smc_sweep", once, pool="swing")[0]) == [16]
    twice = scenario((100.5, 104, 100, 103), (103, 103, 95, 101), (101, 102, 94, 100))              # bar 16 already pierced 96
    sigs = signals("smc_sweep", twice, pool="swing", wick_min=0.0)[0]
    assert 17 not in sigs


def test_range_low_sweep_and_the_bias_option():
    flat = [(100, 101, 99, 100)] * 25
    df = make_df(flat + [(100, 100.5, 97, 100)])                                          # pierces the 20-bar low (99), closes back at 100
    sigs, _ = signals("smc_sweep", df, pool="range")
    assert list(sigs) == [25] and sigs[25].entry == 100
    assert signals("smc_sweep", df, pool="range", bias="bullish")[0] == {}                # structure is undefined (0), not bullish
    assert signals("smc_sweep", df, pool="swing")[0] == {}                                # no swing lows exist in a flat series
    with pytest.raises(ValueError, match="pool"):
        registry.get("smc_sweep", pool="x").prepare(df)


# ------------------------------------------------------------------ causality of the engine itself, and plumbing

def test_engine_output_never_changes_when_future_bars_are_removed():
    df = make_bars("DEMO3", n=1200)
    full = analyze(df)
    for cut in (250, 600, 900):
        part = analyze(df.iloc[:cut])
        for name in ("trend", "eq", "atr", "bos_up", "choch_up", "bos_down", "choch_down"):
            np.testing.assert_array_equal(getattr(part, name), getattr(full, name)[:cut], err_msg=name)
        assert part.obs == [z for z in full.obs if z.created < cut]
        assert part.fvgs == [z for z in full.fvgs if z.created < cut]
        assert part.swing_lows == [s for s in full.swing_lows if s[2] < cut]
        assert part.swing_highs == [s for s in full.swing_highs if s[2] < cut]


def test_smc_strategies_are_registered_and_run_in_the_portfolio_backtest():
    names = {"smc_fvg", "smc_order_block", "smc_sweep"}
    assert names <= set(registry.discover())
    frames = {f"S{i}": make_bars(f"DEMO{i}", n=900) for i in (1, 2, 3)}
    res = run_portfolio_backtest([registry.get(n) for n in sorted(names)], frames, capital=100_000)
    assert res.trades, "the SMC strategies should trade on 3 x 900 bars"
    assert {t.strategy for t in res.trades} <= names
    for t in res.trades:
        assert t.exit_price > 0 and t.qty > 0


def test_two_zones_touched_on_the_same_bar_give_one_signal_from_the_older_zone():
    from tradelens.smc import Zone, zone_retest_signals
    df = scenario((100.5, 104, 100, 103))
    fr = analyze(df, left=2, right=2)
    a = Zone("ob", 13, 96.0, 101.0, 8, "bos", 4.0, 0.0, 1, 12)
    b = Zone("ob", 14, 98.0, 101.0, 9, "bos", 4.0, 0.0, 1, 13)
    res = zone_retest_signals(df, fr, [b, a], entry_mode="touch")
    assert int(res["sig"].sum()) == 1 and res["sig"][15]
    assert res["zlo"][15] == 96.0                                   # the older zone (a) won, b did not overwrite it
