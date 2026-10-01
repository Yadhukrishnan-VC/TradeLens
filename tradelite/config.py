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
    live_screener: bool = True          # poll free intraday prices during NSE hours
    live_interval: int = 300            # seconds between checks
    telegram_token: str = ""            # optional free alerts via a Telegram bot
    telegram_chat_id: str = ""


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
        live_screener=os.getenv("LIVE_SCREENER", "1") == "1",
        live_interval=max(60, int(os.getenv("LIVE_INTERVAL_SECONDS", "300"))),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
