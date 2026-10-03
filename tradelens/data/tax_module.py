"""Capital gains tax module with configurable rates and holding periods.

Simple loss set-off within a year. Rates and holding periods should be verified
against current Indian income tax rules.

Config fields:
- short_term_rate: tax rate for holdings <= 1 year (equity: 15% typical, verify)
- long_term_rate: tax rate for holdings > 1 year (equity: 10% with indexation, verify)
- holding_period_days: threshold between short and long term (typically 365 or 12 months)
- enable_loss_set_off: whether to allow loss set-off within a year
- loss_set_off_cap: cap on loss set-off amount per year
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class TaxConfig:
    """Tax configuration for capital gains."""
    short_term_rate: float = 0.15       # Short-term capital gains rate (15% typical for Indian equity)
    long_term_rate: float = 0.10        # Long-term capital gains rate (10% typical for Indian equity)
    holding_period_days: int = 365      # Threshold between short and long term
    enable_loss_set_off: bool = True    # Allow loss set-off within a year
    loss_set_off_cap: float | None = None  # Max loss set-off amount per year (in equity)


def compute_holding_period(entry_date: datetime, exit_date: datetime) -> int:
    """Compute holding period in days."""
    if exit_date is None or entry_date is None:
        return 0
    delta = exit_date - entry_date
    return max(0, delta.days)


def compute_tax(
    gross_pnl: float,
    entry_date: datetime,
    exit_date: datetime,
    tax_config: TaxConfig | None = None,
) -> dict[str, Any]:
    """Compute tax on a trade's P&L.

    Returns dict with:
    - gross_pnl: original P&L
    - taxable_pnl: P&L after allowable adjustments
    - short_term_pnl: portion taxed as short-term
    - long_term_pnl: portion taxed as long-term
    - tax: total tax amount
    - net_pnl: P&L after tax
    - holding_period_days: days held
    """
    tax_config = tax_config or TaxConfig()
    holding_days = compute_holding_period(entry_date, exit_date)

    # Classify as short-term or long-term
    is_short_term = holding_days <= tax_config.holding_period_days

    if is_short_term:
        tax_rate = tax_config.short_term_rate
        taxable_pnl = gross_pnl
        short_term_pnl = gross_pnl
        long_term_pnl = 0.0
    else:
        tax_rate = tax_config.long_term_rate
        taxable_pnl = gross_pnl
        short_term_pnl = 0.0
        long_term_pnl = gross_pnl

    # Loss set-off within a year (if enabled)
    if tax_config.enable_loss_set_off and is_short_term:
        # Loss can set off against short-term gains within the same year
        # This is a simplified placeholder - full implementation would track
        # losses across multiple trades and years
        pass

    # Compute tax
    tax = taxable_pnl * tax_rate if taxable_pnl > 0 else 0.0

    net_pnl = gross_pnl - tax

    return {
        "gross_pnl": gross_pnl,
        "taxable_pnl": taxable_pnl,
        "short_term_pnl": short_term_pnl,
        "long_term_pnl": long_term_pnl,
        "tax": tax,
        "net_pnl": net_pnl,
        "holding_period_days": holding_days,
        "tax_rate_applied": tax_rate,
        "is_short_term": is_short_term,
    }


def compute_cost_drag(
    gross_profit: float,
    total_costs: float,
    total_tax: float,
) -> float:
    """Compute cost drag as a share of gross profit.

    cost_drag = (costs + tax) / gross_profit
    """
    if gross_profit == 0:
        return 0.0
    return (total_costs + total_tax) / gross_profit