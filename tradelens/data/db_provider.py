from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import PriceBarRow
from .base import validate_bars


class DbProvider:
    """Reads daily bars stored in the database (filled by the ingest service)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.sf = session_factory

    def symbols(self) -> list[str]:
        with self.sf() as s:
            return sorted(s.scalars(select(PriceBarRow.symbol).distinct()).all())

    def get_bars(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame:
        if timeframe != "1d":
            raise ValueError(f"database provider only stores 1d bars, not {timeframe}")
        with self.sf() as s:
            rows = s.execute(
                select(PriceBarRow.ts, PriceBarRow.open, PriceBarRow.high, PriceBarRow.low,
                       PriceBarRow.close, PriceBarRow.volume)
                .where(PriceBarRow.symbol == symbol).order_by(PriceBarRow.ts)).all()
        if not rows:
            raise FileNotFoundError(f"no stored price data for {symbol}. Fetch it first (POST /data/fetch or `tradelens fetch`).")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"])
        return validate_bars(df.set_index("ts").astype(float))
