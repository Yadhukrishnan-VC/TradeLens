from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: str
    demo: bool
    starting_capital: float
    mode: str
    broker: str
    data_source: str = "db"   # db (prices in the database) | csv (files in data_dir)


def get_settings() -> Settings:
    """Read settings from the environment at call time (so tests can override)."""
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///tradelite.db"),
        data_dir=os.getenv("DATA_DIR", "data"),
        demo=os.getenv("TRADELITE_DEMO", "0") == "1",
        starting_capital=float(os.getenv("STARTING_CAPITAL", "100000")),
        mode=os.getenv("TRADELITE_MODE", "semi_auto"),
        broker=os.getenv("BROKER", "paper"),
        data_source=os.getenv("DATA_SOURCE", "db"),
    )
