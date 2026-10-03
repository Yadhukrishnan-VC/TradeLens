"""Regression tests for defects introduced by branch merges / the tradelite -> tradelens rename."""
from __future__ import annotations

import ast
from datetime import datetime
import re
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FixedProvider, fresh_engine
from fastapi.testclient import TestClient

from tradelens.api.main import create_app
from tradelens.backtest.engine import run_backtest
from tradelens.backtest.walkforward import _window_bounds, params_hash, walkforward_validate
from tradelens.config import Settings, get_settings
from tradelens.data.synthetic import DemoProvider, make_bars
from tradelens.db import make_session_factory
from tradelens.models import StrategyFitRow, TrialsRow
from tradelens.services import ranking
from tradelens.strategies import registry

ROOT = Path(__file__).resolve().parent.parent


def _fit(session, verdict, pf=1.5, symbol="AAA", cfg="default"):
    session.add(StrategyFitRow(strategy="macd_cross", config_name=cfg, symbol=symbol, timeframe="1d", n_bars=1000,
                               avg_volume=1e5, n_trades=40, profit_factor=pf, expectancy=1.0, verdict=verdict,
                               detail={}, start=datetime(2018, 1, 1), end=datetime(2023, 1, 1),
                               updated_at=datetime(2023, 1, 2)))
    session.commit()


# ---- ranking: one rank_for, only two rank values (columns are String(10)) ----
@pytest.mark.parametrize("verdict", ["no_edge", "insufficient_data"])
def test_rank_is_only_proven_or_unproven(session, verdict):
    _fit(session, verdict)
    rank, score, adjusted, reason = ranking.rank_for(session, "macd_cross", "default", "AAA")
    assert rank == "unproven" and score == 0.0 and adjusted is False


def test_rank_proven_only_for_candidate(session):
    _fit(session, "candidate", pf=1.8)
    assert ranking.rank_for(session, "macd_cross", "default", "AAA")[:2] == ("proven", 1.8)
    assert ranking.rank_for(session, "macd_cross", "default", "ZZZ")[0] == "unproven"


def test_rank_flags_multiple_settings_tried(session):
    _fit(session, "candidate")
    for i in range(3):
        session.add(TrialsRow(strategy="macd_cross", symbol="AAA", params_hash=f"h{i}", run_at=datetime(2024, 1, 1 + i),
                              n_trades=1))
    session.commit()
    _, _, adjusted, reason = ranking.rank_for(session, "macd_cross", "default", "AAA")
    assert adjusted and "3 settings" in reason


# ---- backtest engine: liquidity-aware slippage path used to crash (no `math`, Signal has no qty) ----
def test_backtest_with_liquidity_impact_runs_and_costs_more():
    df = make_bars("DEMO2")
    base = run_backtest(registry.get("donchian_breakout"), df, "DEMO2")
    impact = run_backtest(registry.get("donchian_breakout"), df, "DEMO2", adv20_value=2_000_000.0, impact_k=50.0)
    assert base.trades and impact.trades
    assert impact.metrics["net_pnl"] <= base.metrics["net_pnl"]


# ---- walk-forward: was a non-functional stub ----
def test_window_bounds_roll_by_step_and_respect_embargo():
    import pandas as pd
    w = _window_bounds(pd.Timestamp("2018-01-01"), pd.Timestamp("2024-01-01"), 3, 1, 1, 0.25)
    assert len(w) >= 2
    assert w[1]["train_start"] - w[0]["train_start"] == pd.Timedelta(days=365)
    assert (w[0]["test_start"] - w[0]["train_end"]).days > 0
    assert all(x["test_end"] <= pd.Timestamp("2024-01-01") for x in w)


def test_params_hash_distinguishes_values():
    assert params_hash({"fast": 8}) != params_hash({"fast": 9})
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})


def test_walkforward_runs_and_counts_every_setting(session):
    prov = FixedProvider({"DEMO2": make_bars("DEMO2", n=1800)})
    grid = [{"fast": 8, "slow": 21, "signal": 5}, {"fast": 12, "slow": 26, "signal": 9}]
    out = walkforward_validate(session, prov, "macd_cross", "DEMO2", param_grid=grid, train_years=2, test_years=1)
    assert out["n_windows"] >= 1 and out["settings_tried"] == 2
    assert any(w["selected_params"] in grid for w in out["windows"])
    assert session.query(TrialsRow).filter_by(strategy="macd_cross", symbol="DEMO2").count() == 2


def test_walkforward_endpoint():
    settings = Settings("sqlite://", "data", True, 100_000.0, "semi_auto", "paper")
    c = TestClient(create_app(settings, provider=DemoProvider(), session_factory=make_session_factory(fresh_engine())))
    r = c.post("/walkforward", json={"strategy": "ema_cross", "symbol": "DEMO1", "train_years": 2, "test_years": 1})
    assert r.status_code == 200, r.text
    assert r.json()["n_windows"] >= 1
    assert c.post("/walkforward", json={"strategy": "nope", "symbol": "DEMO1"}).status_code == 404
    assert c.post("/holdout/evaluate").status_code in (404, 405)       # the fake endpoint is gone


# ---- config: old TRADELITE_* names keep working after the rename ----
def test_legacy_env_names_still_read(monkeypatch):
    for k in ("TRADELENS_MODE", "TRADELENS_DEMO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRADELITE_MODE", "signal_only")
    monkeypatch.setenv("TRADELITE_DEMO", "1")
    s = get_settings()
    assert s.mode == "signal_only" and s.demo is True
    monkeypatch.setenv("TRADELENS_MODE", "auto")                     # the new name wins
    assert get_settings().mode == "auto"


def test_no_old_project_name_left_in_tracked_text():
    bad = re.compile(r"tradelite|tradevision|tradelence", re.I)
    skip = {".git", "node_modules", "dist", "__pycache__", ".pytest_cache", "tradelens.egg-info"}
    hits = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not (skip & set(p.relative_to(ROOT).parts)) and p.suffix in {".py", ".md", ".yml", ".toml", ".tsx", ".ts", ".json", ".example", ".txt", ".ini"}:
            if p.name in ("test_regressions.py", "config.py"):        # config keeps the legacy fallback on purpose
                continue
            if bad.search(p.read_text(errors="ignore")):
                hits.append(str(p.relative_to(ROOT)))
    assert not hits, hits


# ---- packaging / deployment ----
def test_every_third_party_import_is_a_declared_dependency():
    declared = {re.split(r"[<>=\[ ]", d.strip().strip('",'))[0].lower().replace("_", "-")
                for d in re.findall(r'^\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)}
    aliases = {"sklearn": "scikit-learn", "yaml": "pyyaml", "psycopg": "psycopg"}
    stdlib, local = set(sys.stdlib_module_names), {"tradelens", "conftest"}
    missing = set()
    for base in ("tradelens", "scripts"):
        for p in (ROOT / base).rglob("*.py"):
            for node in ast.walk(ast.parse(p.read_text())):
                mods = [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else \
                       [a.name for a in node.names] if isinstance(node, ast.Import) else []
                for m in mods:
                    top = m.split(".")[0]
                    if top in stdlib or top in local:
                        continue
                    if aliases.get(top, top).lower().replace("_", "-") not in declared:
                        missing.add(top)
    assert not missing, f"imported but not declared in pyproject.toml: {sorted(missing)}"


def test_compose_has_one_app_one_worker_and_app_starts_the_server():
    class Strict(yaml.SafeLoader):
        pass

    def no_dupes(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        assert len(keys) == len(set(keys)), f"duplicate keys: {sorted({k for k in keys if keys.count(k) > 1})}"
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dupes)
    comp = yaml.load((ROOT / "docker-compose.yml").read_text(), Loader=Strict)
    assert set(comp["services"]) == {"db", "app", "worker"}
    assert "uvicorn" in " ".join(comp["services"]["app"]["command"])
    assert "&&" not in comp["services"]["worker"]["command"]          # exec-form lists do not run shells


def test_cli_migrate_and_import_universe(tmp_path, monkeypatch, capsys):
    from tradelens.cli import main
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["migrate"]) == 0
    (tmp_path / "u.csv").write_text("symbol,start,end\nAAA,2020-01-01,\nBBB,2019-01-01,2022-01-01\n")
    assert main(["import-universe", "u.csv"]) == 0
    assert "2 symbols" in capsys.readouterr().out
    assert main(["import-universe", "missing.csv"]) == 1
