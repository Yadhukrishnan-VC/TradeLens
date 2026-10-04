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
    # --- secrets are excluded from repr so they never land in logs ---
    api_token: str = field(default="", repr=False)            # Bearer token for the API; empty = auth OFF (local dev only)
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    alert_webhook_url: str = field(default="", repr=False)
    healthcheck_ping_url: str = field(default="", repr=False)
    # Worker / daily job
    daily_run_at: str = "16:30"          # IST, after the close
    holidays_file: str = ""              # plain file in data_dir with YYYY-MM-DD per line
    refresh_years: float = 5.0               # full history is re-downloaded every run
    live_screener: bool = True          # poll free intraday prices during NSE hours
    live_interval: int = 300            # seconds between checks
    # Decision support
    gate_mode: str = "auto"              # off | advisory | enforce | auto (= enforce in auto mode, advisory otherwise)
    benchmark_symbol: str = "NIFTYBEES"  # stored symbol used to read the market regime
    enforce_regime: bool = False         # True: a strategy outside its usual regime is held back, not just flagged
    event_window_days: int = 2           # an event within this many days of a signal makes it risky
    validation_max_age_days: int = 45    # a "proven" verdict older than this counts as stale
    paper_fill: str = "instant"                               # instant | next_open (mirrors the backtest; the real default via env)
    # --- live broker (BROKER=zerodha). Real money: every default is the cautious one ---
    zerodha_api_key: str = field(default="", repr=False)
    zerodha_api_secret: str = field(default="", repr=False)
    zerodha_dry_run: bool = True                              # True = do everything except send the order
    live_max_order_value: float = 10_000.0                    # rupees per BUY order, enforced inside the broker class
    live_max_daily_value: float = 25_000.0                    # rupees of BUYs per day, from the broker's own order book
    live_limit_buffer_bps: float = 30.0                       # marketable-limit cushion around the reference price
    live_stop_buffer_bps: float = 150.0                       # GTT stop: limit this far below the trigger
    live_allow_auto: bool = False                             # permit TRADELENS_MODE=auto with a real broker
    # --- signal filters ---
    events_file: str = ""                                     # CSV symbol,date,kind (results, dividend, ...) for the blackout filter
    event_blackout_days: int = 3
    filters_enforced: bool = False                            # False = shadow: log verdicts, block nothing


def _env(name: str, default: str) -> str:
    """TRADELENS_<name>, falling back to the pre-rename TRADELITE_<name> so an old .env keeps working."""
    return os.getenv(f"TRADELENS_{name}", os.getenv(f"TRADELITE_{name}", default))


def get_settings() -> Settings:
    """Read settings from the environment at call time (so tests can override)."""
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///tradelens.db"),
        data_dir=os.getenv("DATA_DIR", "data"),
        demo=_env("DEMO", "0") == "1",
        starting_capital=float(os.getenv("STARTING_CAPITAL", "100000")),
        mode=_env("MODE", "semi_auto"),
        broker=os.getenv("BROKER", "paper"),
        data_source=os.getenv("DATA_SOURCE", "db"),
        api_token=os.getenv("API_TOKEN", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
        healthcheck_ping_url=os.getenv("HEALTHCHECK_PING_URL", ""),
        daily_run_at=os.getenv("DAILY_RUN_AT", "16:30"),
        holidays_file=os.getenv("HOLIDAYS_FILE", ""),
        refresh_years=float(os.getenv("REFRESH_YEARS", "5")),
        live_screener=os.getenv("LIVE_SCREENER", "1") == "1",
        live_interval=max(60, int(os.getenv("LIVE_INTERVAL_SECONDS", "300"))),
        gate_mode=os.getenv("GATE_MODE", "auto").lower(),
        benchmark_symbol=os.getenv("BENCHMARK_SYMBOL", "NIFTYBEES").upper(),
        enforce_regime=os.getenv("ENFORCE_REGIME", "0") == "1",
        event_window_days=max(0, int(os.getenv("EVENT_WINDOW_DAYS", "2"))),
        validation_max_age_days=max(1, int(os.getenv("VALIDATION_MAX_AGE_DAYS", "45"))),
        paper_fill=os.getenv("PAPER_FILL", "next_open"),
        zerodha_api_key=os.getenv("ZERODHA_API_KEY", ""),
        zerodha_api_secret=os.getenv("ZERODHA_API_SECRET", ""),
        zerodha_dry_run=os.getenv("ZERODHA_DRY_RUN", "1") != "0",
        live_max_order_value=float(os.getenv("LIVE_MAX_ORDER_VALUE", "10000")),
        live_max_daily_value=float(os.getenv("LIVE_MAX_DAILY_VALUE", "25000")),
        live_limit_buffer_bps=float(os.getenv("LIVE_LIMIT_BUFFER_BPS", "30")),
        live_stop_buffer_bps=float(os.getenv("LIVE_STOP_BUFFER_BPS", "150")),
        live_allow_auto=os.getenv("LIVE_ALLOW_AUTO", "0") == "1",
        events_file=os.getenv("EVENTS_FILE", ""),
        event_blackout_days=int(os.getenv("EVENT_BLACKOUT_DAYS", "3")),
        filters_enforced=os.getenv("FILTERS_ENFORCED", "0") == "1",
    )
