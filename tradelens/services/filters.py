"""Signal filters. They can only VETO a trade the risk engine already approved; they never create or enlarge one.

Every verdict is written to filter_log, including SHADOW verdicts (enforced=False) that block nothing. Run new filters in
shadow during paper trading, then compare the outcome of trades the filter would have vetoed with the ones it would have
allowed (paper review, "filters" section) BEFORE switching enforcement on. A filter that has not been shown to help
should not be enforced.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from ..domain import Signal


class SignalFilter(Protocol):
    name: str
    enforced: bool

    def check(self, sig: Signal, now: datetime) -> tuple[bool, str]: ...


def load_events(path: str | Path) -> dict[str, list[tuple[date, str]]]:
    """CSV with header symbol,date,kind. kind is free text (results, dividend, split, agm ...)."""
    out: dict[str, list[tuple[date, str]]] = defaultdict(list)
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames or not {"symbol", "date"} <= {c.strip().lower() for c in rd.fieldnames}:
            raise ValueError(f"{path}: need columns symbol,date[,kind]")
        for n, row in enumerate(rd, start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            try:
                out[row["symbol"].upper()].append((date.fromisoformat(row["date"]), row.get("kind") or "event"))
            except ValueError as e:
                raise ValueError(f"{path} line {n}: bad date '{row['date']}'") from e
    return dict(out)


class EventBlackoutFilter:
    """Veto when a known event (results date, ex-dividend...) falls within `days` calendar days after the signal date,
    i.e. the new position would be open through it. Events come from a file YOU maintain: nothing is fetched."""

    def __init__(self, events: dict[str, list[tuple[date, str]]], days: int = 3, enforced: bool = False) -> None:
        if days < 0:
            raise ValueError("days must be >= 0")
        self.events, self.days, self.enforced, self.name = events, days, enforced, "event_blackout"

    def check(self, sig: Signal, now: datetime) -> tuple[bool, str]:
        d0 = sig.ts.date()
        for d, kind in self.events.get(sig.symbol.upper(), []):
            if 0 <= (d - d0).days <= self.days:
                return False, f"{kind} on {d.isoformat()}, {(d - d0).days} day(s) after the signal"
        return True, ""
