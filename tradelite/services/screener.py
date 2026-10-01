"""Live screener: during NSE hours, build today's in-progress daily bar from free intraday prices and run
EVERY strategy (built-in settings + the user's saved presets) on every stored stock.

A match is PROVISIONAL: it is computed on a bar that is still forming, so it can fade before the close.
It only informs (dashboard, optional Telegram). It never creates orders; the confirmed after-close scan
and the approval step on the Desk do that. Strategy code is unchanged: the same on_bar() decides."""
from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..data.base import DataProvider
from ..data.live import LiveSource
from ..data.sources import clean_bars
from ..domain import AccountState
from ..models import LiveMatchRow, SignalRow
from ..risk.engine import RiskEngine
from ..strategies import registry
from . import presets, ranking
from .notify import NullNotifier, Notifier

IST = timezone(timedelta(hours=5, minutes=30))
OPEN, CLOSE = time(9, 15), time(15, 30)


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def market_open(now: datetime) -> bool:
    """NSE regular session, Mon-Fri 09:15-15:30 IST. Exchange holidays are not known here: on a holiday
    the source returns no bar dated today, so nothing matches."""
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def with_live_bar(history: pd.DataFrame, bar: dict, today: date) -> pd.DataFrame:
    """History up to yesterday + today's in-progress bar. Any stored bar dated today is replaced."""
    day = pd.Timestamp(today)
    hist = history[history.index < day]
    row = clean_bars(pd.DataFrame([{k: bar[k] for k in ("open", "high", "low", "close", "volume")}], index=[day]))
    return pd.concat([hist, row])


def alert_text(m: LiveMatchRow) -> str:
    rr = f" (1:{m.rr:.1f})" if m.rr else ""
    tag = " PROVEN" if m.rank == "proven" else ""
    cfg = "" if m.config_name == "default" else f" [{m.config_name}]"
    return (f"LIVE {m.strategy}{cfg}: {m.side} {m.symbol}{tag}\n"
            f"entry ~{m.entry:.2f}  stop {m.stop:.2f}  target {m.target:.2f}{rr}\n"
            "Provisional: today's bar is still forming. Confirm after the close.")


def run_cycle(sf: sessionmaker[Session], provider: DataProvider, source: LiveSource, risk: RiskEngine,
              state_fn: Callable[[Session], AccountState], *, now: datetime, notifier: Notifier | None = None,
              symbols: list[str] | None = None) -> dict:
    notifier = notifier or NullNotifier()
    today = now.date()
    syms = symbols or provider.symbols()
    summary = {"symbols": len(syms), "priced": 0, "matches": 0, "new": 0, "faded": 0, "errors": []}
    if not syms:
        return summary
    try:
        bars = source.snapshot(syms, today)
    except Exception as e:  # noqa: BLE001 - a flaky free source must never crash the loop
        summary["errors"].append(f"price source failed: {str(e)[:150]}")
        return summary
    catalog = registry.discover()
    new_rows: list[LiveMatchRow] = []
    with sf() as s:
        preset_map = presets.for_strategies(s, sorted(catalog))
        state = state_fn(s)
        for sym in syms:
            bar = bars.get(sym)
            if bar is None or bar["date"] != today:
                continue
            summary["priced"] += 1
            try:
                df = with_live_bar(provider.get_bars(sym), bar, today)
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"{sym}: {str(e)[:100]}")
                continue
            hits: set[tuple[str, str]] = set()
            for name, cls in catalog.items():
                for cfg_name, params in [(presets.DEFAULT, {})] + [(r.name, r.params) for r in preset_map[name]]:
                    strat = cls(**params)
                    if "1d" not in strat.meta.timeframes or len(df) < strat.meta.min_bars + 2:
                        continue
                    sig = strat.on_bar(strat.prepare(df), len(df) - 1, sym)
                    if sig is None:
                        continue
                    hits.add((name, cfg_name))
                    rank, score = ranking.rank_for(s, name, cfg_name, sym)
                    decision = risk.evaluate(sig, state)
                    row = s.scalar(select(LiveMatchRow).where(
                        LiveMatchRow.strategy == name, LiveMatchRow.config_name == cfg_name,
                        LiveMatchRow.symbol == sym, LiveMatchRow.trading_day == today))
                    if row is None:
                        row = LiveMatchRow(strategy=name, config_name=cfg_name, symbol=sym, trading_day=today,
                                           first_seen=now)
                        s.add(row)
                        new_rows.append(row)
                    row.status, row.side, row.last_seen = "live", sig.side.value, now
                    row.entry, row.stop, row.target, row.rr = sig.entry, sig.stop, sig.target, sig.rr
                    row.rank, row.rank_score = rank, score
                    row.suggested_qty, row.fit = (decision.qty if decision.approved else 0), decision.reason
            for old in s.scalars(select(LiveMatchRow).where(
                    LiveMatchRow.symbol == sym, LiveMatchRow.trading_day == today, LiveMatchRow.status == "live")).all():
                if (old.strategy, old.config_name) not in hits:   # condition no longer true on the forming bar
                    old.status = "faded"
                    summary["faded"] += 1
        s.commit()
        summary["matches"] = len(s.scalars(select(LiveMatchRow).where(
            LiveMatchRow.trading_day == today, LiveMatchRow.status == "live")).all())
        summary["new"] = len(new_rows)
        for m in sorted(new_rows, key=lambda r: (r.rank != "proven", -r.rank_score)):
            try:
                notifier.send(alert_text(m))
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"alert failed: {str(e)[:100]}")
                break
    return summary


class LiveScreener:
    """Owns the polling loop and the last-run status. run_once() is also what the 'Check now' button calls."""

    def __init__(self, sf, provider, source: LiveSource | None, risk: RiskEngine, state_fn, *, interval: int = 300,
                 notifier: Notifier | None = None, clock: Callable[[], datetime] = ist_now, enabled: bool = True) -> None:
        self.sf, self.provider, self.source, self.risk, self.state_fn = sf, provider, source, risk, state_fn
        self.interval, self.notifier, self.clock, self.enabled = interval, notifier or NullNotifier(), clock, enabled
        self.telegram = not isinstance(self.notifier, NullNotifier)
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict | None = None
        self.last_run: datetime | None = None

    def run_once(self, force: bool = False) -> dict:
        now = self.clock()
        if self.source is None:
            return {"ran": False, "reason": "no live price source is configured (demo mode)"}
        if not force and not market_open(now):
            return {"ran": False, "reason": "market is closed (NSE trades Mon-Fri 09:15-15:30 IST)"}
        if not self._lock.acquire(blocking=False):
            return {"ran": False, "reason": "a check is already running"}
        try:
            self.last = run_cycle(self.sf, self.provider, self.source, self.risk, self.state_fn, now=now,
                                  notifier=self.notifier)
            self.last_run = now
            return {"ran": True, **self.last}
        finally:
            self._lock.release()

    def status(self) -> dict:
        now = self.clock()
        return {"enabled": self.enabled and self.source is not None, "market_open": market_open(now),
                "now_ist": now.isoformat(timespec="seconds"), "interval_seconds": self.interval,
                "last_run": self.last_run.isoformat(timespec="seconds") if self.last_run else None,
                "last_result": self.last, "telegram": self.telegram,
                "source": getattr(self.source, "name", None)}

    def start(self) -> None:
        if not self.enabled or self.source is None or self._thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - keep polling whatever happens
                    pass
                if self._stop.wait(self.interval):
                    return

        self._thread = threading.Thread(target=loop, name="live-screener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
