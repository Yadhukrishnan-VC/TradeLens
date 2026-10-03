"""draft -> validated -> active -> retired.

draft      a new preset: backtest it, nothing else. Not scanned, not screened.
validated  a backtest passed the stability check on at least one stock. Scanned and screened; orders still need
           an explicit 'active'.
active     the human switched it on. Scanned, screened, may create orders (subject to the gate).
retired    switched off for good. Never scanned.

Evidence moves a preset forward (draft -> validated, automatically). Only a human makes it active.
No row = active, so the built-in strategies and presets saved before lifecycle existed keep working.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import StrategyFitRow, StrategyLifecycleRow

STATES = ("draft", "validated", "active", "retired")
SCANNED = ("validated", "active")
# manual moves a human may make (draft -> validated is evidence-only, so it is not listed)
ALLOWED = {
    ("validated", "active"), ("active", "validated"), ("draft", "retired"), ("validated", "retired"),
    ("active", "retired"), ("retired", "draft"), ("validated", "draft"), ("active", "draft"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(s: Session, strategy: str, config: str) -> StrategyLifecycleRow | None:
    return s.scalar(select(StrategyLifecycleRow).where(
        StrategyLifecycleRow.strategy == strategy, StrategyLifecycleRow.config_name == config))


def state_of(s: Session, strategy: str, config: str) -> str:
    r = _row(s, strategy, config)
    return r.state if r else "active"


def states_for(s: Session) -> dict[tuple[str, str], str]:
    return {(r.strategy, r.config_name): r.state for r in s.scalars(select(StrategyLifecycleRow)).all()}


def is_scanned(states: dict[tuple[str, str], str], strategy: str, config: str) -> bool:
    return states.get((strategy, config), "active") in SCANNED


def start_as_draft(s: Session, strategy: str, config: str) -> None:
    """Called when a preset is saved. Re-saving an existing preset keeps its state unless its numbers changed
    (new numbers = new, untested strategy)."""
    r = _row(s, strategy, config)
    if r is None:
        s.add(StrategyLifecycleRow(strategy=strategy, config_name=config, state="draft", updated_at=_now(),
                                   note="new settings: backtest them"))
    else:
        r.state, r.note, r.updated_at = "draft", "settings changed: backtest them again", _now()
    s.commit()


def set_state(s: Session, strategy: str, config: str, state: str, note: str = "") -> StrategyLifecycleRow:
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    cur = state_of(s, strategy, config)
    if cur == state:
        raise ValueError(f"{strategy} [{config}] is already {state}")
    if (cur, state) not in ALLOWED:
        hint = " A draft becomes 'validated' only when a backtest passes the stability check." if cur == "draft" and state in ("validated", "active") else ""
        raise ValueError(f"cannot move {strategy} [{config}] from {cur} to {state}.{hint}")
    r = _row(s, strategy, config)
    if r is None:
        r = StrategyLifecycleRow(strategy=strategy, config_name=config)
        s.add(r)
    r.state, r.note, r.updated_at = state, note[:200], _now()
    s.commit()
    return r


def record_evidence(s: Session, strategy: str, config: str) -> None:
    """Called after every backtest. If ANY stock passes the stability check, a draft becomes validated; if none
    does any more, a validated config goes back to draft. An 'active' one stays active: the human decided, and the
    gate will say that the evidence is gone."""
    r = _row(s, strategy, config)
    if r is None:                     # built-ins (no row) are active already
        return
    passed = s.scalar(select(StrategyFitRow.id).where(
        StrategyFitRow.strategy == strategy, StrategyFitRow.config_name == config,
        StrategyFitRow.verdict == "candidate").limit(1)) is not None
    if passed and r.state == "draft":
        r.state, r.note, r.updated_at = "validated", "passed the stability check", _now()
    elif not passed and r.state == "validated":
        r.state, r.note, r.updated_at = "draft", "no stock passes the stability check any more", _now()
    s.commit()
