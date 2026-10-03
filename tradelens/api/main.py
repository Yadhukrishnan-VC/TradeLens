from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..broker.base import Broker
from ..broker.paper import PaperBroker
from ..config import Settings, get_settings
from ..data.base import DataProvider, DataQualityError
from ..data.csv_provider import CsvProvider
from ..data.db_provider import DbProvider
from ..data.live import LiveSource, YahooLive
from ..data.sources import NIFTY_LARGE_CAPS, get_source
from ..data.synthetic import DemoProvider
from ..db import make_engine, make_session_factory
from ..domain import Mode
from ..models import (AlertRow, BacktestRunRow, EventRow, JobRunRow, LiveMatchRow, OrderRow, PortfolioRunRow,
                      PositionAlertRow, PositionRow, SignalRow, StrategyConfigRow, StrategyFitRow,
                      StrategyLifecycleRow, WatchRow)
from ..risk.engine import RiskEngine
from ..services import backtests, context as context_svc, events as events_svc, ingest, lifecycle, portfolio as portfolio_svc, presets
from ..services.alerts import Alert, build_notifier
from ..services.jobs import build_pipeline
from ..services.screener import LiveScreener, ist_now
from ..services.gate import GateConfig
from ..services.pipeline import Pipeline
from ..strategies import registry
from .auth import install_auth


class BacktestRequest(BaseModel):
    strategy: str
    symbol: str
    timeframe: str = "1d"
    capital: float = Field(100_000.0, gt=0)
    params: dict[str, Any] = {}
    config_name: str = "default"


class PortfolioRequest(BaseModel):
    strategies: list[str] | None = None        # default: every registered strategy
    symbols: list[str] | None = None           # default: everything stored (or the universe file's members)
    capital: float = Field(100_000.0, gt=0)
    start: str | None = None                   # YYYY-MM-DD; indicators still warm up on earlier data
    end: str | None = None
    benchmark: str = portfolio_svc.DEFAULT_BENCHMARK
    universe_file: str | None = None           # plain file name inside DATA_DIR: symbol,start,end (membership history)
    liquid_top_n: int | None = Field(None, ge=1)   # only trade the N most liquid symbols each day (prior data only)
    slippage_bps: float = Field(5.0, ge=0)
    risk_free: float = Field(0.0, ge=0, le=0.5)    # annual rate used for Sharpe / alpha


class PresetRequest(BaseModel):
    strategy: str
    name: str
    params: dict[str, Any]


class FetchRequest(BaseModel):
    symbols: list[str] | None = None      # default: a liquid NSE large-cap list
    years: float = Field(5.0, gt=0, le=25)
    source: str = "yahoo"


class BatchRequest(BaseModel):
    strategies: list[str] | None = None
    symbols: list[str] | None = None
    rr: float | None = Field(None, gt=0, le=20)   # e.g. 5 -> tests every strategy with a 1:5 target (preset "rr5")


class LifecycleRequest(BaseModel):
    strategy: str
    config_name: str = "default"
    state: str
    note: str = ""


class EventRequest(BaseModel):
    symbol: str = "*"                 # '*' = the whole market
    day: date
    kind: str = Field("event", max_length=32)
    note: str = Field("", max_length=200)


class EventCsv(BaseModel):
    csv: str = Field(max_length=200_000)


class ScanRequest(BaseModel):
    symbols: list[str] | None = None
    strategies: list[str] | None = None
    timeframe: str = "1d"
    mode: Mode | None = None
    require_fit: bool = False
    use_presets: bool = True


class KillSwitchRequest(BaseModel):
    active: bool


def _row(r) -> dict:
    return {c.name: getattr(r, c.name) for c in r.__table__.columns}


def _proven_first(rows: list) -> list:
    """Stable sort of an already newest-first list: proven signals/orders on top, best profit factor first."""
    return sorted(rows, key=lambda r: (r.rank != "proven", -(r.rank_score or 0.0)))


class WalkForwardRequest(BaseModel):
    strategy: str
    symbol: str
    param_grid: list[dict[str, Any]] | None = None
    train_years: float = Field(3.0, gt=0, le=20)
    test_years: float = Field(1.0, gt=0, le=10)
    step_years: float = Field(1.0, gt=0, le=10)
    embargo_years: float = Field(0.0, ge=0, le=2)


def create_app(
    settings: Settings | None = None,
    *,
    provider: DataProvider | None = None,
    broker: Broker | None = None,
    session_factory=None,
    risk: RiskEngine | None = None,
    frontend_dist: Path | None = None,
    live_source: LiveSource | None = None,
    live_clock=None,
    notifier=None,
    start_live: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.broker != "paper":
        raise RuntimeError("Only BROKER=paper is implemented. Live Zerodha is deliberately not built yet.")
    sf = session_factory or make_session_factory(make_engine(settings.database_url))
    if provider is None:
        if settings.demo:
            provider = DemoProvider()
        elif settings.data_source == "csv":
            provider = CsvProvider(settings.data_dir)
        else:
            provider = DbProvider(sf)
    broker = broker or PaperBroker()
    risk = risk or RiskEngine()

    if live_source is None and not settings.demo:
        live_source = YahooLive()
    if notifier is None:
        # Alerts go to the log plus any configured channel, and are always stored so GET /alerts works.
        notifier = build_notifier(settings, sf)

    def _account_state(sess):
        return Pipeline(sess, provider, broker, risk=risk, starting_capital=settings.starting_capital).account_state()

    screener = LiveScreener(sf, provider, live_source, risk, _account_state, interval=settings.live_interval,
                            notifier=notifier, clock=live_clock or ist_now, enabled=settings.live_screener,
                            benchmark=settings.benchmark_symbol, event_window_days=settings.event_window_days,
                            telegram=bool(settings.telegram_bot_token and settings.telegram_chat_id))

    @asynccontextmanager
    async def lifespan(_app):
        if start_live:
            screener.start()      # polls only during NSE hours; a no-op in demo mode
        yield
        screener.stop()

    app = FastAPI(title="tradelens", version="0.2.0", lifespan=lifespan)
    # Auth BEFORE CORS: CORS is added last, so it stays the outermost layer and answers preflight without a token.
    auth_on = install_auth(app, settings.api_token)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"],
                       allow_methods=["*"], allow_headers=["*"])

    def session_dep():
        with sf() as s:
            yield s

    def pipe(s, mode: Mode | None = None, require_fit: bool = False) -> Pipeline:
        return build_pipeline(s, settings, provider, broker, risk, notifier, mode, require_fit)

    @app.exception_handler(FileNotFoundError)
    async def _nf(_, exc):  # noqa: ANN001
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(DataQualityError)
    async def _dq(_, exc):  # noqa: ANN001
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": f"data quality: {exc}"}, status_code=422)

    @app.get("/health")
    def health():
        with sf() as s:
            dialect = s.get_bind().dialect.name
        return {"status": "ok", "mode": settings.mode, "broker": settings.broker, "demo": settings.demo,
                "database": dialect, "data_source": "demo" if settings.demo else settings.data_source,
                "auth_required": auth_on}

    @app.get("/symbols")
    def symbols():
        return provider.symbols()

    @app.get("/risk-config")
    def risk_config():
        return asdict(risk.cfg)

    @app.get("/strategies")
    def strategies():
        return [
            {**{k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(cls.meta).items()},
             "default_params": cls.default_params}
            for cls in registry.discover().values()
        ]

    @app.post("/backtests")
    def run_backtest(req: BacktestRequest, s=Depends(session_dep)):
        try:
            return backtests.run_and_store(s, provider, req.strategy, req.symbol, timeframe=req.timeframe,
                                           capital=req.capital, params=req.params, risk=risk,
                                           config_name=req.config_name)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/backtests")
    def list_backtests(s=Depends(session_dep)):
        rows = s.scalars(select(BacktestRunRow).order_by(BacktestRunRow.id.desc()).limit(100)).all()
        return [{k: v for k, v in _row(r).items() if k != "trades"} for r in rows]

    @app.get("/backtests/{run_id}")
    def get_backtest(run_id: int, s=Depends(session_dep)):
        r = s.get(BacktestRunRow, run_id)
        if r is None:
            raise HTTPException(404, "not found")
        return _row(r)

    @app.post("/portfolio-backtests")
    def run_portfolio_backtest(req: PortfolioRequest, s=Depends(session_dep)):
        try:
            ufile = portfolio_svc.resolve_universe_file(settings.data_dir, req.universe_file) if req.universe_file else None
            return portfolio_svc.run_and_store(
                s, provider, strategy_names=req.strategies, symbols=req.symbols, capital=req.capital,
                start=req.start, end=req.end, benchmark=req.benchmark, universe_file=ufile,
                liquid_top_n=req.liquid_top_n, risk=risk, slippage_bps=req.slippage_bps, risk_free=req.risk_free)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/portfolio-backtests")
    def list_portfolio_backtests(s=Depends(session_dep)):
        rows = s.scalars(select(PortfolioRunRow).order_by(PortfolioRunRow.id.desc()).limit(50)).all()
        return [{k: v for k, v in _row(r).items() if k not in ("trades", "curves")} for r in rows]

    @app.get("/portfolio-backtests/{run_id}")
    def get_portfolio_backtest(run_id: int, s=Depends(session_dep)):
        r = s.get(PortfolioRunRow, run_id)
        if r is None:
            raise HTTPException(404, "not found")
        return _row(r)

    @app.get("/strategy-configs")
    def list_presets(strategy: str | None = None, s=Depends(session_dep)):
        q = select(StrategyConfigRow).order_by(StrategyConfigRow.strategy, StrategyConfigRow.name)
        if strategy:
            q = q.where(StrategyConfigRow.strategy == strategy)
        return [_row(r) for r in s.scalars(q).all()]

    @app.post("/strategy-configs")
    def save_preset(req: PresetRequest, s=Depends(session_dep)):
        try:
            return _row(presets.save(s, req.strategy, req.name.strip(), req.params))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e).strip("'\"")) from e

    @app.delete("/strategy-configs/{preset_id}")
    def delete_preset(preset_id: int, s=Depends(session_dep)):
        row = s.get(StrategyConfigRow, preset_id)
        if row is None:
            raise HTTPException(404, "not found")
        s.delete(row)
        s.commit()
        return {"deleted": preset_id}

    @app.get("/track-record")
    def track_record(s=Depends(session_dep)):
        """Stock x strategy pairs that passed the stability check: what worked, on which stock, over
        which dates. Best profit factor first. Everything not proven is listed separately."""
        def item(f: StrategyFitRow) -> dict:
            d = f.detail or {}
            return {"id": f.id, "symbol": f.symbol, "strategy": f.strategy, "config_name": f.config_name,
                    "verdict": f.verdict, "tested_from": f.start, "tested_to": f.end, "n_bars": f.n_bars,
                    "avg_volume": f.avg_volume, "n_trades": f.n_trades, "profit_factor": f.profit_factor,
                    "expectancy": f.expectancy, "first_win": d.get("first_win"), "last_win": d.get("last_win"),
                    "early_pf": (d.get("early") or {}).get("profit_factor"),
                    "late_pf": (d.get("late") or {}).get("profit_factor"), "verified_at": f.updated_at}
        rows = [item(f) for f in s.scalars(select(StrategyFitRow)).all()]
        proven = sorted((r for r in rows if r["verdict"] == "candidate"),
                        key=lambda r: -(r["profit_factor"] if r["profit_factor"] is not None else 99.0))
        return {"proven": proven, "not_proven": [r for r in rows if r["verdict"] != "candidate"]}

    @app.get("/data/coverage")
    def data_coverage(s=Depends(session_dep)):
        return ingest.coverage(s)

    @app.post("/data/fetch")
    def data_fetch(req: FetchRequest):
        """Download daily history from a free provider into the database. Synchronous: ~1-2s per symbol."""
        try:
            src = get_source(req.source)
        except KeyError as e:
            raise HTTPException(400, str(e).strip("'\"")) from e
        return ingest.fetch_symbols(sf, src, req.symbols or NIFTY_LARGE_CAPS, req.years)

    @app.post("/backtests/batch")
    def backtest_batch(req: BatchRequest, s=Depends(session_dep)):
        """Backtest strategies on stocks in one go to build the track record. With `rr`, first saves an
        'rr<N>' preset per strategy (same settings, target = N x risk) and tests that. Can take minutes."""
        catalog = registry.discover()
        names = req.strategies or sorted(catalog)
        unknown = [n for n in names if n not in catalog]
        if unknown:
            raise HTTPException(400, f"unknown strategies: {unknown}")
        config = f"rr{req.rr:g}" if req.rr else "default"
        ran = skipped = proven = 0
        problems: list[str] = []
        for name in names:
            if req.rr:
                presets.save(s, name, config, {"rr": req.rr})
            for sym in req.symbols or provider.symbols():
                try:
                    out = backtests.run_and_store(s, provider, name, sym, risk=risk, config_name=config)
                    ran += 1
                    proven += out["verdict"] == "candidate"
                except (KeyError, ValueError, FileNotFoundError, DataQualityError) as e:
                    skipped += 1
                    if len(problems) < 5:
                        problems.append(f"{name} on {sym}: {str(e)[:100]}")
        return {"config_name": config, "ran": ran, "skipped": skipped, "proven": proven, "problems": problems}

    @app.get("/screener")
    def screener_view(s=Depends(session_dep)):
        """Today's live matches grouped by strategy (every strategy is listed, matched or not)."""
        today = screener.clock().date()
        rows = s.scalars(select(LiveMatchRow).where(LiveMatchRow.trading_day == today)).all()
        confirmed = {(r.strategy, r.config_name, r.symbol) for r in s.scalars(
            select(SignalRow).where(SignalRow.ts >= datetime.combine(today, datetime.min.time()))).all()}
        by_strategy: dict[str, list[dict]] = {}
        for r in sorted(rows, key=lambda r: (r.status != "live", r.rank != "proven", -r.rank_score, r.first_seen)):
            risk_ps = abs(r.entry - r.stop)
            by_strategy.setdefault(r.strategy, []).append({
                **_row(r), "risk_per_share": risk_ps,
                "confirmed": (r.strategy, r.config_name, r.symbol) in confirmed})
        return {"status": screener.status(), "strategies": [
            {"name": name, "description": cls.meta.description, "matches": by_strategy.get(name, [])}
            for name, cls in sorted(registry.discover().items())]}

    @app.get("/screener/status")
    def screener_status():
        return screener.status()

    @app.post("/screener/run")
    def screener_run(force: bool = False):
        """Check now. Outside market hours this does nothing unless force=true (useful for testing)."""
        return screener.run_once(force=force)

    # ---------- decision support: context, watchlist, advice, lifecycle, events ----------
    @app.get("/context")
    def market_context(s=Depends(session_dep)):
        """Market regime + account + risk state in one place, plus how the gate is set."""
        p = pipe(s)
        regime = p.regime_now()
        return {"as_of": screener.clock().date().isoformat(), "regime": context_svc.regime_dict(regime),
                "portfolio": context_svc.portfolio_snapshot(s, p.account_state(), settings.starting_capital, risk.cfg),
                "gate": {"mode": GateConfig.from_settings(settings, Mode(settings.mode)).mode,
                         "enforce_regime": settings.enforce_regime, "event_window_days": settings.event_window_days,
                         "validation_max_age_days": settings.validation_max_age_days}}

    @app.get("/watchlist")
    def watchlist(s=Depends(session_dep)):
        """Setups one step from triggering, from the latest scan: proven ones first, then the closest."""
        latest = s.scalar(select(func.max(WatchRow.bar_ts)))
        if latest is None:
            return {"as_of": None, "items": []}
        p = pipe(s)
        items = []
        for w in s.scalars(select(WatchRow).where(WatchRow.bar_ts == latest)).all():
            rank, score, _adjusted, _reason = p.rank_for(w.strategy, w.config_name, w.symbol)
            items.append({**_row(w), "rank": rank, "rank_score": score})
        items.sort(key=lambda d: (d["rank"] != "proven", abs(d["distance_pct"]) if d["distance_pct"] is not None else 99.0))
        return {"as_of": latest.date().isoformat(), "items": items[:60]}

    @app.get("/position-alerts")
    def position_alerts(open_only: bool = True, s=Depends(session_dep)):
        q = select(PositionAlertRow).order_by(PositionAlertRow.id.desc()).limit(100)
        if open_only:
            q = q.join(PositionRow, PositionRow.id == PositionAlertRow.position_id).where(PositionRow.closed_at.is_(None))
        return [_row(a) for a in s.scalars(q).all()]

    @app.post("/position-alerts/review")
    def review_positions(s=Depends(session_dep)):
        return [_row(a) for a in pipe(s).review_positions()]

    @app.post("/position-alerts/{alert_id}/ack")
    def ack_position_alert(alert_id: int, s=Depends(session_dep)):
        row = s.get(PositionAlertRow, alert_id)
        if row is None:
            raise HTTPException(404, "not found")
        row.acknowledged = True
        s.commit()
        return _row(row)

    @app.get("/lifecycle")
    def lifecycle_list(s=Depends(session_dep)):
        """Every strategy and saved preset with its state and how much evidence it has."""
        states = lifecycle.states_for(s)
        notes = {(r.strategy, r.config_name): r for r in s.scalars(select(StrategyLifecycleRow)).all()}
        fits = s.scalars(select(StrategyFitRow)).all()
        out = []
        for name, cls in sorted(registry.discover().items()):
            configs = ["default"] + [r.name for r in presets.for_strategies(s, [name])[name]]
            for cfg in configs:
                mine = [f for f in fits if f.strategy == name and f.config_name == cfg]
                out.append({"strategy": name, "config_name": cfg, "state": states.get((name, cfg), "active"),
                            "note": notes[(name, cfg)].note if (name, cfg) in notes else "",
                            "tested": len(mine), "proven": sum(f.verdict == "candidate" for f in mine),
                            "regimes": list(cls.meta.regimes)})
        return out

    @app.post("/lifecycle")
    def lifecycle_set(req: LifecycleRequest, s=Depends(session_dep)):
        if req.strategy not in registry.discover():
            raise HTTPException(400, f"unknown strategy '{req.strategy}'")
        if req.config_name != "default" and not s.scalar(select(StrategyConfigRow.id).where(
                StrategyConfigRow.strategy == req.strategy, StrategyConfigRow.name == req.config_name)):
            raise HTTPException(400, f"no preset '{req.config_name}' for {req.strategy}")
        try:
            row = lifecycle.set_state(s, req.strategy, req.config_name, req.state, req.note)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return _row(row)

    @app.get("/events")
    def events_list(days: int = 90, s=Depends(session_dep)):
        today = screener.clock().date()
        q = select(EventRow).where(EventRow.day >= today, EventRow.day <= today + timedelta(days=days)).order_by(EventRow.day, EventRow.symbol)
        return [_row(e) for e in s.scalars(q).all()]

    @app.post("/events")
    def events_add(req: EventRequest, s=Depends(session_dep)):
        return _row(events_svc.add_event(s, req.symbol, req.day, req.kind, req.note))

    @app.post("/events/import")
    def events_import(req: EventCsv, s=Depends(session_dep)):
        return events_svc.import_csv(s, req.csv)

    @app.delete("/events/{event_id}")
    def events_delete(event_id: int, s=Depends(session_dep)):
        row = s.get(EventRow, event_id)
        if row is None:
            raise HTTPException(404, "not found")
        s.delete(row)
        s.commit()
        return {"deleted": event_id}

    @app.get("/fits")
    def fits(s=Depends(session_dep)):
        return [_row(r) for r in s.scalars(select(StrategyFitRow).order_by(StrategyFitRow.strategy,
                                                                          StrategyFitRow.symbol)).all()]

    @app.post("/scan")
    def scan(req: ScanRequest, s=Depends(session_dep)):
        try:
            rows = pipe(s, req.mode, req.require_fit).scan(req.symbols, req.strategies, req.timeframe, req.use_presets)
        except KeyError as e:
            raise HTTPException(400, f"unknown strategy or symbol: {e}") from e
        return [_row(r) for r in _proven_first(rows)]

    @app.post("/walkforward")
    def walkforward(req: WalkForwardRequest, s=Depends(session_dep)):
        """Pick settings on a training slice, judge them on the next unseen slice, over rolling windows.
        Every setting tried is counted in the trial registry."""
        from ..backtest.walkforward import walkforward_validate
        try:
            return walkforward_validate(
                s, provider, req.strategy.strip(), req.symbol.strip().upper(), param_grid=req.param_grid,
                train_years=req.train_years, test_years=req.test_years, step_years=req.step_years,
                embargo_years=req.embargo_years)
        except KeyError as e:
            raise HTTPException(404, f"unknown strategy or symbol: {e}") from e
        except (ValueError, DataQualityError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/signals")
    def signals(s=Depends(session_dep)):
        rows = s.scalars(select(SignalRow).order_by(SignalRow.id.desc()).limit(200)).all()
        return [_row(r) for r in _proven_first(list(rows))]

    @app.get("/orders")
    def orders(status: str | None = None, s=Depends(session_dep)):
        q = select(OrderRow).order_by(OrderRow.id.desc()).limit(200)
        if status:
            q = q.where(OrderRow.status == status)
        return [_row(r) for r in _proven_first(list(s.scalars(q).all()))]

    @app.post("/orders/{order_id}/approve")
    def approve(order_id: int, s=Depends(session_dep)):
        try:
            return _row(pipe(s).approve(order_id))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/orders/{order_id}/reject")
    def reject(order_id: int, s=Depends(session_dep)):
        try:
            return _row(pipe(s).reject(order_id))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/exits/check")
    def check_exits(s=Depends(session_dep)):
        return [_row(p) for p in pipe(s).check_exits()]

    @app.get("/positions")
    def positions(open: bool | None = None, s=Depends(session_dep)):  # noqa: A002
        q = select(PositionRow).order_by(PositionRow.id.desc())
        if open is True:
            q = q.where(PositionRow.closed_at.is_(None))
        elif open is False:
            q = q.where(PositionRow.closed_at.is_not(None))
        return [_row(r) for r in s.scalars(q).all()]

    @app.get("/account")
    def account(s=Depends(session_dep)):
        p = pipe(s)
        st = p.account_state()
        s.commit()
        return {**vars(st), "open_symbols": sorted(st.open_symbols),
                "starting_capital": p.account().starting_capital}

    @app.post("/kill-switch")
    def kill_switch(req: KillSwitchRequest, s=Depends(session_dep)):
        pipe(s).set_kill_switch(req.active)
        notifier.send(Alert("warning", "Kill switch " + ("ON: no new trades" if req.active else "OFF: trading allowed again")))
        return {"kill_switch": req.active}

    @app.get("/alerts")
    def alerts(limit: int = 100, s=Depends(session_dep)):
        rows = s.scalars(select(AlertRow).order_by(AlertRow.id.desc()).limit(max(1, min(limit, 500)))).all()
        return [_row(r) for r in rows]

    @app.get("/jobs")
    def jobs(limit: int = 30, s=Depends(session_dep)):
        rows = s.scalars(select(JobRunRow).order_by(JobRunRow.id.desc()).limit(max(1, min(limit, 200)))).all()
        return [_row(r) for r in rows]

    dist = frontend_dist or Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():   # `npm run build` in frontend/ -> the dashboard is served at /ui/ by this same process
        app.mount("/ui", StaticFiles(directory=dist, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/ui/")

    return app
