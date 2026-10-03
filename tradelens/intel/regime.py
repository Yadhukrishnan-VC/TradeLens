from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..models import PriceBarRow


TREND_MA_50 = "ma_50"
TREND_MA_200 = "ma_200"
TREND_UP = "up"
TREND_DOWN = "down"
VOL_LOW = "low"
VOL_HIGH = "high"
BREADTH_POS = "positive"
BREADTH_NEG = "negative"


def _sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average with NaN padding at the start."""
    if len(values) < period:
        return np.full(len(values), np.nan)
    return np.convolve(values, np.ones(period) / period, mode="valid")


def compute_regime(
    close: np.ndarray,
    volumes: np.ndarray | None = None,
    vix_data: dict[str, float] | None = None,
    benchmark_clos: np.ndarray | None = None,
) -> dict[str, Any]:
    """Compute market regime labels using deterministic rules.

    Regime = trend (up/down) by volatility (low/high).

    Trend: benchmark close vs its 200-day and 50-day simple moving averages.
    - If price > both MAs: bullish
    - If price < both MAs: bearish
    - Otherwise: mixed

    Volatility: using India VIX (if provided) or realised volatility as fallback.
    - Realised vol: std of daily returns over the last 20 trading days
    - VIX: provided value (lower = lower volatility)

    Breadth: share of stored stocks above their 50-day average (computed elsewhere).

    Returns dict with: trend, volatility, breadth, label
    """
    n = len(close)
    if n < 200:
        return {"trend": None, "volatility": None, "breadth": None, "label": None}

    # --- Trend ---
    # Use benchmark close if provided, otherwise use the close series itself
    prices = benchmark_clos if benchmark_clos is not None else close

    ma_50 = _sma(prices, 50)
    ma_200 = _sma(prices, 200)

    # For the most recent bar, check position relative to MAs
    # We need to align the MA values with the full length
    full_ma_50 = np.full(n, np.nan)
    full_ma_200 = np.full(n, np.nan)
    if len(ma_50) > 0:
        full_ma_50[-len(ma_50) :] = ma_50
    if len(ma_200) > 0:
        full_ma_200[-len(ma_200) :] = ma_200

    latest_close = close[-1] if len(close) > 0 else np.nan
    latest_ma_50 = full_ma_50[-1] if not np.isnan(full_ma_50[-1]) else np.nan
    latest_ma_200 = full_ma_200[-1] if not np.isnan(full_ma_200[-1]) else np.nan

    if not np.isnan(latest_ma_50) and not np.isnan(latest_ma_200):
        if latest_close > latest_ma_50 > latest_ma_200:
            trend = TREND_UP
        elif latest_close < latest_ma_50 < latest_ma_200:
            trend = TREND_DOWN
        else:
            trend = "mixed"
    else:
        trend = None

    # --- Volatility ---
    if vix_data is not None and "vix" in vix_data:
        vix = float(vix_data["vix"])
        vol_label = VOL_LOW if vix < 20 else VOL_HIGH
    else:
        # Realised volatility: std of daily returns over last 20 days
        if len(close) >= 20:
            returns = np.diff(close.astype(float)).astype(float) / close[:-1].astype(float)
            vol = float(np.std(returns[-20:]))
            vol_label = VOL_LOW if vol < 0.02 else VOL_HIGH  # 2% daily vol threshold
        else:
            vol_label = None

    # --- Breadth ---
    # Placeholder: will be computed from the full universe later
    breadth = None

    # --- Label ---
    if trend is not None and vol_label is not None:
        if trend == TREND_UP and vol_label == VOL_LOW:
            label = "bull_low_vol"
        elif trend == TREND_UP and vol_label == VOL_HIGH:
            label = "bull_high_vol"
        elif trend == TREND_DOWN and vol_label == VOL_LOW:
            label = "bear_low_vol"
        elif trend == TREND_DOWN and vol_label == VOL_HIGH:
            label = "bear_high_vol"
        else:
            label = "mixed"
    else:
        label = None

    return {
        "trend": trend,
        "volatility": vol_label,
        "breadth": breadth,
        "label": label,
    }


def regimes_to_label(regime: str | None) -> str:
    """Convert regime code to plain-language description."""
    mapping = {
        "bull_low_vol": "Bullish, low volatility",
        "bull_high_vol": "Bullish, high volatility",
        "bear_low_vol": "Bearish, low volatility",
        "bear_high_vol": "Bearish, high volatility",
        "mixed": "Mixed trend",
    }
    return mapping.get(regime, regime or "unknown")


def add_regime_to_bar(
    bar: dict[str, Any],
    close_series: np.ndarray,
    volumes: np.ndarray | None = None,
    vix_data: dict[str, float] | None = None,
    benchmark_clos: np.ndarray | None = None,
) -> dict[str, Any]:
    """Add regime label to a single bar dict.

    Used by the worker to annotate signals with the current regime.
    """
    regime = compute_regime(close_series, volumes, vix_data, benchmark_clos)
    bar["regime_trend"] = regime["trend"]
    bar["regime_volatility"] = regime["volatility"]
    bar["regime_breadth"] = regime["breadth"]
    bar["regime_label"] = regime["label"]
    return bar