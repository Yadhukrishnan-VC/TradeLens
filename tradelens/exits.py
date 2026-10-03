"""Exit rules used by BOTH the backtester and the live/paper pipeline, so they cannot drift."""

from __future__ import annotations

from .domain import Side


def check_exit(
    side: Side,
    stop: float,
    target: float | None,
    o: float, h: float, l: float,
    slip: float = 0.0,
) -> tuple[float | None, str | None]:
    """Default exit rule: conservative gaps-through-fill, stop wins ties.
    
    IDENTICAL to original function for backward compatibility.
    """
    if side is Side.BUY:
        if o <= stop:
            return o * (1 - slip), "stop_gap"
        if l <= stop:
            return stop * (1 - slip), "stop"
        if target is not None:
            if o >= target:
                return o, "target_gap"
            if h >= target:
                return target, "target"
    else:
        if o >= stop:
            return o * (1 + slip), "stop_gap"
        if h >= stop:
            return stop * (1 + slip), "stop"
        if target is not None:
            if o <= target:
                return o, "target_gap"
            if l <= target:
                return target, "target"
    return None, None
