from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..data.base import DataProvider
from .base import Broker
from .paper import PaperBroker


def build_broker(settings: Settings, sf: sessionmaker[Session], provider: DataProvider) -> Broker:
    if settings.broker == "paper":
        # demo prices have no "next session" to wait for, so demo always fills at once
        model = "instant" if settings.demo else settings.paper_fill
        return PaperBroker(fill_model=model, bars=provider.get_bars if model == "next_open" else None)
    if settings.broker == "zerodha":
        from .zerodha import build_zerodha
        return build_zerodha(settings, sf)
    raise RuntimeError(f"unknown BROKER '{settings.broker}' (paper | zerodha)")
