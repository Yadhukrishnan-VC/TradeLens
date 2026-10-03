"""Free intraday prices for the live screener (Yahoo Finance via yfinance, 5-minute bars).
Turns today's 5-minute bars into ONE in-progress daily bar. Yahoo's intraday NSE data can be delayed
and is unofficial; anything it returns is treated as provisional."""
from __future__ import annotations

from datetime import date, timedelta, timezone
from typing import Callable, Protocol

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))


class LiveSource(Protocol):
    name: str

    def snapshot(self, symbols: list[str], today: date) -> dict[str, dict]: ...


def aggregate_today(df: pd.DataFrame, today: date) -> dict | None:
    """5-minute bars -> today's daily bar {date, open, high, low, close, volume, as_of}, or None."""
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d.columns = [str(c).strip().lower() for c in d.columns]
    if not {"open", "high", "low", "close", "volume"} <= set(d.columns):
        return None
    d = d[["open", "high", "low", "close", "volume"]].astype(float).dropna()
    idx = pd.DatetimeIndex(d.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    d.index = idx
    d = d[[t.date() == today for t in d.index]]
    d = d[(d[["open", "high", "low", "close"]] > 0).all(axis=1)]
    if d.empty:
        return None
    return {"date": today, "open": float(d["open"].iloc[0]), "high": float(d["high"].max()),
            "low": float(d["low"].min()), "close": float(d["close"].iloc[-1]),
            "volume": float(d["volume"].sum()), "as_of": d.index[-1].to_pydatetime().replace(tzinfo=None)}


def _yfinance_intraday(tickers: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf  # lazy: only needed when the screener actually polls
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), 20):     # small batches: gentler on the free service
        chunk = tickers[i:i + 20]
        raw = yf.download(tickers=chunk, period="1d", interval="5m", group_by="ticker",
                          progress=False, threads=True, auto_adjust=True)
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            level0 = set(raw.columns.get_level_values(0))
            for t in chunk:
                if t in level0:
                    out[t] = raw[t].dropna(how="all")
        else:
            out[chunk[0]] = raw.dropna(how="all")
    return out


class YahooLive:
    name = "yahoo"

    def __init__(self, suffix: str = ".NS", downloader: Callable[[list[str]], dict[str, pd.DataFrame]] | None = None) -> None:
        self.suffix = suffix
        self._download = downloader or _yfinance_intraday

    def ticker(self, symbol: str) -> str:
        return symbol if ("." in symbol or symbol.startswith("^")) else symbol + self.suffix

    def snapshot(self, symbols: list[str], today: date) -> dict[str, dict]:
        by_ticker = {self.ticker(s): s for s in symbols}
        frames = self._download(list(by_ticker))
        out: dict[str, dict] = {}
        for t, df in frames.items():
            bar = aggregate_today(df, today)
            if bar is not None and t in by_ticker:
                out[by_ticker[t]] = bar
        return out
