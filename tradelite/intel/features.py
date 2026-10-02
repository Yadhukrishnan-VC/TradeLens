from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from ..models import PriceBarRow


def atr_percent(price: float, atr: float, close_series: np.ndarray | None = None) -> float:
    """ATR as a percent of current price."""
    if atr is None or atr == 0:
        return 0.0
    return atr / price * 100.0


def distance_in_atr_units(
    price: float,
    ma_series: np.ndarray,
    atr_series: np.ndarray,
    ma_period: int,
    side: str = "above",
) -> float | None:
    """Distance from price to MA in ATR units.

    side: 'above' or 'below'
    """
    if atr_series is None or len(atr_series) == 0:
        return None
    ma_val = ma_series[-1] if len(ma_series) > 0 else np.nan
    atr_val = atr_series[-1] if len(atr_series) > 0 else np.nan
    if np.isnan(ma_val) or np.isnan(atr_val):
        return None
    diff = abs(price - ma_val) / atr_val
    return diff


def volume_ratio(current_volume: float, ma_volume: float | None) -> float | None:
    """Current volume vs its moving average."""
    if ma_volume is None or ma_volume == 0:
        return None
    return current_volume / ma_volume


def return_rank(universe_returns: np.ndarray, symbol_returns: float) -> float | None:
    """Return rank of symbol's return against the universe.

    universe_returns: array of returns for all symbols in the universe
    symbol_returns: the symbol's return
    """
    if universe_returns is None or len(universe_returns) == 0:
        return None
    try:
        rank = float(np.sum(universe_returns <= symbol_returns) / len(universe_returns) * 100)
        return rank
    except (TypeError, ValueError):
        return None


def gap_size(open_price: float, prev_close: float) -> float | None:
    """Gap size as a percent of previous close."""
    if prev_close == 0:
        return None
    return (open_price - prev_close) / prev_close * 100.0


def point_in_time_features(
    bar_date: datetime,
    close: float,
    atr: float,
    ma_20: float | None,
    ma_50: float | None,
    ma_200: float | None,
    ma_60: float | None,
    volume: float,
    ma_volume: float | None,
    prev_close: float,
    regieme_label: str | None,
    strategy_name: str | None,
) -> dict[str, Any]:
    """Point-in-time features at the signal bar: no future data used.

    All values are computed from data available at the signal bar.
    """
    features: dict[str, Any] = {}

    # ATR as percent of price
    features["atr_pct"] = atr_percent(close, atr)

    # Distance from MAs in ATR units
    if ma_20 is not None:
        features["dist_ma20_atr"] = distance_in_atr_units(close, np.array([ma_20]) if isinstance(ma_20, float) else ma_20, np.array([atr]) if isinstance(atr, float) else atr, 20)
    else:
        features["dist_ma20_atr"] = None
    if ma_50 is not None:
        features["dist_ma50_atr"] = distance_in_atr_units(close, np.array([ma_50]) if isinstance(ma_50, float) else ma_50, np.array([atr]) if isinstance(atr, float) else atr, 50)
    else:
        features["dist_ma50_atr"] = None
    if ma_200 is not None:
        features["dist_ma200_atr"] = distance_in_atr_units(close, np.array([ma_200]) if isinstance(ma_200, float) else ma_200, np.array([atr]) if isinstance(atr, float) else atr, 200)
    else:
        features["dist_ma200_atr"] = None
    if ma_60 is not None:
        features["dist_ma60_atr"] = distance_in_atr_units(close, np.array([ma_60]) if isinstance(ma_60, float) else ma_60, np.array([atr]) if isinstance(atr, float) else atr, 60)
    else:
        features["dist_ma60_atr"] = None

    # Volume ratio
    features["volume_ratio"] = volume_ratio(volume, ma_volume)

    # 20-day return rank
    # Placeholder: would need universe returns data
    features["return_rank_20"] = None

    # 60-day return rank
    features["return_rank_60"] = None

    # Gap size
    features["gap_pct"] = gap_size(close, prev_close)

    # Regime one-hot encoding
    if regieme_label == "bull_low_vol":
        features["regime_bull"] = 1.0
        features["regime_bear"] = 0.0
        features["regime_highvol"] = 0.0
        features["regime_lowvol"] = 1.0
    elif regieme_label == "bull_high_vol":
        features["regime_bull"] = 1.0
        features["regime_bear"] = 0.0
        features["regime_highvol"] = 1.0
        features["regime_lowvol"] = 0.0
    elif regieme_label == "bear_low_vol":
        features["regime_bull"] = 0.0
        features["regime_bear"] = 1.0
        features["regime_highvol"] = 0.0
        features["regime_lowvol"] = 1.0
    elif regieme_label == "bear_high_vol":
        features["regime_bull"] = 0.0
        features["regime_bear"] = 1.0
        features["regime_highvol"] = 1.0
        features["regime_lowvol"] = 0.0
    elif regieme_label == "mixed":
        features["regime_bull"] = 0.5
        features["regime_bear"] = 0.5
        features["regime_highvol"] = 0.5
        features["regime_lowvol"] = 0.5
    else:
        features["regime_bull"] = 0.0
        features["regime_bear"] = 0.0
        features["regime_highvol"] = 0.0
        features["regime_lowvol"] = 0.0

    # Strategy one-hot (placeholder - would be many strategies)
    if strategy_name:
        # Encode strategy name as a simple hash-based one-hot
        # In production, would use learned embeddings or rare categories handling
        h = hash(strategy_name) % 1000
        features["strategy_hash"] = h / 1000.0
    else:
        features["strategy_hash"] = 0.0

    # Timestamp features
    features["day_of_week"] = bar_date.weekday() / 6.0
    features["day_of_month"] = bar_date.day / 31.0
    features["hour"] = bar_date.hour / 24.0

    return features