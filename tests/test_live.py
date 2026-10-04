import inspect
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from conftest import FixedProvider, buy_signal, fresh_engine, make_df
from sqlalchemy import text

from tradelens.broker.base import Broker, BrokerError, BrokerUncertain, Fill
from tradelens.broker.paper import PaperBroker
from tradelens.broker.zerodha import (ZerodhaBroker, complete_login, extract_request_token, session_kite_factory,
                                      validate_live_settings)
from tradelens.config import Settings
from tradelens.db import add_missing_columns, make_engine, make_session_factory
from tradelens.domain import Mode, Side
from tradelens.models import EquitySnapshotRow, FilterLogRow, OrderRow, PositionRow, SignalRow
from tradelens.services import jobs, paper_review
from tradelens.services.alerts import RecordingNotifier
from tradelens.services.filters import EventBlackoutFilter, load_events
from tradelens.services.pipeline import Pipeline

SIG_TS = datetime(2020, 1, 8)          # last bar of the 6-bar frame below


def frame(n=6, last_open=100.0, low=99.0):
    rows = [(100, 101, 99, 100)] * (n - 1) + [(last_open, last_open + 1, low, 100)]
    return make_df(rows)


def proposed_order(session, symbol="X", qty=200, entry=100.0, stop=95.0, target=110.0, ts=SIG_TS) -> OrderRow:
    sig = SignalRow(strategy="test", symbol=symbol, side="BUY", ts=ts, entry=entry, stop=stop, target=target,
                    status="proposed", suggested_qty=qty, created_at=datetime(2020, 1, 8, 16, 30))
    session.add(sig)
    session.flush()
    o = OrderRow(signal_id=sig.id, symbol=symbol, side="BUY", qty=qty, status="PENDING_APPROVAL", mode="semi_auto",
                 tag=f"t{sig.id}", created_at=datetime(2020, 1, 8, 16, 30))
    session.add(o)
    session.flush()
    return o


def settings(**kw) -> Settings:
    base = dict(database_url="sqlite://", data_dir="data", demo=False, starting_capital=100_000.0, mode="semi_auto", broker="paper")
    base.update(kw)
    return Settings(**base)


# ------------------------------------------------------------------ paper broker, next-open model

def test_next_open_order_waits_then_fills_at_the_next_open_with_slippage():
    frames = {"X": frame()}
    b = PaperBroker(fill_model="next_open", bars=lambda s: frames[s])
    f = b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="a", signal_ts=SIG_TS)
    assert f.status == "OPEN" and b.get_order(f.order_id).status == "OPEN"        # no next bar yet
    frames["X"] = make_df([(100, 101, 99, 100)] * 5 + [(100, 101, 99, 100), (103, 104, 102, 103)])
    done = b.get_order(f.order_id)
    assert done.status == "FILLED" and done.price == pytest.approx(103 * 1.0005)
    assert b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="a").order_id == f.order_id   # idempotent


def test_a_different_paper_broker_instance_can_complete_the_order():
    """API process places the order, the worker process (another instance, no shared memory) collects the fill."""
    frames = {"X": frame()}
    api = PaperBroker(fill_model="next_open", bars=lambda s: frames[s])
    f = api.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="a", signal_ts=SIG_TS)
    frames["X"] = make_df([(100, 101, 99, 100)] * 6 + [(103, 104, 102, 103)])
    worker = PaperBroker(fill_model="next_open", bars=lambda s: frames[s])
    done = worker.get_order(f.order_id)
    assert done.status == "FILLED" and done.price == pytest.approx(103 * 1.0005) and done.qty == 10
    with pytest.raises(KeyError):
        worker.get_order("not-a-paper-id")


def test_paper_exits_fill_at_once_even_in_next_open_mode():
    b = PaperBroker(fill_model="next_open", bars=lambda s: frame())
    f = b.place_order(symbol="X", side=Side.SELL, qty=10, price=95.0, tag="x1")          # exit: no signal_ts
    assert f.status == "FILLED" and f.price == pytest.approx(95.0 * (1 - 0.0005))


def test_open_paper_order_can_be_cancelled_and_never_fills():
    frames = {"X": frame()}
    b = PaperBroker(fill_model="next_open", bars=lambda s: frames[s])
    f = b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="a", signal_ts=SIG_TS)
    assert b.cancel_order(f.order_id) is True and b.get_order(f.order_id).status == "CANCELLED"
    assert b.cancel_order(f.order_id) is False


def test_next_open_requires_a_bars_function():
    with pytest.raises(ValueError):
        PaperBroker(fill_model="next_open")


def test_pipeline_submits_then_syncs_the_fill_into_a_position(session):
    frames = {"X": frame()}
    prov = FixedProvider(frames)
    p = Pipeline(session, prov, PaperBroker(fill_model="next_open", bars=prov.get_bars), notifier=RecordingNotifier())
    o = proposed_order(session)
    p.approve(o.id)
    assert o.status == "SUBMITTED" and session.query(PositionRow).count() == 0
    assert session.get(SignalRow, o.signal_id).status == "submitted"
    assert p.sync_orders() == []                                                   # still waiting
    frames["X"] = make_df([(100, 101, 99, 100)] * 6 + [(102, 103, 101, 102)])
    assert [x.id for x in p.sync_orders()] == [o.id]
    pos = session.query(PositionRow).one()
    assert o.status == "FILLED" and pos.entry_price == pytest.approx(102 * 1.0005) and pos.signal_entry == 100.0
    assert pos.qty == o.qty and session.get(SignalRow, o.signal_id).status == "executed"


# ------------------------------------------------------------------ scripted broker for lifecycle + stops

class FakeBroker(Broker):
    supports_exchange_stops = True

    def __init__(self, entry="FILLED"):
        self.entry, self.calls, self.orders = entry, [], {}
        self.stop_id, self.stop_error, self.stop_state, self.cancel_ok = "G1", None, Fill("G1", "X", Side.SELL, 0, 0, "OPEN"), True
        self.stop_calls, self.held = [], None
        self.exit_status = "FILLED"

    def place_order(self, *, symbol, side, qty, price, tag, signal_ts=None):
        self.calls.append((side.value, qty, tag))
        if self.entry == "UNCERTAIN" and side is Side.BUY:
            raise BrokerUncertain("timeout")
        status = self.entry if side is Side.BUY else self.exit_status
        f = Fill(f"o{len(self.calls)}", symbol, side, qty, price, status, reason="because" if status == "REJECTED" else "")
        self.orders[f.order_id] = f
        return f

    def get_order(self, order_id):
        return self.orders[order_id]

    def place_protective_stop(self, *, symbol, qty, trigger, tag):
        self.stop_calls.append((symbol, qty, trigger, tag))
        if self.stop_error:
            raise BrokerError(self.stop_error)
        return self.stop_id

    def stop_status(self, stop_id):
        return self.stop_state

    def cancel_protective_stop(self, stop_id):
        return self.cancel_ok

    def held_quantities(self):
        return self.held


def live_pipeline(session, broker, frames=None):
    rec = RecordingNotifier()
    return Pipeline(session, FixedProvider(frames or {"X": frame()}), broker, notifier=rec), rec


def test_entry_fill_places_an_exchange_stop(session):
    b = FakeBroker()
    p, rec = live_pipeline(session, b)
    p.approve(proposed_order(session).id)
    pos = session.query(PositionRow).one()
    assert pos.broker_stop_id == "G1" and b.stop_calls == [("X", 200, 95.0, f"s{pos.id}")] and rec.sent == []


@pytest.mark.parametrize("how", ["none", "error"])
def test_missing_exchange_stop_is_a_critical_alert(session, how):
    b = FakeBroker()
    if how == "none":
        b.stop_id = None
    else:
        b.stop_error = "stop is not below the last price"
    p, rec = live_pipeline(session, b)
    p.approve(proposed_order(session).id)
    assert session.query(PositionRow).one().broker_stop_id is None
    assert rec.sent[0].level == "critical" and "NO exchange-side stop" in rec.sent[0].title


def test_uncertain_entry_is_flagged_and_never_retried(session):
    b = FakeBroker(entry="UNCERTAIN")
    p, rec = live_pipeline(session, b)
    o = proposed_order(session)
    p.approve(o.id)
    assert o.status == "UNCERTAIN" and session.query(PositionRow).count() == 0
    assert rec.sent[0].level == "critical" and "UNKNOWN" in rec.sent[0].title
    with pytest.raises(ValueError):
        p.approve(o.id)                                   # not pending any more: cannot be re-sent by clicking again


def test_exchange_stop_fill_closes_the_position_without_a_second_sell(session):
    b = FakeBroker()
    p, _ = live_pipeline(session, b)
    p.approve(proposed_order(session).id)
    b.stop_state = Fill("G1", "X", Side.SELL, 200, 94.0, "FILLED")
    closed = p.check_exits()
    assert [c.exit_price for c in closed] == [94.0] and closed[0].exit_reason == "stop"
    assert [c[0] for c in b.calls] == ["BUY"]             # no SELL was sent: the exchange already sold


def test_software_stop_cancels_the_exchange_stop_first_and_aborts_if_it_cannot(session):
    breach = make_df([(100, 101, 99, 100)] * 5 + [(100, 101, 99, 100), (96, 97, 90, 92)])     # day after signal breaches 95
    b = FakeBroker()
    p, rec = live_pipeline(session, b, {"X": make_df([(100, 101, 99, 100)] * 6)})
    p.approve(proposed_order(session).id)
    p.provider.frames["X"] = breach
    b.cancel_ok = False
    assert p.check_exits() == [] and [c[0] for c in b.calls] == ["BUY"]
    assert any("cannot cancel the exchange stop" in a.title for a in rec.sent)
    b.cancel_ok = True
    closed = p.check_exits()
    assert len(closed) == 1 and closed[0].exit_reason == "stop" and [c[0] for c in b.calls] == ["BUY", "SELL"]
    assert closed[0].broker_stop_id is None


def test_a_vanished_exchange_stop_is_reported_and_replaced(session):
    b = FakeBroker()
    p, rec = live_pipeline(session, b)
    p.approve(proposed_order(session).id)
    b.stop_state = Fill("G1", "X", Side.SELL, 200, 0, "REJECTED", reason="GTT triggered but its order failed")
    b.stop_id = "G2"
    p.check_exits()
    assert any("exchange-side stop is gone" in a.title for a in rec.sent) and len(b.stop_calls) == 2
    assert session.query(PositionRow).one().broker_stop_id == "G2"


def test_pending_exit_order_is_tracked_until_it_fills_or_fails(session):
    b = FakeBroker()
    frames = {"X": make_df([(100, 101, 99, 100)] * 6)}
    p, rec = live_pipeline(session, b, frames)
    p.approve(proposed_order(session).id)
    p.provider.frames["X"] = make_df([(100, 101, 99, 100)] * 6 + [(96, 97, 90, 92)])
    b.exit_status, b.stop_state = "OPEN", Fill("G1", "X", Side.SELL, 200, 0, "OPEN")
    assert p.check_exits() == []
    pos = session.query(PositionRow).one()
    assert pos.exit_order_id and pos.closed_at is None and pos.exit_pending_reason == "stop"
    p.check_exits()
    assert len([c for c in b.calls if c[0] == "SELL"]) == 1                      # waiting, not re-sending
    b.orders[pos.exit_order_id] = Fill(pos.exit_order_id, "X", Side.SELL, 200, 94.5, "REJECTED", reason="price band")
    p.sync_orders()
    assert pos.exit_order_id is None and any("EXIT ORDER FAILED" in a.title for a in rec.sent)
    b.exit_status = "FILLED"
    closed = p.check_exits()
    assert closed and closed[0].exit_reason == "stop"
    assert [c[2] for c in b.calls if c[0] == "SELL"] == [f"x{pos.id}", f"x{pos.id}r1"]     # a retry gets a fresh tag


def test_reconcile_only_alarms_when_the_broker_holds_less(session):
    b = FakeBroker()
    p, rec = live_pipeline(session, b)
    p.approve(proposed_order(session).id)
    assert p.reconcile() == []                                  # broker cannot report: nothing to say
    b.held = {"X": 200, "TCS": 50}                              # extra holdings are the user's own investments
    assert p.reconcile() == []
    b.held = {"X": 120}
    assert p.reconcile() == ["X: tradelens thinks it holds 200, the broker shows 120"]
    assert rec.sent[-1].level == "critical"
    b.held = {}
    assert len(p.reconcile()) == 1


# ------------------------------------------------------------------ Zerodha adapter against a fake Kite

class NetworkException(Exception): ...
class TokenException(Exception): ...
class InputException(Exception): ...


class FakeKite:
    def __init__(self):
        self.book, self.placed, self.gtt_calls, self.gtts, self.cancelled, self.deleted = [], [], [], {}, [], []
        self.fail, self.ltp_price, self.holdings_, self.positions_ = None, 100.0, [], {"net": []}
        self.book_fails = False

    def orders(self):
        if self.book_fails:
            raise NetworkException("down")
        return list(self.book)

    def place_order(self, **kw):
        if self.fail:
            raise self.fail
        self.placed.append(kw)
        oid = str(len(self.placed))
        self.book.append({"order_id": oid, "tag": kw["tag"], "status": "OPEN", "quantity": kw["quantity"], "price": kw["price"],
                          "transaction_type": kw["transaction_type"], "variety": kw["variety"], "tradingsymbol": kw["tradingsymbol"]})
        return oid

    def order_history(self, order_id):
        return [o for o in self.book if o["order_id"] == order_id]

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self.cancelled.append((variety, order_id))
        for o in self.book:
            if o["order_id"] == order_id:
                o["status"] = "CANCELLED"

    def ltp(self, instruments):
        return {k: {"last_price": self.ltp_price} for k in instruments}

    def place_gtt(self, **kw):
        self.gtt_calls.append(kw)
        tid = len(self.gtts) + 1
        self.gtts[tid] = {"status": "active", "condition": {"tradingsymbol": kw["tradingsymbol"]}, "orders": [dict(kw["orders"][0])]}
        return {"trigger_id": tid}

    def get_gtt(self, tid):
        return self.gtts[tid]

    def delete_gtt(self, tid):
        self.gtts[tid]["status"] = "deleted"
        self.deleted.append(tid)

    def holdings(self):
        return self.holdings_

    def positions(self):
        return self.positions_


WED_10AM, WED_4PM, SAT_10AM = datetime(2026, 9, 30, 10, 0), datetime(2026, 9, 30, 16, 45), datetime(2026, 10, 3, 10, 0)


def zb(kite=None, now=WED_10AM, **kw):
    kite = kite or FakeKite()
    kw.setdefault("dry_run", False)
    return ZerodhaBroker(lambda: kite, clock=lambda: now, **kw), kite


def test_buy_is_a_marketable_limit_on_the_tick_with_a_clean_tag():
    b, k = zb()
    f = b.place_order(symbol="INFY", side=Side.BUY, qty=10, price=123.47, tag="ab-12_cd!ef")
    q = k.placed[0]
    assert (q["order_type"], q["price"], q["product"], q["validity"], q["variety"]) == ("LIMIT", 123.85, "CNC", "DAY", "regular")
    assert q["tag"] == "ab12cdef" and "market_protection" not in q and f.status == "OPEN" and f.order_id == "1"


def test_sell_limit_rounds_down_and_after_hours_orders_are_amo():
    b, k = zb(now=WED_4PM)
    b.place_order(symbol="INFY", side=Side.SELL, qty=10, price=123.47, tag="s1")
    assert k.placed[0]["price"] == 123.05 and k.placed[0]["variety"] == "amo"
    b2, k2 = zb(now=SAT_10AM)
    b2.place_order(symbol="INFY", side=Side.BUY, qty=1, price=100, tag="s2")
    assert k2.placed[0]["variety"] == "amo"


def test_dry_run_sends_nothing_and_says_what_it_would_have_done():
    b, k = zb(dry_run=True)
    f = b.place_order(symbol="INFY", side=Side.BUY, qty=10, price=100.0, tag="t1")
    assert k.placed == [] and f.status == "REJECTED" and f.reason.startswith("DRY_RUN: would place regular LIMIT BUY 10 INFY @ 100.3")
    assert b.place_protective_stop(symbol="INFY", qty=10, trigger=95, tag="s1") is None and k.gtt_calls == []


def test_value_caps_apply_to_buys_only_and_the_daily_cap_comes_from_the_brokers_book():
    b, k = zb(max_order_value=5_000, max_daily_value=8_000)
    with pytest.raises(BrokerError, match="per-order cap"):
        b.place_order(symbol="X", side=Side.BUY, qty=100, price=100.0, tag="a")             # 10,030
    b.place_order(symbol="X", side=Side.BUY, qty=40, price=100.0, tag="b")                  # 4,012 ok
    with pytest.raises(BrokerError, match="daily cap"):
        b.place_order(symbol="Y", side=Side.BUY, qty=45, price=100.0, tag="c")             # 4,012 + 4,513 > 8,000
    k.book[0]["status"] = "REJECTED"                                                       # a rejected order frees the budget
    b.place_order(symbol="Y", side=Side.BUY, qty=45, price=100.0, tag="c")
    b.place_order(symbol="X", side=Side.SELL, qty=1000, price=100.0, tag="exit")           # exits are never capped
    assert [p["tag"] for p in k.placed] == ["b", "c", "exit"]


def test_same_tag_never_places_twice():
    b, k = zb()
    a = b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="same")
    again = b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="same")
    assert len(k.placed) == 1 and again.order_id == a.order_id


def test_timeout_checks_the_order_book_before_declaring_anything():
    k = FakeKite()
    k.fail = NetworkException("timed out")
    b, _ = zb(k)
    with pytest.raises(BrokerUncertain, match="no order with tag"):
        b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="t1")
    # the request actually landed before the connection dropped
    real = k.place_order
    def lands_then_times_out(**kw):
        k.fail = None
        real(**kw)
        k.fail = NetworkException("reset")
        raise k.fail
    k.place_order = lands_then_times_out
    f = b.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="t2")
    assert f.status == "OPEN" and len(k.placed) == 1
    # and when even the book cannot be read
    k2 = FakeKite()
    k2.fail = NetworkException("x")
    b2, _ = zb(k2)
    k2.orders = lambda: ([] if not k2.fail_once else (_ for _ in ()).throw(NetworkException("down")))
    k2.fail_once = False
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise NetworkException("down")
        return []
    k2.orders = flaky
    with pytest.raises(BrokerUncertain, match="could not be read"):
        b2.place_order(symbol="X", side=Side.BUY, qty=10, price=100.0, tag="t3")


def test_errors_are_translated_without_leaking_sdk_types():
    k = FakeKite()
    k.fail = TokenException("Incorrect api_key or access_token")
    b, _ = zb(k)
    with pytest.raises(BrokerError, match="kite-login") as e:
        b.place_order(symbol="X", side=Side.BUY, qty=1, price=100.0, tag="a")
    assert not isinstance(e.value, BrokerUncertain)
    k.fail = InputException("IP 1.2.3.4 is not allowed to place orders for this app")
    with pytest.raises(BrokerError, match="not allowed to place orders"):
        b.place_order(symbol="X", side=Side.BUY, qty=1, price=100.0, tag="b")


def test_order_status_mapping_including_partial_fills():
    b, k = zb()
    base = {"tradingsymbol": "X", "transaction_type": "BUY", "quantity": 10, "variety": "regular"}
    cases = [({"status": "COMPLETE", "filled_quantity": 10, "average_price": 100.2}, "FILLED"),
             ({"status": "REJECTED", "status_message": "RMS: insufficient funds"}, "REJECTED"),
             ({"status": "CANCELLED", "filled_quantity": 4, "average_price": 100.1}, "CANCELLED"),
             ({"status": "CANCELLED AMO"}, "CANCELLED"), ({"status": "TRIGGER PENDING"}, "OPEN"), ({"status": "AMO REQ RECEIVED"}, "OPEN")]
    for i, (extra, want) in enumerate(cases):
        k.book.append({"order_id": f"q{i}", **base, **extra})
        assert b.get_order(f"q{i}").status == want
    f = b.get_order("q2")
    assert f.done_qty == 4 and f.price == 100.1
    assert b.get_order("q0").done_qty == 10 and "insufficient funds" in b.get_order("q1").reason
    with pytest.raises(BrokerError, match="not found"):
        b.get_order("nope")


def test_gtt_stop_is_a_single_sell_limit_below_the_trigger():
    b, k = zb()
    sid = b.place_protective_stop(symbol="INFY", qty=25, trigger=95.0, tag="s1")
    c = k.gtt_calls[0]
    assert sid == "1" and c["trigger_values"] == [95.0] and c["last_price"] == 100.0 and c["trigger_type"] == "single"
    leg = c["orders"][0]
    assert (leg["transaction_type"], leg["order_type"], leg["product"], leg["quantity"]) == ("SELL", "LIMIT", "CNC", 25)
    assert leg["price"] == 93.55 and leg["price"] < c["trigger_values"][0]               # 95 * (1 - 1.5%) = 93.575 -> floor 93.55
    k.ltp_price = 94.0
    with pytest.raises(BrokerError, match="trigger at once"):
        b.place_protective_stop(symbol="INFY", qty=25, trigger=95.0, tag="s2")


def test_stop_status_reports_what_really_happened():
    b, k = zb()
    sid = b.place_protective_stop(symbol="INFY", qty=25, trigger=95.0, tag="s1")
    g = k.gtts[int(sid)]
    assert b.stop_status(sid).status == "OPEN"
    g["status"] = "triggered"
    g["orders"][0]["result"] = {"order_result": {"status": "failure", "rejection_reason": "TPIN not authorised"}}
    st = b.stop_status(sid)
    assert st.status == "REJECTED" and "TPIN not authorised" in st.reason          # the unsold-position case, never hidden
    k.book.append({"order_id": "77", "status": "OPEN", "quantity": 25, "transaction_type": "SELL", "tradingsymbol": "INFY", "variety": "regular"})
    g["orders"][0]["result"] = {"order_result": {"status": "success", "order_id": "77"}}
    assert b.stop_status(sid).reason == "TRIGGERED_RESTING"
    k.book[-1].update(status="COMPLETE", filled_quantity=25, average_price=94.6)
    done = b.stop_status(sid)
    assert done.status == "FILLED" and done.price == 94.6
    g["status"] = "disabled"
    assert b.stop_status(sid).status == "CANCELLED"


def test_cancel_protective_stop_never_invites_a_double_sell():
    b, k = zb()
    sid = b.place_protective_stop(symbol="INFY", qty=25, trigger=95.0, tag="s1")
    assert b.cancel_protective_stop(sid) is True and k.deleted == [1]                # active: deleted
    sid2 = b.place_protective_stop(symbol="INFY", qty=25, trigger=95.0, tag="s2")
    g = k.gtts[int(sid2)]
    g["status"] = "triggered"
    k.book.append({"order_id": "88", "status": "OPEN", "quantity": 25, "transaction_type": "SELL", "tradingsymbol": "INFY", "variety": "regular"})
    g["orders"][0]["result"] = {"order_result": {"status": "success", "order_id": "88"}}
    assert b.cancel_protective_stop(sid2) is True and k.cancelled == [("regular", "88")]      # resting order cancelled
    k.book[-1].update(status="COMPLETE", filled_quantity=25, average_price=94.0)
    assert b.cancel_protective_stop(sid2) is False                                   # already sold: do NOT sell again


def test_held_quantities_merges_holdings_t1_and_todays_positions():
    b, k = zb()
    k.holdings_ = [{"tradingsymbol": "TCS", "exchange": "NSE", "quantity": 10, "t1_quantity": 5},
                   {"tradingsymbol": "ABC", "exchange": "BSE", "quantity": 99}]
    k.positions_ = {"net": [{"tradingsymbol": "INFY", "product": "CNC", "exchange": "NSE", "quantity": 25},
                            {"tradingsymbol": "TCS", "product": "CNC", "exchange": "NSE", "quantity": 12},
                            {"tradingsymbol": "ZZZ", "product": "MIS", "exchange": "NSE", "quantity": 7}]}
    assert b.held_quantities() == {"TCS": 15, "INFY": 25}


def test_kite_calls_match_the_real_sdk_signatures_and_constants():
    kc = pytest.importorskip("kiteconnect").KiteConnect
    b, k = zb()
    b.place_order(symbol="INFY", side=Side.BUY, qty=10, price=100.0, tag="t1")
    b.place_protective_stop(symbol="INFY", qty=10, trigger=95.0, tag="s1")
    b.cancel_order("1")
    inspect.signature(kc.place_order).bind(None, **k.placed[0])
    inspect.signature(kc.place_gtt).bind(None, **k.gtt_calls[0])
    inspect.signature(kc.cancel_order).bind(None, variety="regular", order_id="1")
    q, leg = k.placed[0], k.gtt_calls[0]["orders"][0]
    assert (q["order_type"], q["product"], q["validity"], q["variety"]) == (kc.ORDER_TYPE_LIMIT, kc.PRODUCT_CNC, kc.VALIDITY_DAY, kc.VARIETY_REGULAR)
    assert k.gtt_calls[0]["trigger_type"] == kc.GTT_TYPE_SINGLE and leg["transaction_type"] == kc.TRANSACTION_TYPE_SELL
    assert zb(now=WED_4PM)[0]._variety() == kc.VARIETY_AMO and kc.EXCHANGE_NSE == "NSE"


# ------------------------------------------------------------------ login + configuration

def test_request_token_can_be_pasted_bare_or_as_the_whole_redirect_url():
    assert extract_request_token("abcDEF123456") == "abcDEF123456"
    assert extract_request_token("https://127.0.0.1/?request_token=Zx9Yw8Vv7Uu6&action=login&status=success") == "Zx9Yw8Vv7Uu6"
    with pytest.raises(ValueError):
        extract_request_token("not a token!")


class FakeKiteCls:
    token = "tok-1"
    def __init__(self, api_key):
        self.api_key = api_key
    def generate_session(self, request_token, api_secret):
        assert request_token == "Rq1234567890" and api_secret == "sec"
        return {"access_token": FakeKiteCls.token, "user_id": "AB1234"}
    def set_access_token(self, t):
        self.t = t


def test_login_stores_the_token_and_the_factory_follows_new_logins():
    sf = make_session_factory(fresh_engine())
    factory = session_kite_factory(sf, "key", kite_cls=FakeKiteCls)
    with pytest.raises(BrokerError, match="kite-login"):
        factory()
    assert complete_login(sf, "key", "sec", "Rq1234567890", kite_cls=FakeKiteCls) == "AB1234"
    first = factory()
    assert first.t == "tok-1" and factory() is first                                  # cached while the token is unchanged
    FakeKiteCls.token = "tok-2"
    complete_login(sf, "key", "sec", "?request_token=Rq1234567890&x=1", kite_cls=FakeKiteCls)
    assert factory().t == "tok-2" and factory() is not first                          # next morning's login is picked up live
    FakeKiteCls.token = "tok-1"


def test_unsafe_live_settings_are_refused():
    with pytest.raises(RuntimeError, match="ZERODHA_API_KEY"):
        validate_live_settings(settings(broker="zerodha"))
    with pytest.raises(RuntimeError, match="auto"):
        validate_live_settings(settings(broker="zerodha", mode="auto", zerodha_api_key="k", zerodha_api_secret="s"))
    validate_live_settings(settings(broker="zerodha", mode="auto", live_allow_auto=True, zerodha_api_key="k", zerodha_api_secret="s"))
    with pytest.raises(RuntimeError, match="DAILY"):
        validate_live_settings(settings(broker="zerodha", zerodha_api_key="k", zerodha_api_secret="s",
                                        live_max_order_value=5000, live_max_daily_value=1000))
    assert "secret-xyz" not in repr(settings(zerodha_api_secret="secret-xyz"))


def test_live_defaults_are_the_cautious_ones():
    s = settings()
    assert s.zerodha_dry_run is True and s.live_allow_auto is False and s.filters_enforced is False


# ------------------------------------------------------------------ filters

def mk_filter_pipeline(session, filters):
    return Pipeline(session, FixedProvider({"X": frame()}), PaperBroker(), notifier=RecordingNotifier(), filters=filters)


def route(p, session):
    row = p._store_signal(buy_signal())
    p._route(row, buy_signal())
    return row


def test_event_blackout_vetoes_only_events_just_after_the_signal(tmp_path):
    f = EventBlackoutFilter({"X": [(date(2020, 1, 3), "results")]}, days=3)
    sig = buy_signal()                                       # signal bar 2020-01-01
    assert f.check(sig, datetime.now()) == (False, "results on 2020-01-03, 2 day(s) after the signal")
    assert f.check(buy_signal(symbol="Y"), datetime.now())[0] is True
    assert EventBlackoutFilter({"X": [(date(2020, 1, 9), "results")]}, days=3).check(sig, datetime.now())[0] is True
    assert EventBlackoutFilter({"X": [(date(2019, 12, 30), "results")]}, days=3).check(sig, datetime.now())[0] is True   # already past
    csv = tmp_path / "e.csv"
    csv.write_text("symbol,date,kind\nx,2020-01-03,results\n")
    assert "X" in load_events(csv)
    csv.write_text("symbol,date\nX,03/01/2020\n")
    with pytest.raises(ValueError, match="line 2"):
        load_events(csv)


def test_shadow_filter_logs_a_veto_but_the_trade_goes_ahead(session):
    p = mk_filter_pipeline(session, [EventBlackoutFilter({"X": [(date(2020, 1, 3), "results")]}, enforced=False)])
    row = route(p, session)
    assert row.status == "proposed" and session.query(OrderRow).count() == 1
    log = session.query(FilterLogRow).one()
    assert (log.verdict, log.enforced, log.filter) == ("veto", False, "event_blackout")


def test_enforced_filter_blocks_the_trade_and_says_why(session):
    p = mk_filter_pipeline(session, [EventBlackoutFilter({"X": [(date(2020, 1, 3), "results")]}, enforced=True)])
    row = route(p, session)
    assert row.status == "rejected" and row.reason == "FILTER_event_blackout" and session.query(OrderRow).count() == 0


class Crashing:
    name, enforced = "boom", False
    def check(self, sig, now):
        raise RuntimeError("x")


def test_a_crashing_filter_fails_closed_only_when_enforced(session):
    c = Crashing()
    p = mk_filter_pipeline(session, [c])
    assert route(p, session).status == "proposed"
    c.enforced = True
    p2 = mk_filter_pipeline(session, [c])
    row = p2._store_signal(buy_signal(symbol="Z"))
    p2._route(row, buy_signal(symbol="Z"))
    assert row.status == "rejected" and row.reason == "FILTER_boom"


# ------------------------------------------------------------------ migration, snapshots, review

def test_additive_migration_adds_missing_columns_to_an_old_database(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, signal_id INTEGER, symbol VARCHAR(32), side VARCHAR(4), "
                       "qty INTEGER, price FLOAT, status VARCHAR(20), mode VARCHAR(16), tag VARCHAR(20), reason VARCHAR(64), "
                       "rank VARCHAR(10), rank_score FLOAT, created_at DATETIME)"))
        c.execute(text("INSERT INTO orders (id, symbol, side, qty, status, mode, tag, created_at) "
                       "VALUES (1,'X','BUY',5,'FILLED','semi_auto','old1','2020-01-01')"))
    make_session_factory(eng)
    with eng.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(orders)"))}
        assert {"broker_order_id", "submitted_at"} <= cols
        assert c.execute(text("SELECT tag FROM orders")).scalar() == "old1"          # data untouched
    assert add_missing_columns(eng) == []                                            # idempotent


def test_daily_job_writes_the_equity_snapshot_and_reconciles(monkeypatch):
    from types import SimpleNamespace

    from test_ops import FakePipeline, fresh_frames, go
    calls = []
    class P(FakePipeline):
        def reconcile(self):
            calls.append("reconcile")
            return ["X: tradelens thinks it holds 10, the broker shows 0"]
    monkeypatch.setattr(jobs, "build_pipeline", lambda *a, **k: P([]))
    sf = make_session_factory(fresh_engine())
    rec = RecordingNotifier()
    res = jobs.run_daily(sf, settings(), FixedProvider(fresh_frames("AAA")), PaperBroker(), jobs.RiskEngine(), rec,
                         clock=lambda: datetime(2026, 9, 30, 16, 45), holidays=set())
    assert calls == ["reconcile"] and res["status"] == "ok" and res["reconcile"]
    with sf() as s:
        snap = s.query(EquitySnapshotRow).one()
    assert snap.day == datetime(2026, 9, 30) and snap.equity == 98_000.0 and snap.open_positions == 1
    assert rec.sent[-1].level == "warning" and "mismatch" in rec.sent[-1].body
    jobs.run_daily(sf, settings(), FixedProvider(fresh_frames("AAA")), PaperBroker(), jobs.RiskEngine(), rec,
                   clock=lambda: datetime(2026, 9, 30, 17, 5), holidays=set())
    with sf() as s:
        assert s.query(EquitySnapshotRow).count() == 1                                # same day: updated, not duplicated


def test_wilson_interval_is_honestly_wide():
    lo, hi = paper_review.wilson(5, 8)
    assert 0.30 < lo < 0.32 and 0.85 < hi < 0.87
    assert paper_review.wilson(0, 0) is None
    lo, hi = paper_review.wilson(60, 100)
    assert hi - lo < 0.2


def synthetic(seed):
    r = np.random.default_rng(seed).normal(0.0006, 0.013, 500)
    close = 100 * np.cumprod(1 + r)
    open_ = np.concatenate([[100.0], close[:-1]])
    idx = pd.bdate_range("2024-01-01", periods=500)
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.005, "low": np.minimum(open_, close) * 0.995,
                         "close": close, "volume": 1e6}, index=idx)


def test_review_needs_snapshots_then_reports_without_overclaiming():
    sf = make_session_factory(fresh_engine())
    prov = FixedProvider({"AAA": synthetic(1), "BBB": synthetic(2)})
    with pytest.raises(ValueError, match="at least 2 equity snapshots"):
        paper_review.review(sf, prov, settings(), benchmark=None)
    days = prov.frames["AAA"].index[-30:]
    with sf() as s:
        for i, d in enumerate(days):
            s.add(EquitySnapshotRow(day=d.to_pydatetime(), equity=100_000 + 10 * i, unrealized_pnl=0, exposure=0, open_positions=0))
        s.commit()
    r = paper_review.review(sf, prov, settings(), benchmark=None)
    assert r["window"] == [str(days[0].date()), str(days[-1].date())] and r["trading_days"] == 30
    assert r["edge_evidence"]["enough_to_judge"] is False and "Too few closed trades" in r["edge_evidence"]["statement"]
    assert r["signals"]["live_signals"] == 0 and r["signals"]["backtest_signals"] >= 0
    names = {c["check"] for c in r["checks"]}
    assert names == {"signals match the replay", "fills match the model", "daily job ran", "equity tracks the replay"}
    job = next(c for c in r["checks"] if c["check"] == "daily job ran")
    assert job["ok"] is False and job["value"] == 0                                    # no job runs were recorded
    if r["signals"]["backtest_signals"]:
        assert r["implementation_ok"] is False
        assert all(m["likely_cause"] == "no successful daily run that day" for m in r["signals"]["missing_live"])


# ------------------------------------------------------------------ factory + API surface

def test_broker_factory_picks_the_right_broker_and_refuses_unsafe_live_setups():
    from tradelens.broker.factory import build_broker
    sf = make_session_factory(fresh_engine())
    prov = FixedProvider({"X": frame()})
    assert build_broker(settings(paper_fill="next_open"), sf, prov).fill_model == "next_open"
    assert build_broker(settings(paper_fill="next_open", demo=True), sf, prov).fill_model == "instant"   # demo has no next session
    with pytest.raises(RuntimeError, match="ZERODHA_API_KEY"):
        build_broker(settings(broker="zerodha"), sf, prov)
    with pytest.raises(RuntimeError, match="unknown BROKER"):
        build_broker(settings(broker="robinhood"), sf, prov)
    live = build_broker(settings(broker="zerodha", zerodha_api_key="k", zerodha_api_secret="s"), sf, prov)
    assert isinstance(live, ZerodhaBroker) and live.dry_run is True and live.max_order_value == 10_000.0


def test_api_exposes_sync_and_review_behind_the_token():
    from fastapi.testclient import TestClient

    from tradelens.api.main import create_app
    sf = make_session_factory(fresh_engine())
    app = create_app(settings(api_token="k" * 32), provider=FixedProvider({"X": frame()}), notifier=RecordingNotifier(),
                     session_factory=sf)
    c = TestClient(app)
    assert c.post("/orders/sync").status_code == 401 and c.get("/paper-review").status_code == 401
    h = {"Authorization": "Bearer " + "k" * 32}
    assert c.post("/orders/sync", headers=h).json() == []
    r = c.get("/paper-review", headers=h)
    assert r.status_code == 400 and "equity snapshots" in r.json()["detail"]


def test_approving_via_the_api_with_a_next_open_broker_returns_submitted():
    from fastapi.testclient import TestClient

    from tradelens.api.main import create_app
    sf = make_session_factory(fresh_engine())
    prov = FixedProvider({"X": frame()})
    app = create_app(settings(), provider=prov, notifier=RecordingNotifier(), session_factory=sf,
                     broker=PaperBroker(fill_model="next_open", bars=prov.get_bars))
    with sf() as s:
        oid = proposed_order(s).id
        s.commit()
    r = TestClient(app).post(f"/orders/{oid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "SUBMITTED" and r.json()["broker_order_id"].startswith("paper|")
