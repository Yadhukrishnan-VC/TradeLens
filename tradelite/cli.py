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
    ap = argparse.ArgumentParser(prog="tradelite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("strategies", help="list available strategies")
    bt = sub.add_parser("backtest", help="backtest one strategy on one symbol")
    bt.add_argument("--strategy", required=True)
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--capital", type=float, default=100_000.0)
    bt.add_argument("--demo", action="store_true", help="use synthetic data (plumbing only)")
    ft = sub.add_parser("fetch", help="download daily history from a free provider into the database")
    ft.add_argument("--symbols", help="comma-separated NSE symbols (default: a liquid large-cap list)")
    ft.add_argument("--years", type=float, default=5.0)
    ft.add_argument("--source", default="yahoo")
    sub.add_parser("import-csv", help="load SYMBOL.csv files from DATA_DIR into the database")
    a = ap.parse_args(argv)

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
