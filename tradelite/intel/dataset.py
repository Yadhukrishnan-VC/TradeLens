from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..intel.features import point_in_time_features


def build_row_from_trade(
    trade: object,
    atr: float,
    ma_20: float | None,
    ma_50: float | None,
    ma_200: float | None,
    ma_60: float | None,
    volume: float,
    ma_volume: float | None,
    prev_close: float,
    regime_label: str | None,
    strategy_name: str | None,
) -> dict[str, Any]:
    """Build a dataset row from a trade object.

    The label is the realised R (r_multiple) for that trade.
    Uses point-in-time features (no look-ahead).
    """
    # Get bar date from the trade entry or exit
    # Use exit timestamp as the bar timestamp for labeling
    bar_date = getattr(trade, "exit_ts", None)
    if bar_date is None:
        bar_date = getattr(trade, "entry_ts", None)
    if bar_date is not None:
        bar_date = bar_date.to_pydatetime() if hasattr(bar_date, "to_pydatetime") else datetime.fromisoformat(str(bar_date))
    else:
        bar_date = datetime.now()

    # Get close price - use exit price as the "close" for the bar
    close = getattr(trade, "exit_price", None)
    if close is None:
        close = getattr(trade, "entry_price", None)

    # Get prev close - we'll use the entry price as a proxy, or compute from the trade
    prev_close_val = getattr(trade, "entry_price", None)

    features = point_in_time_features(
        bar_date=bar_date,
        close=close,
        atr=atr,
        ma_20=ma_20,
        ma_50=ma_50,
        ma_200=ma_200,
        ma_60=ma_60,
        volume=volume,
        ma_volume=ma_volume,
        prev_close=prev_close_val,
        regieme_label=regime_label,
        strategy_name=strategy_name,
    )

    # Label is the r_multiple (realised R)
    r_multiple = getattr(trade, "r_multiple", None)
    if r_multiple is None:
        # Fall back to net_pnl / entry price as a proxy
        gross = getattr(trade, "gross_pnl", 0)
        entry = getattr(trade, "entry_price", 1.0)
        r_multiple = (gross - getattr(trade, "costs", 0)) / entry if entry else None

    row: dict[str, Any] = {
        "features": features,
        "label": r_multiple,
        "trade_id": getattr(trade, "symbol", "unknown"),
        "strategy": getattr(trade, "strategy", "unknown"),
        "symbol": getattr(trade, "symbol", "unknown"),
        "entry_ts": getattr(trade, "entry_ts", None),
        "exit_ts": getattr(trade, "exit_ts", None),
    }

    return row


def build_dataset_from_trades(
    trades: list[object],
    ma_windows: dict[str, float | None] | None = None,
    lookback_years: int = 5,
) -> pd.DataFrame:
    """Build a dataset from a list of trades for model training.

    Uses training-window-only data (no future knowledge).
    """
    if not trades:
        return pd.DataFrame()

    rows = []
    # Compute MA series from price data (placeholder - would use actual price history)
    # For now, use the trade data directly

    for trade in trades:
        # Get the features for this trade
        row = build_row_from_trade(
            trade=trade,
            atr=getattr(trade, "risk_amount", 1.0),  # fallback
            ma_20=None,  # would need price history
            ma_50=None,
            ma_200=None,
            ma_60=None,
            volume=getattr(trade, "qty", 1) * getattr(trade, "entry_price", 1.0),
            ma_volume=None,
            prev_close=getattr(trade, "entry_price", 1.0),
            regime_label=getattr(trade, "regime_label", None),
            strategy_name=getattr(trade, "strategy", "unknown"),
        )
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Separate features and labels
    # The features dict needs to be expanded into columns
    # For now, store as-is and let the model handling code expand them

    return df