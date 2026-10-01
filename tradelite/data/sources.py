"""Free historical-data sources. Each returns a clean daily OHLCV DataFrame (DatetimeIndex, no tz)."""
from __future__ import annotations

from datetime import date
from typing import Callable, Protocol

import pandas as pd

# A liquid starting universe (NSE symbols). Fetch errors are reported per symbol, never hidden.
NIFTY_LARGE_CAPS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL",
    "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "NESTLEIND", "BAJFINANCE", "WIPRO", "HCLTECH", "ONGC", "NTPC", "POWERGRID", "TATASTEEL",
]


class HistorySource(Protocol):
    name: str

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...


def clean_bars(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise a provider frame: lower-case columns, tz-free daily index, drop unusable rows and
    repair sub-paisa rounding so high/low bound open/close (validate_bars stays strict)."""
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna()
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1) & (df["volume"] >= 0)]
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    return df


class YahooSource:
    """Yahoo Finance via the `yfinance` package: free, no API key. Prices are split/dividend
    adjusted (auto_adjust), which is what a backtest needs. Unofficial API: it can rate-limit or
    change, so failures are surfaced per symbol."""
    name = "yahoo"

    def __init__(self, suffix: str = ".NS", downloader: Callable[[str, date, date], pd.DataFrame] | None = None) -> None:
        self.suffix = suffix
        self._download = downloader or self._yfinance

    def ticker(self, symbol: str) -> str:
        return symbol if ("." in symbol or symbol.startswith("^")) else symbol + self.suffix

    @staticmethod
    def _yfinance(ticker: str, start: date, end: date) -> pd.DataFrame:
        import yfinance as yf  # imported lazily: only needed when actually fetching
        return yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat(),
                                         interval="1d", auto_adjust=True)

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        raw = self._download(self.ticker(symbol), start, end)
        if raw is None or len(raw) == 0:
            raise ValueError(f"{self.ticker(symbol)}: provider returned no data")
        return clean_bars(raw)


SOURCES: dict[str, Callable[[], HistorySource]] = {"yahoo": YahooSource}


def get_source(name: str) -> HistorySource:
    if name not in SOURCES:
        raise KeyError(f"unknown data source '{name}'. available: {sorted(SOURCES)}")
    return SOURCES[name]()
