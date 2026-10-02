from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Single-account row. Equity is DERIVED (starting capital + realized P&L), never stored."""
    __tablename__ = "account"
    id: Mapped[int] = mapped_column(primary_key=True)
    starting_capital: Mapped[float] = mapped_column(Float)
    kill_switch: Mapped[bool] = mapped_column(default=False)


class SignalRow(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("strategy", "config_name", "symbol", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    config_name: Mapped[str] = mapped_column(String(64), default="default")
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    ts: Mapped[datetime] = mapped_column(DateTime)
    entry: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="new")  # signal|proposed|executed|rejected
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_qty: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[str] = mapped_column(String(10), default="unproven")   # proven|unproven
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)       # profit factor of the matching proven fit
    created_at: Mapped[datetime] = mapped_column(DateTime)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)   # fill price once filled
    status: Mapped[str] = mapped_column(String(20))  # PENDING_APPROVAL|FILLED|REJECTED|CANCELLED
    mode: Mapped[str] = mapped_column(String(16))
    tag: Mapped[str] = mapped_column(String(20), unique=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rank: Mapped[str] = mapped_column(String(10), default="unproven")
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    config_name: Mapped[str] = mapped_column(String(64), default="default")
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_costs: Mapped[float] = mapped_column(Float, default=0.0)
    signal_bar_ts: Mapped[datetime] = mapped_column(DateTime)   # exits are checked on bars AFTER this
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)     # net of all costs, once closed


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    config_name: Mapped[str] = mapped_column(String(64), default="default")
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    params: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class StrategyFitRow(Base):
    """The strategy <-> symbol map: what data range/volume a strategy was tested on and how it did."""
    __tablename__ = "strategy_fit"
    __table_args__ = (UniqueConstraint("strategy", "config_name", "symbol", "timeframe"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    config_name: Mapped[str] = mapped_column(String(64), default="default")
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    n_bars: Mapped[int] = mapped_column(Integer)
    avg_volume: Mapped[float] = mapped_column(Float)
    n_trades: Mapped[int] = mapped_column(Integer)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(20))   # candidate|no_edge|insufficient_data
    detail: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class PortfolioRunRow(Base):
    """One portfolio backtest: every strategy and symbol sharing a single account, plus the benchmark
    comparison, the survivorship report and the equity curves."""
    __tablename__ = "portfolio_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategies: Mapped[list] = mapped_column(JSON)
    symbols: Mapped[list] = mapped_column(JSON)
    capital: Mapped[float] = mapped_column(Float)
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    settings: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    by_strategy: Mapped[dict] = mapped_column(JSON)
    comparison: Mapped[dict] = mapped_column(JSON)
    survivorship: Mapped[dict] = mapped_column(JSON)
    skipped: Mapped[dict] = mapped_column(JSON)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    curves: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AlertRow(Base):
    """Every alert tradelite wanted to send a human, kept whether or not a channel accepted it.
    The message body is stored (not the destination), so no bot token or webhook URL is persisted."""
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    level: Mapped[str] = mapped_column(String(10))          # info | warning | critical
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(4000), default="")
    delivered: Mapped[bool] = mapped_column(default=False)  # did a real channel accept it?


class JobRunRow(Base):
    """History of the scheduled daily job: started/finished/status plus a JSON detail blob
    (fetch results, skipped stale symbols, exits, scan counts, warnings)."""
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")   # running | ok | failed
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class TrialsRow(Base):
    """Every strategy-variant tried on a symbol, for multiple-testing correction."""
    __tablename__ = "trials"
    __table_args__ = (UniqueConstraint("strategy", "config_name", "symbol", "params_hash", "run_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    config_name: Mapped[str] = mapped_column(String(64), default="default")
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    params_hash: Mapped[str] = mapped_column(String(64), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime)
    n_trades: Mapped[int] = mapped_column(Integer)
    expectancy: Mapped[float | None] = mapped_column(Float, nullable=True)


class StrategyConfigRow(Base):
    """A user-customised parameter set for a strategy ("preset"). The built-in defaults are the
    implicit config named 'default'; presets are tested, ranked and scanned separately."""
    __tablename__ = "strategy_configs"
    __table_args__ = (UniqueConstraint("strategy", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PriceBarRow(Base):
    """Daily OHLCV bars fetched from free providers, stored in the database."""
    __tablename__ = "price_bars"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), default="csv")


class LiveMatchRow(Base):
    """A strategy that matches a stock RIGHT NOW on today's in-progress bar. Provisional: the bar can
    still change until the close, so a match may 'fade'. Never creates orders by itself."""
    __tablename__ = "live_matches"
    __table_args__ = (UniqueConstraint("strategy", "config_name", "symbol", "trading_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    config_name: Mapped[str] = mapped_column(String(64), default="default")
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trading_day: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(10), default="live")     # live | faded
    side: Mapped[str] = mapped_column(String(4))
    entry: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[str] = mapped_column(String(10), default="unproven")
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_qty: Mapped[int] = mapped_column(Integer, default=0)
    fit: Mapped[str] = mapped_column(String(32), default="OK")          # risk-engine verdict for YOUR capital
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
