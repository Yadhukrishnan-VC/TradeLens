"""The daily job and the worker loop that schedules it.

Order matters: refresh prices -> refuse to trade on stale prices -> exits (frees slots) -> scan -> alert.
Everything downstream is idempotent (signals are unique per strategy/symbol/bar, orders per tag), so a
retry or a restart in the middle of a run cannot double-trade.

Why the refresh downloads the FULL history: Yahoo prices are dividend/split adjusted. A corporate action
rewrites every earlier bar, so refreshing only the last few days would splice adjusted and unadjusted
prices together and create fake jumps that trip indicators.
"""
from __future__ import annotations

import logging
import signal as _signal
import time as _time
import urllib.request
from datetime import date, datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..broker.base import Broker
from ..config import Settings
from ..data.base import DataProvider
from ..data.sources import HistorySource
from ..domain import Mode
from ..models import JobRunRow, SignalRow
from ..risk.engine import RiskEngine
from . import ingest
from .alerts import Alert, Notifier
from .gate import GateConfig
from .pipeline import Pipeline, ist_now
from .schedule import due, expected_last_bar, load_holidays, parse_hhmm

log = logging.getLogger("tradelite.jobs")
JOB = "daily"


class StaleDataError(RuntimeError):
    pass


def build_pipeline(s: Session, settings: Settings, provider: DataProvider, broker: Broker, risk: RiskEngine,
                   notifier: Notifier | None, mode: Mode | None = None, require_fit: bool = False) -> Pipeline:
    mode = mode or Mode(settings.mode)
    return Pipeline(s, provider, broker, risk=risk, mode=mode, starting_capital=settings.starting_capital,
                    require_fit=require_fit, notifier=notifier, gate=GateConfig.from_settings(settings, mode),
                    benchmark=settings.benchmark_symbol, stale_check=True)


def stale_symbols(provider: DataProvider, symbols: list[str], expected: date) -> list[str]:
    out = []
    for sym in symbols:
        try:
            if provider.get_bars(sym).index[-1].date() < expected:
                out.append(sym)
        except (FileNotFoundError, ValueError):
            out.append(sym)
    return out


def run_daily(sf: sessionmaker[Session], settings: Settings, provider: DataProvider, broker: Broker, risk: RiskEngine,
              notifier: Notifier, *, source: HistorySource | None = None, clock: Callable[[], datetime] = ist_now,
              holidays: set | None = None, ping: Callable[[str], None] | None = None) -> dict:
    """One full pass. Never raises: failures are recorded, alerted and returned as status='failed'."""
    now = clock()
    holidays = load_holidays(settings.holidays_file) if holidays is None else holidays
    with sf() as s:
        run = JobRunRow(job=JOB, started_at=now, status="running", detail={})
        s.add(run)
        s.commit()
        run_id = run.id
    detail: dict = {"warnings": []}
    status = "ok"
    try:
        # 1) refresh prices (only when prices live in the database and we have a source to pull from)
        if source is not None and settings.data_source == "db" and not settings.demo:
            results = ingest.fetch_symbols(sf, source, provider.symbols(), settings.refresh_years, today=now.date())
            bad = [f"{r['symbol']}: {r['error']}" for r in results if not r["ok"]]
            detail["fetch"] = {"ok": sum(r["ok"] for r in results), "failed": bad}
            if bad:
                detail["warnings"].append(f"{len(bad)} symbol(s) failed to refresh")

        # 2) never trade on stale prices
        symbols = provider.symbols()
        if not symbols:
            raise StaleDataError("no symbols with price data: fetch history first")
        if settings.demo:
            fresh, stale = symbols, []
        else:
            stale = stale_symbols(provider, symbols, expected_last_bar(now, holidays))
            fresh = [x for x in symbols if x not in stale]
        detail["stale"] = stale
        if not fresh:
            raise StaleDataError(f"no symbol has a bar for {expected_last_bar(now, holidays)} yet (exchange holiday missing "
                                 f"from HOLIDAYS_FILE, or the data source is behind). Nothing was scanned.")
        if stale:
            detail["warnings"].append(f"{len(stale)} symbol(s) skipped, stale data: {', '.join(stale[:10])}")

        # 3) exits first: they free slots and cash for the scan
        with sf() as s:
            pipe = build_pipeline(s, settings, provider, broker, risk, notifier)
            closed = pipe.check_exits()
            advice = pipe.review_positions()      # strategy opinions (EXIT / REDUCE) on what is still open; advice only
            detail["advice"] = [{"symbol": a.symbol, "action": a.action, "reason": a.reason} for a in advice]
            detail["exits"] = [{"symbol": p.symbol, "reason": p.exit_reason, "pnl": round(p.pnl or 0.0, 2)} for p in closed]
            for p in closed:
                notifier.send(Alert("warning" if p.exit_reason == "stop" else "info",
                                    f"{p.symbol} closed at {p.exit_reason}: P&L {p.pnl:+,.0f}",
                                    f"{p.strategy} {p.side} qty {p.qty}, entry {p.entry_price:.2f} -> exit {p.exit_price:.2f}"))

        # 4) scan
        with sf() as s:
            pipe = build_pipeline(s, settings, provider, broker, risk, notifier)
            rows = pipe.scan(symbols=fresh)
            new = [(r.id, r.strategy, r.symbol, r.status, r.reason, r.suggested_qty, r.entry, r.stop, r.target, r.rank)
                   for r in rows]
            st = pipe.account_state()
            watch = list(pipe.last_watch)
        proposed = [n for n in new if n[3] == "proposed"]
        executed = [n for n in new if n[3] == "executed"]
        rejected: dict[str, int] = {}
        for n in new:
            if n[3] == "rejected":
                rejected[n[4] or "?"] = rejected.get(n[4] or "?", 0) + 1
        gated = [n for n in new if n[3] == "gated"]
        detail["scan"] = {"new_signals": len(new), "proposed": len(proposed), "executed": len(executed), "rejected": rejected,
                          "gated": len(gated), "watch": len(watch)}
        for _, strat, sym, _, _, qty, entry, stop, target, rank in proposed[:10]:
            notifier.send(Alert("info", f"Approve? BUY {qty} {sym} @ ~{entry:.2f}",
                                f"{strat} ({rank}) stop {stop:.2f}" + (f", target {target:.2f}" if target else "")
                                + ". Pending in the dashboard; it goes stale when the next bar prints."))

        # 5) conditions a human should know about
        limit = risk.cfg.daily_loss_limit_pct * st.equity
        if st.realized_pnl_today <= -limit:
            notifier.send(Alert("warning", "Daily loss limit reached", f"realized today {st.realized_pnl_today:+,.0f} "
                                f"(limit {limit:,.0f}). New trades are blocked until tomorrow."))
        if st.kill_switch:
            detail["warnings"].append("kill switch is ON: nothing new is opened")
        if st.unpriced_positions:
            detail["warnings"].append(f"{st.unpriced_positions} open position(s) have no price; equity marks them at entry")
        detail["account"] = {"equity": round(st.equity, 2), "unrealized_pnl": round(st.unrealized_pnl, 2),
                             "open_positions": st.open_positions}

        summary = (f"{len(new)} new signal(s): {len(proposed)} awaiting approval, {len(executed)} executed, "
                   f"{len(gated)} held back by the gate, {sum(rejected.values())} rejected. {len(closed)} position(s) closed. "
                   f"Equity {st.equity:,.0f} (open P&L {st.unrealized_pnl:+,.0f}).")
        if watch:
            summary += "\nWatch tomorrow: " + "; ".join(
                f"{w['symbol']} ({w['strategy']})" + (f" above {w['trigger']:.2f}" if w["trigger"] else "") for w in watch[:5])
        if advice:
            summary += "\nStrategy advice on open positions: " + "; ".join(f"{a.action} {a.symbol}" for a in advice[:5])
        if detail["warnings"]:
            summary += "\n" + "\n".join("- " + w for w in detail["warnings"])
        notifier.send(Alert("warning" if detail["warnings"] else "info", "Daily run complete", summary))
    except Exception as e:  # noqa: BLE001 - the worker must survive anything
        status = "failed"
        detail["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        log.exception("daily job failed")
        notifier.send(Alert("critical", "Daily run FAILED", detail["error"] + "\nIt will be retried; check the worker logs."))
    with sf() as s:
        row = s.get(JobRunRow, run_id)
        row.status, row.finished_at, row.detail = status, clock(), detail
        s.commit()
    if status == "ok" and settings.healthcheck_ping_url:
        (ping or _ping)(settings.healthcheck_ping_url)
    return {"run_id": run_id, "status": status, **detail}


def _ping(url: str) -> None:
    from .alerts import safe_http_url
    try:
        with urllib.request.urlopen(safe_http_url(url), timeout=10):   # noqa: S310 - scheme checked above
            pass
    except Exception as e:  # noqa: BLE001
        log.error("heartbeat ping failed: %s", type(e).__name__)


def todays_runs(s: Session, now: datetime) -> list[tuple[str, datetime]]:
    start = datetime.combine(now.date(), datetime.min.time())
    rows = s.scalars(select(JobRunRow).where(JobRunRow.job == JOB, JobRunRow.started_at >= start)).all()
    return [(r.status, r.started_at) for r in rows]


def worker_loop(sf: sessionmaker[Session], settings: Settings, provider: DataProvider, broker: Broker, risk: RiskEngine,
                notifier: Notifier, source: HistorySource | None, *, clock: Callable[[], datetime] = ist_now,
                sleep: Callable[[float], None] = _time.sleep, max_ticks: int | None = None, poll_seconds: float = 30.0) -> None:
    """Checks every `poll_seconds` whether the daily job is due. Restart-safe: what already ran today is read
    from the database, never from memory."""
    run_at = parse_hhmm(settings.daily_run_at)
    holidays = load_holidays(settings.holidays_file)
    stop = {"now": False}
    previous = {}
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            previous[sig] = _signal.signal(sig, lambda *_: stop.update(now=True))
        except ValueError:      # not the main thread (tests)
            pass
    notifier.send(Alert("info", "Worker started", f"Daily run at {settings.daily_run_at} IST on trading days. "
                        f"Mode {settings.mode}, broker {settings.broker}."))
    ticks = 0
    try:
        while not stop["now"] and (max_ticks is None or ticks < max_ticks):
            ticks += 1
            now = clock()
            with sf() as s:
                runs = todays_runs(s, now)
            if due(now, run_at, runs, holidays):
                res = run_daily(sf, settings, provider, broker, risk, notifier, source=source, clock=clock, holidays=holidays)
                if res["status"] == "failed" and len(runs) + 1 >= 3:
                    notifier.send(Alert("critical", "Daily run gave up after 3 attempts", "No scan happened today. Fix the cause, "
                                        "then run `tradelite run-daily` by hand."))
            for _ in range(int(poll_seconds)):
                if stop["now"]:
                    break
                sleep(1.0 if max_ticks is None else 0.0)
    finally:
        for sig, handler in previous.items():
            _signal.signal(sig, handler)
    log.info("worker stopped")
