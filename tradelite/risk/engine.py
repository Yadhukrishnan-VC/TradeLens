"""Pre-trade risk checks. The SAME engine gates backtests and live orders."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain import AccountState, Signal


@dataclass(frozen=True)
class RiskConfig:
    risk_pct: float = 0.01              # equity risked per trade (entry->stop)
    max_position_pct: float = 0.25      # max notional per position, % of equity
    max_exposure_pct: float = 1.0       # max total notional, % of equity
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 0.03  # stop opening trades after this realized daily loss
    min_rr: float = 1.5                 # min reward:risk when a target exists


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: int
    reason: str  # "OK" or a reject code


def _reject(code: str) -> RiskDecision:
    return RiskDecision(False, 0, code)


def position_size(equity: float, risk_pct: float, entry: float, stop: float, max_notional: float) -> int:
    rpu = abs(entry - stop)
    if rpu <= 0 or entry <= 0 or equity <= 0:
        return 0
    by_risk = math.floor(equity * risk_pct / rpu)
    by_notional = math.floor(max(0.0, max_notional) / entry)
    return max(0, min(by_risk, by_notional))


class RiskEngine:
    def __init__(self, cfg: RiskConfig | None = None) -> None:
        self.cfg = cfg or RiskConfig()

    def evaluate(self, s: Signal, a: AccountState) -> RiskDecision:
        c = self.cfg
        if a.kill_switch:
            return _reject("KILL_SWITCH")
        if s.symbol in a.open_symbols:
            return _reject("ALREADY_IN_POSITION")
        if not s.stop_is_valid() or s.risk_per_unit <= 0:
            return _reject("INVALID_STOP")
        rr = s.rr
        if rr is not None and rr < c.min_rr:
            return _reject("RR_TOO_LOW")
        if a.realized_pnl_today <= -c.daily_loss_limit_pct * a.equity:
            return _reject("DAILY_LOSS_LIMIT")
        if a.open_positions >= c.max_open_positions:
            return _reject("MAX_POSITIONS")

        max_notional = min(
            c.max_position_pct * a.equity,
            max(0.0, c.max_exposure_pct * a.equity - a.exposure),
            max(0.0, a.cash),
        )
        by_risk = math.floor(a.equity * c.risk_pct / s.risk_per_unit) if a.equity > 0 else 0
        if by_risk < 1:
            return _reject("SIZE_ZERO_RISK_BUDGET")   # stop too wide for this account's risk budget
        qty = position_size(a.equity, c.risk_pct, s.entry, s.stop, max_notional)
        if qty < 1:
            return _reject("SIZE_ZERO_CAPITAL")       # cannot afford even one unit
        return RiskDecision(True, qty, "OK")
