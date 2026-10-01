"""When should the daily job run? Pure functions (no clock, no DB) so they are easy to test."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

MARKET_CLOSE = time(15, 30)   # NSE cash session ends 15:30 IST
STALE_RUNNING_AFTER = timedelta(minutes=30)


def load_holidays(path: str | Path | None) -> set[date]:
    """One YYYY-MM-DD per line; blank lines and '#' comments are ignored. Keep this file current
    yourself (take dates from the exchange's holiday circular): nothing here is hard-coded."""
    if not path:
        return set()
    out: set[date] = set()
    for n, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            out.add(date.fromisoformat(line))
        except ValueError as e:
            raise ValueError(f"{path} line {n}: expected YYYY-MM-DD, got '{line}'") from e
    return out


def is_trading_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def previous_trading_day(d: date, holidays: set[date]) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d


def expected_last_bar(now: datetime, holidays: set[date]) -> date:
    """Date of the newest daily bar that should exist right now: today once the session has closed
    on a trading day, otherwise the previous trading day."""
    if is_trading_day(now.date(), holidays) and now.time() >= MARKET_CLOSE:
        return now.date()
    return previous_trading_day(now.date(), holidays)


def parse_hhmm(text: str) -> time:
    try:
        h, m = text.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"DAILY_RUN_AT must look like 16:30, got '{text}'") from e


def due(now: datetime, run_at: time, todays_runs: list[tuple[str, datetime]], holidays: set[date], *,
        retry_after: timedelta = timedelta(minutes=15), max_attempts: int = 3) -> bool:
    """`todays_runs`: (status, started_at) of today's runs of this job. Runs once per trading day,
    at or after `run_at`; a failed run is retried after `retry_after`, up to `max_attempts` in total."""
    if not is_trading_day(now.date(), holidays) or now.time() < run_at:
        return False
    if any(st == "ok" for st, _ in todays_runs):
        return False
    for st, started in todays_runs:
        if st == "running" and now - started < STALE_RUNNING_AFTER:
            return False                       # another worker is on it
    attempts = [started for _, started in todays_runs]
    if len(attempts) >= max_attempts:
        return False
    return not attempts or now - max(attempts) >= retry_after
