"""The go/no-go gate: may this signal become an order?

A signal is always RECORDED and shown. The gate only decides whether an order follows. It lives in the live path
(scan -> order) and nowhere else: backtests and portfolio backtests never call it, because a gate that blocks a
backtest can never collect the evidence it demands (a deadlock). Tests pin this.

reasons (each is a plain-language fact the dashboard can print):
  NOT_ACTIVE        the strategy/settings have not been switched on (draft, validated, retired)
  NOT_VALIDATED     this strategy has not passed the stability check on THIS stock
  STALE_VALIDATION  it passed, but too long ago to rely on
  EVENT_RISK        results or another event land within the event window
  OFF_REGIME        the market regime is not one the strategy is meant for (only when ENFORCE_REGIME=1)

mode off      -> nothing is evaluated
mode advisory -> reasons are attached; orders follow as usual
mode enforce  -> any reason holds the signal back (status 'gated')
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..domain import Mode

REASON_TEXT = {
    "NOT_ACTIVE": "this strategy is not switched on (draft, validated or retired)",
    "NOT_VALIDATED": "it has not passed the stability check on this stock",
    "STALE_VALIDATION": "its last passing test is too old to rely on",
    "EVENT_RISK": "results or another event are due within days",
    "OFF_REGIME": "the market regime is not the one this strategy is made for",
}


@dataclass(frozen=True)
class GateConfig:
    mode: str = "off"                 # off | advisory | enforce
    max_age_days: int = 45
    enforce_regime: bool = False
    event_window_days: int = 2

    @staticmethod
    def from_settings(settings, trading_mode: Mode) -> "GateConfig":
        mode = settings.gate_mode
        if mode == "auto":            # no human approves in auto mode, so the gate must
            mode = "enforce" if trading_mode is Mode.AUTO else "advisory"
        if mode not in ("off", "advisory", "enforce"):
            raise ValueError("GATE_MODE must be off, advisory, enforce or auto")
        return GateConfig(mode, settings.validation_max_age_days, settings.enforce_regime, settings.event_window_days)


@dataclass(frozen=True)
class GateResult:
    verdict: str                      # off | pass | warn | block
    reasons: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return ",".join(self.reasons)


def evaluate(cfg: GateConfig, *, state: str, rank: str, fit_updated_at: datetime | None, today: date,
             has_event: bool, off_regime: bool) -> GateResult:
    if cfg.mode == "off":
        return GateResult("off")
    reasons: list[str] = []
    if state != "active":
        reasons.append("NOT_ACTIVE")
    if rank != "proven":
        reasons.append("NOT_VALIDATED")
    elif fit_updated_at is not None and (today - fit_updated_at.date()).days > cfg.max_age_days:
        reasons.append("STALE_VALIDATION")
    if has_event:
        reasons.append("EVENT_RISK")
    if off_regime and cfg.enforce_regime:
        reasons.append("OFF_REGIME")
    if not reasons:
        return GateResult("pass")
    return GateResult("block" if cfg.mode == "enforce" else "warn", tuple(reasons))
