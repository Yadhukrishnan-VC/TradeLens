from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from ..data.base import validate_bars
from ..data.csv_provider import CsvProvider
from ..data.sources import HistorySource
from ..models import PriceBarRow


def store_bars(session: Session, symbol: str, df: pd.DataFrame, source: str) -> int:
    """Replace the stored bars for the covered date range (idempotent: re-fetching never duplicates)."""
    validate_bars(df)
    session.execute(delete(PriceBarRow).where(
        PriceBarRow.symbol == symbol, PriceBarRow.ts >= df.index[0].to_pydatetime(),
        PriceBarRow.ts <= df.index[-1].to_pydatetime()))
    session.execute(PriceBarRow.__table__.insert(), [
        {"symbol": symbol, "ts": ts.to_pydatetime(), "open": r.open, "high": r.high, "low": r.low,
         "close": r.close, "volume": r.volume, "source": source}
        for ts, r in df.iterrows()])
    session.commit()
    return len(df)


def fetch_symbols(sf: sessionmaker[Session], source: HistorySource, symbols: list[str], years: float = 5.0,
                  today: date | None = None) -> list[dict]:
    """Fetch + store each symbol independently; one failure never blocks the others."""
    end = (today or date.today()) + timedelta(days=1)
    start = end - timedelta(days=int(365.25 * years))
    out: list[dict] = []
    for raw in symbols:
        sym = raw.strip().upper()
        if not sym:
            continue
        try:
            df = source.fetch(sym, start, end)
            with sf() as s:
                n = store_bars(s, sym, df, source.name)
            out.append({"symbol": sym, "ok": True, "bars": n,
                        "start": df.index[0].date().isoformat(), "end": df.index[-1].date().isoformat()})
        except Exception as e:  # noqa: BLE001 - report every failure per symbol
            out.append({"symbol": sym, "ok": False, "error": str(e)[:200]})
    return out


def import_csv_dir(sf: sessionmaker[Session], data_dir: str) -> list[dict]:
    prov = CsvProvider(data_dir)
    out: list[dict] = []
    for sym in prov.symbols():
        try:
            df = prov.get_bars(sym)
            with sf() as s:
                n = store_bars(s, sym, df, "csv")
            out.append({"symbol": sym, "ok": True, "bars": n})
        except Exception as e:  # noqa: BLE001
            out.append({"symbol": sym, "ok": False, "error": str(e)[:200]})
    return out


def coverage(session: Session) -> list[dict]:
    rows = session.execute(
        select(PriceBarRow.symbol, func.count(), func.min(PriceBarRow.ts), func.max(PriceBarRow.ts))
        .group_by(PriceBarRow.symbol).order_by(PriceBarRow.symbol)).all()
    return [{"symbol": s, "bars": n, "start": a.date().isoformat(), "end": b.date().isoformat()} for s, n, a, b in rows]
