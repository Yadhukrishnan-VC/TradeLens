"""Event calendar: dates that make a new trade riskier (results, board meetings, policy days).

A breakout the day before results is a coin flip with a gap attached; the strategies cannot know that, so the
calendar tells the gate. Symbol '*' means the whole market (budget day, rate decision).

Free sources only: a CSV you maintain (symbol,date,kind[,note]) and a best-effort Yahoo lookup of earnings
dates. Yahoo's calendar for NSE names is patchy, so a missing event is NOT a guarantee of no event.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EventRow
from .breaker import get_breaker


def add_event(session: Session, symbol: str, day: date, kind: str = "event", note: str = "", source: str = "manual") -> EventRow:
    symbol, kind = symbol.strip().upper() or "*", (kind.strip().lower() or "event")[:32]
    row = session.scalar(select(EventRow).where(EventRow.symbol == symbol, EventRow.day == day, EventRow.kind == kind))
    if row is None:
        row = EventRow(symbol=symbol, day=day, kind=kind)
        session.add(row)
    row.note, row.source = note[:200], source
    session.commit()
    return row


def parse_csv(text: str) -> tuple[list[tuple[str, date, str, str]], list[str]]:
    """symbol,date,kind[,note]. A header line is optional. Returns (rows, problems); bad lines never abort the rest."""
    rows, problems = [], []
    for n, rec in enumerate(csv.reader(io.StringIO(text)), 1):
        if not rec or not "".join(rec).strip() or rec[0].strip().startswith("#"):
            continue
        if n == 1 and rec[0].strip().lower() == "symbol":
            continue
        try:
            rows.append((rec[0].strip().upper() or "*", date.fromisoformat(rec[1].strip()),
                         (rec[2].strip() if len(rec) > 2 and rec[2].strip() else "event"), rec[3].strip() if len(rec) > 3 else ""))
        except (IndexError, ValueError):
            problems.append(f"line {n}: expected symbol,YYYY-MM-DD,kind")
    return rows, problems


def import_csv(session: Session, text: str) -> dict:
    rows, problems = parse_csv(text)
    for sym, day, kind, note in rows:
        add_event(session, sym, day, kind, note, source="csv")
    return {"imported": len(rows), "problems": problems}


def upcoming(session: Session, symbol: str, on: date, window_days: int) -> list[EventRow]:
    """Events for this symbol (or the whole market) from `on` through `on + window_days`."""
    return list(session.scalars(select(EventRow).where(
        EventRow.symbol.in_([symbol, "*"]), EventRow.day >= on, EventRow.day <= on + timedelta(days=window_days)
    ).order_by(EventRow.day)).all())


def _yahoo_earnings(ticker: str) -> list[date]:
    import yfinance as yf   # lazy: only needed when actually fetching
    cal = yf.Ticker(ticker).calendar
    raw = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
    return [d if isinstance(d, date) else d.date() for d in raw]


def fetch_earnings(session: Session, symbols: list[str], *, suffix: str = ".NS",
                   fetcher: Callable[[str], list[date]] = _yahoo_earnings) -> list[dict]:
    """Best-effort: pull upcoming earnings dates from Yahoo. One failing symbol never stops the rest, and a
    failing SERVICE (several errors in a row) pauses the lookups through the shared circuit breaker."""
    breaker = get_breaker("yahoo-events")
    out: list[dict] = []
    for sym in symbols:
        if not breaker.allow():
            out.append({"symbol": sym, "ok": False, "error": "skipped: Yahoo paused after repeated failures"})
            continue
        try:
            days = [d for d in fetcher(sym if "." in sym else sym + suffix) if d >= date.today()]
            breaker.record_success()
        except Exception as e:  # noqa: BLE001
            breaker.record_failure(type(e).__name__)
            out.append({"symbol": sym, "ok": False, "error": type(e).__name__})
            continue
        for d in days:
            add_event(session, sym, d, "earnings", source="yahoo")
        out.append({"symbol": sym, "ok": True, "events": len(days)})
    return out
