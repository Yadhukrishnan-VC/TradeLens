from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from tradelite.broker.paper import PaperBroker
from tradelite.data.synthetic import DemoProvider
from tradelite.db import make_engine, make_session_factory
from tradelite.models import Base
from tradelite.domain import Mode, Side, Signal
from tradelite.services.pipeline import Pipeline


def make_df(rows: list[tuple[float, float, float, float]], start: str = "2020-01-01", volume: float = 1e6) -> pd.DataFrame:
    """rows: (open, high, low, close)"""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(
        {"open": [r[0] for r in rows], "high": [r[1] for r in rows], "low": [r[2] for r in rows],
         "close": [r[3] for r in rows], "volume": [volume] * len(rows)}, index=idx)


def fresh_engine():
    """In-memory SQLite by default; set TEST_DATABASE_URL=postgresql://... to run the whole suite on Postgres."""
    url = os.getenv("TEST_DATABASE_URL", "sqlite://")
    engine = make_engine(url)
    if not url.startswith("sqlite"):
        Base.metadata.drop_all(engine)   # every test starts from an empty schema
    return engine


@pytest.fixture
def session():
    sf = make_session_factory(fresh_engine())
    with sf() as s:
        yield s


class FixedProvider:
    """Serves exactly the frames it was given."""
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def symbols(self) -> list[str]:
        return sorted(self.frames)

    def get_bars(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame:
        return self.frames[symbol]


@pytest.fixture
def pipeline_factory(session):
    def build(mode: Mode = Mode.SEMI_AUTO, provider=None, capital: float = 100_000.0, **kw) -> Pipeline:
        return Pipeline(session, provider or DemoProvider(), PaperBroker(), mode=mode,
                        starting_capital=capital, **kw)
    return build


def buy_signal(entry=100.0, stop=95.0, target=110.0, symbol="X") -> Signal:
    return Signal("test", symbol, Side.BUY, pd.Timestamp("2020-01-01").to_pydatetime(), entry, stop, target)


@pytest.fixture(autouse=True)
def _fresh_breakers():
    """Circuit breakers are shared per outside service; every test starts with them closed."""
    from tradelite.services import breaker
    breaker.reset_all()
    yield
    breaker.reset_all()
