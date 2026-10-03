from __future__ import annotations

import argparse

from .backtest.engine import evaluate_fit
from .config import get_settings
from .data.csv_provider import CsvProvider
from .data.sources import NIFTY_LARGE_CAPS, get_source
from .data.synthetic import DemoProvider
from .db import make_engine, make_session_factory
from .services import ingest
from .strategies import registry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradelens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("strategies", help="list available strategies")
    bt = sub.add_parser("backtest", help="backtest one strategy on one symbol")
    bt.add_argument("--strategy", required=True)
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--capital", type=float, default=100_000.0)
    bt.add_argument("--demo", action="store_true", help="use synthetic data (plumbing only)")
    pf = sub.add_parser("portfolio", help="portfolio backtest with benchmark comparison and survivorship report")
    pf.add_argument("--strategies", help="comma-separated (default: all)")
    pf.add_argument("--symbols", help="comma-separated (default: everything stored, or the universe file's members)")
    pf.add_argument("--capital", type=float, default=100_000.0)
    pf.add_argument("--start")
    pf.add_argument("--end")
    pf.add_argument("--benchmark", default="NIFTYBEES")
    pf.add_argument("--universe-file", help="CSV with symbol,start,end membership history")
    pf.add_argument("--liquid-top-n", type=int)
    pf.add_argument("--risk-free", type=float, default=0.0, help="annual rate, e.g. 0.065")
    pf.add_argument("--demo", action="store_true", help="synthetic data (plumbing only)")
    sub.add_parser("worker", help="long-running scheduler: runs the daily job after market close on trading days")
    rd = sub.add_parser("run-daily", help="run the daily job once now (refresh data, exits, scan, alerts)")
    rd.add_argument("--no-fetch", action="store_true", help="skip the price refresh")
    sub.add_parser("test-alert", help="send a test alert to every configured channel")
    sub.add_parser("token", help="print a new random API token for API_TOKEN")
    sub.add_parser("migrate", help="create the database schema or add any missing columns (safe to repeat)")
    ft = sub.add_parser("fetch", help="download daily history from a free provider into the database")
    ft.add_argument("--symbols", help="comma-separated NSE symbols (default: a liquid large-cap list)")
    ft.add_argument("--years", type=float, default=5.0)
    ft.add_argument("--source", default="yahoo")
    sub.add_parser("import-csv", help="load SYMBOL.csv files from DATA_DIR into the database")
    ie = sub.add_parser("import-events", help="load an event calendar CSV (symbol,date,kind[,note]); symbol * = whole market")
    ie.add_argument("file")
    fe = sub.add_parser("fetch-events", help="best effort: pull upcoming earnings dates for stored stocks from Yahoo")
    fe.add_argument("--symbols", help="comma-separated (default: every stored stock)")
    mu = sub.add_parser("import-universe", help="validate a universe membership CSV (symbol,start,end per row, end blank = still a member)")
    mu.add_argument("filename", help="path to universe CSV inside DATA_DIR")
    a = ap.parse_args(argv)

    if a.cmd in ("import-events", "fetch-events"):
        from .data.db_provider import DbProvider
        from .services import events
        settings = get_settings()
        sf = make_session_factory(make_engine(settings.database_url))
        with sf() as s:
            if a.cmd == "import-events":
                with open(a.file, encoding="utf-8") as fh:
                    res = events.import_csv(s, fh.read())
                for problem in res["problems"]:
                    print("  skipped", problem)
                print(f"{res['imported']} event(s) imported")
                return 0 if res["imported"] or not res["problems"] else 1
            syms = [x.strip().upper() for x in a.symbols.split(",")] if a.symbols else DbProvider(sf).symbols()
            out = events.fetch_earnings(s, [x for x in syms if x != settings.benchmark_symbol])
        for r in out:
            print(f"  {r['symbol']:14s} " + (f"{r['events']} upcoming earnings date(s)" if r["ok"] else f"FAILED: {r['error']}"))
        print(f"{sum(r['ok'] for r in out)}/{len(out)} symbols looked up. Yahoo's calendar is patchy for NSE: no date does not mean no event.")
        return 0

    if a.cmd in ("fetch", "import-csv"):
        settings = get_settings()
        sf = make_session_factory(make_engine(settings.database_url))
        if a.cmd == "fetch":
            syms = [x for x in a.symbols.split(",")] if a.symbols else NIFTY_LARGE_CAPS
            results = ingest.fetch_symbols(sf, get_source(a.source), syms, a.years)
        else:
            results = ingest.import_csv_dir(sf, settings.data_dir)
        for r in results:
            print(f"  {r['symbol']:14s} " + (f"{r['bars']} bars" + (f" {r['start']} -> {r['end']}" if r.get("start") else "") if r["ok"] else f"FAILED: {r['error']}"))
        ok = sum(r["ok"] for r in results)
        print(f"{ok}/{len(results)} symbols stored")
        return 0 if ok else 1

    if a.cmd == "portfolio":
        return _portfolio(a)
    if a.cmd == "token":
        from .api.auth import new_token
        print(new_token())
        return 0
    if a.cmd == "migrate":
        # The schema is managed by db.make_session_factory (create_all + additive column upgrade), the same
        # code path the API and worker use, so this works on SQLite and PostgreSQL and is safe to repeat.
        settings = get_settings()
        make_session_factory(make_engine(settings.database_url))
        print("Database schema is up to date")
        return 0
    if a.cmd == "import-universe":
        # Universe membership is read straight from the CSV when needed (`portfolio --universe-file`), so this
        # command validates the file and reports what it contains instead of copying it anywhere.
        from pathlib import Path

        from .data.universe import IntervalUniverse
        settings = get_settings()
        filename = Path(settings.data_dir) / a.filename if not Path(a.filename).is_absolute() else Path(a.filename)
        if not filename.exists():
            print(f"universe file not found: {filename}")
            return 1
        uni = IntervalUniverse.from_csv(filename)
        n_rows = sum(len(v) for v in uni.intervals.values())
        print(f"{filename}: {len(uni.symbols)} symbols, {n_rows} membership rows - valid. "
              f"Use it with: python -m tradelens portfolio --universe-file {filename}")
        return 0
    if a.cmd in ("worker", "run-daily", "test-alert"):
        return _ops(a)

    if a.cmd == "strategies":
        for name, cls in sorted(registry.discover().items()):
            print(f"{name:20s} {cls.meta.description}")
        return 0

    settings = get_settings()
    if a.demo or settings.demo:
        provider = DemoProvider()
    elif settings.data_source == "csv":
        provider = CsvProvider(settings.data_dir)
    else:
        from .data.db_provider import DbProvider
        provider = DbProvider(make_session_factory(make_engine(settings.database_url)))
    df = provider.get_bars(a.symbol)
    fit = evaluate_fit(registry.get(a.strategy), df, a.symbol, capital=a.capital)
    fmt = lambda v: "n/a" if v is None else f"{v:,.2f}"  # noqa: E731
    print(f"{a.strategy} on {a.symbol}: {len(df)} bars {df.index[0].date()} -> {df.index[-1].date()}")
    for label in ("all", "early", "late"):
        m = fit[label]
        print(f"  {label:5s} trades={m['n_trades']:3d} win={fmt(m['win_rate'])} pf={fmt(m['profit_factor'])} "
              f"expectancy={fmt(m['expectancy'])} net={fmt(m['net_pnl'])} costs={fmt(m['total_costs'])}")
    print(f"  return={fmt(fit['all'].get('total_return_pct'))}%  max_dd={fmt(fit['all'].get('max_drawdown_pct'))}%")
    print(f"  verdict: {fit['verdict']}  (stability check, not proof of edge)")
    if fit["skipped"]:
        print(f"  skipped signals: {fit['skipped']}")
    return 0


def _portfolio(a) -> int:
    from pathlib import Path

    from .risk.engine import RiskEngine
    from .services import portfolio as svc

    settings = get_settings()
    if a.demo or settings.demo:
        provider = DemoProvider()
    elif settings.data_source == "csv":
        provider = CsvProvider(settings.data_dir)
    else:
        from .data.db_provider import DbProvider
        provider = DbProvider(make_session_factory(make_engine(settings.database_url)))
    split = lambda v: [x.strip() for x in v.split(",") if x.strip()] if v else None  # noqa: E731
    res, x = svc.run_portfolio(
        provider, strategy_names=split(a.strategies), symbols=split(a.symbols), capital=a.capital, start=a.start,
        end=a.end, benchmark=a.benchmark, universe_file=Path(a.universe_file) if a.universe_file else None,
        liquid_top_n=a.liquid_top_n, risk=RiskEngine(), risk_free=a.risk_free)
    f = lambda v, d=1: "n/a" if v is None else f"{v:,.{d}f}"  # noqa: E731
    m = res.metrics
    print(f"Portfolio {res.start.date()} -> {res.end.date()}  strategies={','.join(res.strategies)}  symbols={len(res.symbols)}")
    print(f"  trades={m['n_trades']} win={f(m['win_rate'] and m['win_rate'] * 100, 0)}% pf={f(m['profit_factor'], 2)} "
          f"costs={f(m['total_costs'], 0)} avg_exposure={f(m['avg_exposure_pct'], 0)}% max_open={m['max_open_positions']}")
    print(f"  {'':22s}{'return%':>9s}{'CAGR%':>8s}{'maxDD%':>8s}{'Sharpe':>8s}")
    rows = [("system", x["comparison"]["vs_benchmark"]["strategy"]),
            (f"buy&hold {x['comparison']['benchmark_symbol']}", x["comparison"]["vs_benchmark"]["benchmark"]),
            ("equal-weight universe", x["comparison"]["vs_equal_weight_universe"]["benchmark"])]
    for label, st in rows:
        print(f"  {label:22s}{f(st['total_return_pct']):>9s}{f(st['cagr_pct']):>8s}{f(st['max_drawdown_pct']):>8s}{f(st['sharpe'], 2):>8s}")
    for key, label in (("vs_benchmark", "vs index"), ("vs_equal_weight_universe", "vs equal-weight")):
        c = x["comparison"][key]
        print(f"  {label}: {c['verdict']}  excess CAGR={f(c['excess_cagr_pct'])}%  beta={f(c['beta'], 2)}  alpha={f(c['alpha_annual_pct'])}%/yr")
    sv = x["survivorship"]
    print(f"  survivorship bias risk: {sv['bias_risk'].upper()}  (universe: {sv['universe']})")
    for w in sv["warnings"]:
        print(f"    ! {w}")
    if res.skipped:
        print(f"  skipped signals: {res.skipped}")
    return 0


def _ops(a) -> int:
    import logging

    from .broker.paper import PaperBroker
    from .risk.engine import RiskEngine
    from .services.alerts import Alert, build_notifier
    from .services.jobs import run_daily, worker_loop

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if settings.broker != "paper":
        raise SystemExit("Only BROKER=paper is implemented.")
    sf = make_session_factory(make_engine(settings.database_url))
    notifier = build_notifier(settings, sf)
    if a.cmd == "test-alert":
        ok = notifier.send(Alert("info", "Test alert", "If you can read this, alerts work."))
        print("delivered to an external channel" if ok else "NOT delivered externally (check TELEGRAM_* / ALERT_WEBHOOK_URL); saved to the alerts table")
        return 0 if ok else 1
    if settings.demo:
        provider = DemoProvider()
    elif settings.data_source == "csv":
        provider = CsvProvider(settings.data_dir)
    else:
        from .data.db_provider import DbProvider
        provider = DbProvider(sf)
    source = None if (settings.demo or settings.data_source != "db" or getattr(a, "no_fetch", False)) else get_source("yahoo")
    risk, broker = RiskEngine(), PaperBroker()
    if a.cmd == "run-daily":
        res = run_daily(sf, settings, provider, broker, risk, notifier, source=source)
        print(f"daily run {res['run_id']}: {res['status']}")
        for k in ("fetch", "stale", "exits", "scan", "account", "warnings", "error"):
            if res.get(k):
                print(f"  {k}: {res[k]}")
        return 0 if res["status"] == "ok" else 1
    worker_loop(sf, settings, provider, broker, risk, notifier, source)
    return 0
