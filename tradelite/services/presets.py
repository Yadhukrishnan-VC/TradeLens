"""User-customised strategy parameter sets ("presets"). The built-in defaults are the implicit
preset named 'default'; every other preset is tested, ranked and scanned on its own."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import StrategyConfigRow
from ..strategies import registry
from . import lifecycle

DEFAULT = "default"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")


def validate(strategy: str, name: str, params: dict) -> dict:
    if name.strip().lower() == DEFAULT:
        raise ValueError("'default' is reserved for the built-in parameters; pick another name")
    if not _NAME.match(name):
        raise ValueError("name must be 1-64 characters: letters, digits, space, _ . -")
    catalog = registry.discover()
    if strategy not in catalog:
        raise KeyError(f"unknown strategy '{strategy}'. available: {sorted(catalog)}")
    defaults = catalog[strategy].default_params
    clean: dict = {}
    for k, v in params.items():
        if k not in defaults:
            raise ValueError(f"unknown param '{k}' for {strategy}. valid: {sorted(defaults)}")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"param '{k}' must be a number")
        if v < 0:
            raise ValueError(f"param '{k}' must not be negative")
        clean[k] = int(v) if isinstance(defaults[k], int) and float(v).is_integer() else float(v)
    registry.get(strategy, **clean)   # final check: the strategy accepts the merged set
    return clean


def save(session: Session, strategy: str, name: str, params: dict) -> StrategyConfigRow:
    clean = validate(strategy, name, params)
    row = session.scalar(select(StrategyConfigRow).where(
        StrategyConfigRow.strategy == strategy, StrategyConfigRow.name == name))
    if row is None:
        row = StrategyConfigRow(strategy=strategy, name=name)
        session.add(row)
    changed = row.params != clean if row.id is not None else True
    row.params = clean
    row.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.commit()
    if changed:                                   # new or edited numbers = an untested strategy: start as a draft
        lifecycle.start_as_draft(session, strategy, name)
    return row


def resolve(session: Session, strategy: str, config_name: str) -> dict:
    """Params for (strategy, config_name). 'default' -> {} (the strategy's own defaults)."""
    if config_name == DEFAULT:
        return {}
    row = session.scalar(select(StrategyConfigRow).where(
        StrategyConfigRow.strategy == strategy, StrategyConfigRow.name == config_name))
    if row is None:
        raise KeyError(f"no preset '{config_name}' for strategy '{strategy}'")
    return dict(row.params)


def for_strategies(session: Session, names: list[str]) -> dict[str, list[StrategyConfigRow]]:
    out: dict[str, list[StrategyConfigRow]] = {n: [] for n in names}
    for r in session.scalars(select(StrategyConfigRow).where(StrategyConfigRow.strategy.in_(names))
                             .order_by(StrategyConfigRow.name)).all():
        out[r.strategy].append(r)
    return out
