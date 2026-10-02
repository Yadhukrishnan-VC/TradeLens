"""Is the data behind a signal trustworthy? Every signal carries the answer, so a human sees it next to the
entry and stop instead of finding out later in a log.

level 'bad'  -> a trade must NOT be opened from this signal (it is still recorded and shown)
level 'warn' -> shown, trade allowed
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

LOOKBACK = 250            # bars inspected
RECENT = 20               # a jump this recent makes the signal untrustworthy
MAX_GAP_BUSINESS_DAYS = 5
JUMP_PCT = 0.35           # one-day move with no split in the data: suspicious
MIN_TRADED_VALUE = 5_000_000.0   # rupees per day (20-bar median) below which fills are doubtful


@dataclass(frozen=True)
class Quality:
    level: str                     # ok | warn | bad
    flags: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return ",".join(self.flags)


def assess(df: pd.DataFrame, *, latest_date: date | None = None, min_traded_value: float = MIN_TRADED_VALUE) -> Quality:
    """`latest_date`: the newest bar date seen across the universe. A symbol behind it is STALE."""
    flags: list[str] = []
    bad = False
    if df.empty:
        return Quality("bad", ("NO_DATA",))
    last = df.index[-1].date()
    if latest_date is not None and last < latest_date:
        flags.append("STALE")
        bad = True
    tail = df.iloc[-LOOKBACK:]
    if len(tail) > 1:
        idx = tail.index.values.astype("datetime64[D]")
        gaps = np.busday_count(idx[:-1], idx[1:])
        if (gaps > MAX_GAP_BUSINESS_DAYS).any():
            flags.append("GAP")
        moves = tail["close"].pct_change().abs()
        jumps = moves > JUMP_PCT
        if jumps.iloc[-RECENT:].any():
            flags.append("JUMP")
            bad = True
        elif jumps.any():
            flags.append("OLD_JUMP")
    if (df["volume"].iloc[-5:] == 0).any():
        flags.append("ZERO_VOLUME")
    traded = (df["close"] * df["volume"]).iloc[-20:].median()
    if traded < min_traded_value:
        flags.append("LOW_LIQUIDITY")
    return Quality("bad" if bad else ("warn" if flags else "ok"), tuple(flags))
