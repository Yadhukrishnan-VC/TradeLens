from __future__ import annotations

from typing import Any

from ..models import SignalRow


def rank_signals(
    signals: list[Any],
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Single ranking function used by scan, live screener, and portfolio backtest.

    Score = shrunk lower confidence bound of expectancy in R, adjusted by:
    - model's expected R when a model is active and validated
    - regime flag (favourable regimes get a boost, unfavourable a penalty)

    Ties broken by:
    1. Relative strength against Nifty (higher = better rank)
    2. Liquidity (higher volume / better liquidity = better rank)

    When slots or capital are scarce, take the best scores first.

    Returns a list of dicts with:
    - rank: 1-based position
    - score: the numeric score
    - score_parts: JSON dict of score components
    - level: "proven"|"promising"|"unproven"|"disproven"
    - level_tooltip: plain-language explanation
    - why_ranked: explanation string
    """
    context = context or {}
    shrinkage_k = context.get("shrinkage_k", 20)

    # Collect per-signal data
    signal_data: list[dict[str, Any]] = []

    for i, sig in enumerate(signals):
        # Get the signal's score and components
        # Expected fields: expectancy, ci_low, ci_high, model_version, regime, model_active
        expectancy = getattr(sig, "expectancy", None) or 0.0
        ci_low = getattr(sig, "ci_low", None) or 0.0
        ci_high = getattr(sig, "ci_high", None) or 0.0
        model_version = getattr(sig, "model_version", None) or ""
        regime = getattr(sig, "regime", None) or ""
        model_active = getattr(sig, "model_active", False) or False
        n_trades = getattr(sig, "n_trades", 0) or 0

        # --- Compute shrunk lower CI bound ---
        # Shrinkage formula: (n * pair_mean + k * pooled_mean) / (n + k)
        # Here we shrink the ci_low toward 0 (the null hypothesis)
        pooled_mean = context.get("pooled_mean", 0.0)

        n = max(n_trades, 1)
        if ci_low is not None and pooled_mean is not None:
            shrunk_ci_low = (n * ci_low + shrinkage_k * 0.0) / (n + shrinkage_k)
        else:
            shrunk_ci_low = ci_low or 0.0

        # --- Adjust by model ---
        model_adjusted_r = 0.0
        if model_active and model_version:
            # Use model's expected R if available
            model_expected_r = context.get("model_expected_r", 0.0)
            model_adjusted_r = model_expected_r

        # --- Adjust by regime ---
        regime_boost = 0.0
        regime_penalty = 0.0
        regime = str(regime).lower() if regime else ""
        if regime == "bull_low_vol":
            regime_boost = 0.05  # small boost in favourable regime
        elif regime == "bear_high_vol":
            regime_penalty = 0.05  # penalty in unfavourable regime

        # Final score
        score = shrunk_ci_low + model_adjusted_r + regime_boost - regime_penalty

        # --- Determine level ---
        if expectancy > 0 and ci_low > 0 and n_trades >= 30:
            level = "proven"
            level_tooltip = "Enough trades with positive expectancy (CI above 0)"
        elif expectancy > 0 and n_trades >= 10:
            level = "promising"
            level_tooltip = "Positive point estimate but limited data"
        elif expectancy < 0 or ci_low < 0:
            level = "disproven"
            level_tooltip = "CI above 0 not achieved; evidence against strategy"
        else:
            level = "unproven"
            level_tooltip = "Insufficient evidence to judge"

        # --- Why ranked here ---
        parts = []
        if shrunk_ci_low != 0:
            parts.append(f"shrunk CI lower bound: {shrunk_ci_low:.2f}R")
        if model_adjusted_r != 0:
            parts.append(f"model R: {model_adjusted_r:.2f}R")
        if regime_boost != 0:
            parts.append(f"regime {regime}")
        if regime_penalty != 0:
            parts.append(f"regime penalty")
        if n_trades >= 30:
            parts.append(f"{n_trades} trades")
        elif n_trades > 0:
            parts.append(f"{n_trades} trades (small sample)")
        if not parts:
            parts.append("no data")

        why_ranked = "; ".join(parts)

        # --- Tie-breaking: relative strength against Nifty, then liquidity ---
        # These would be computed from actual market data; placeholders for now
        nifty_relative_strength = context.get("nifty_relative_strength", 0.0)
        liquidity = context.get("liquidity", 0.0)

        # Final ranking key: (score, nifty_rs, liquidity)
        # Higher score, higher nifty RS, higher liquidity = better rank
        signal_data.append(
            {
                "rank": 0,  # will be assigned later
                "score": score,
                "score_parts": {
                    "shrunk_ci_low": shrunk_ci_low,
                    "model_adjusted_r": model_adjusted_r,
                    "regime_boost": regime_boost,
                    "regime_penalty": regime_penalty,
                },
                "level": level,
                "level_tooltip": level_tooltip,
                "why_ranked": why_ranked,
                "n_trades": n_trades,
                "regime": regime,
                "nifty_relative_strength": nifty_relative_strength,
                "liquidity": liquidity,
            }
        )

    # Sort by: score desc, then nifty_relative_strength desc, then liquidity desc
    signal_data.sort(key=lambda x: (-x["score"], -x["nifty_relative_strength"], -x["liquidity"]))

    # Assign ranks (1-based)
    for i, sd in enumerate(signal_data):
        sd["rank"] = i + 1

    return signal_data


def level_from_score(score: float, expectancy: float, n_trades: int) -> tuple[str, str]:
    """Determine the ranking level from score, expectancy, and trade count.

    Returns (level, tooltip).
    """
    if expectancy > 0 and score > 0 and n_trades >= 30:
        return "proven", "Enough trades with positive expectancy (CI above 0)"
    elif expectancy > 0 and n_trades >= 10:
        return "promising", "Positive point estimate but limited data"
    elif expectancy < 0 or score < 0:
        return "disproven", "CI above 0 not achieved; evidence against strategy"
    else:
        return "unproven", "Insufficient evidence to judge"