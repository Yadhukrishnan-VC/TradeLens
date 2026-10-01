from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import validate_bars


class CsvProvider:
    """Reads {data_dir}/{SYMBOL}.csv with columns: date,open,high,low,close,volume."""

    def __init__(self, data_dir: str) -> None:
        self.dir = Path(data_dir)

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.csv"))

    def get_bars(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame:
        path = self.dir / f"{symbol}.csv"
        if not path.exists():
            raise FileNotFoundError(f"no data file for {symbol}: {path}")
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        date_col = "date" if "date" in df.columns else "timestamp"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        return validate_bars(df[["open", "high", "low", "close", "volume"]].astype(float))
