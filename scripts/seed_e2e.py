"""Seed a fresh SQLite DB with one PENDING_APPROVAL order on DEMO1 and one proven strategy/stock pair (used by the UI end-to-end test)."""
import sys
from datetime import datetime

from tradelite.data.synthetic import make_bars
from tradelite.db import make_engine, make_session_factory
from tradelite.models import OrderRow, SignalRow, StrategyFitRow

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
    s.add(StrategyFitRow(strategy="macd_cross", config_name="default", symbol="DEMO2", timeframe="1d",
                         start=datetime(2018, 1, 1), end=datetime(2023, 9, 1), n_bars=1500, avg_volume=4.4e5,
                         n_trades=41, profit_factor=1.7, expectancy=120.0, verdict="candidate",
                         detail={"last_win": "2023-08-14T00:00:00", "early": {"profit_factor": 1.6}, "late": {"profit_factor": 1.9}},
                         updated_at=datetime.now()))
    s.commit()
print("seeded", url, "close", round(close, 2))
