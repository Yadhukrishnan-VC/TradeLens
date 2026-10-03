"""Seed a fresh SQLite DB with one PENDING_APPROVAL order on DEMO1 and one proven strategy/stock pair (used by the UI end-to-end test)."""
import sys
from datetime import datetime

from tradelens.data.synthetic import make_bars
from tradelens.db import make_engine, make_session_factory
from tradelens.models import LiveMatchRow, OrderRow, SignalRow, StrategyFitRow, WatchRow
from tradelens.services.screener import ist_now

url = sys.argv[1] if len(sys.argv) > 1 else "sqlite:///e2e.db"
df = make_bars("DEMO1")
close, ts = float(df["close"].iloc[-1]), df.index[-1].to_pydatetime()   # last bar => signal is not stale
with make_session_factory(make_engine(url))() as s:
    sig = SignalRow(strategy="ema_cross", symbol="DEMO1", side="BUY", ts=ts, entry=close, stop=close * 0.97,
                    target=close * 1.06, status="proposed", suggested_qty=10, rank="proven", rank_score=1.8,
                    created_at=datetime.now())
    s.add(sig); s.flush()
    s.add(OrderRow(signal_id=sig.id, symbol="DEMO1", side="BUY", qty=10, status="PENDING_APPROVAL",
                   mode="semi_auto", tag="e2e-seed-0001", rank="proven", rank_score=1.8, created_at=datetime.now()))
    # a pair that "worked": shows up in the Track Record view (macd_cross on DEMO2 is not touched by the UI test)
    # a signal the gate held back, with its flags (Signals page), and a setup close to triggering (Screener page)
    s.add(SignalRow(strategy="macd_cross", symbol="DEMO3", side="BUY", ts=ts, entry=close, stop=close * 0.97, target=close * 1.06,
                    status="gated", reason="NOT_VALIDATED", suggested_qty=0, rank="unproven", quality="warn",
                    flags="OFF_REGIME,LOW_LIQUIDITY", regime="down/calm", gate="block", gate_reason="NOT_VALIDATED",
                    created_at=datetime.now()))
    s.add(WatchRow(strategy="donchian_breakout", config_name="default", symbol="DEMO2", bar_ts=ts, close=close, trigger=round(close * 1.012, 2),
                   distance_pct=1.2, note="within 1.2% of the 20-day high; a close above it triggers", created_at=datetime.now()))
    now = ist_now()
    s.add(LiveMatchRow(strategy="donchian_breakout", config_name="default", symbol="DEMO3", trading_day=now.date(),
                       status="live", side="BUY", entry=250.0, stop=240.0, target=300.0, rr=5.0, rank="proven", rank_score=1.9,
                       suggested_qty=12, fit="OK", flags="EVENT:earnings,LOW_LIQUIDITY", first_seen=now, last_seen=now))
    s.add(LiveMatchRow(strategy="ema_cross", config_name="default", symbol="DEMO2", trading_day=now.date(),
                       status="faded", side="BUY", entry=100.0, stop=95.0, target=110.0, rr=2.0, rank="unproven", rank_score=0.0,
                       suggested_qty=0, fit="SIZE_ZERO_RISK_BUDGET", first_seen=now, last_seen=now))
    s.add(StrategyFitRow(strategy="macd_cross", config_name="default", symbol="DEMO2", timeframe="1d",
                         start=datetime(2018, 1, 1), end=datetime(2023, 9, 1), n_bars=1500, avg_volume=4.4e5,
                         n_trades=41, profit_factor=1.7, expectancy=120.0, verdict="candidate",
                         detail={"last_win": "2023-08-14T00:00:00", "early": {"profit_factor": 1.6}, "late": {"profit_factor": 1.9}},
                         updated_at=datetime.now()))
    s.commit()
print("seeded", url, "close", round(close, 2))
