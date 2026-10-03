"""Circuit breaker for flaky outside services (Yahoo prices, Telegram, webhooks).

closed     -> calls go through; consecutive failures are counted.
open       -> after `failure_threshold` failures in a row, calls are refused instantly for `cooldown` seconds,
              so a dead service is not hammered (and a rate-limit ban is not made worse).
half_open  -> after the cooldown exactly ONE probe call is allowed. Success closes the breaker, failure
              re-opens it for another cooldown. Other callers are refused while the probe is in flight.

Only failures of the SERVICE count (network errors, rate limits). "This symbol has no data" is a normal answer
from a healthy service and must not trip it: callers decide what to record.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class BreakerOpen(RuntimeError):
    def __init__(self, name: str, retry_in: float) -> None:
        super().__init__(f"{name} is paused after repeated failures; retrying in {int(retry_in) + 1}s")
        self.name, self.retry_in = name, retry_in


class CircuitBreaker:
    def __init__(self, name: str, *, failure_threshold: int = 3, cooldown: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.failure_threshold, self.cooldown, self._clock = name, failure_threshold, cooldown, clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False
        self.last_error = ""

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        """True if a call may go ahead now. In half-open state only the first caller gets a probe."""
        with self._lock:
            st = self._state_locked()
            if st == "closed":
                return True
            if st == "half_open" and not self._probing:
                self._probing = True
                return True
            return False

    def retry_in(self) -> float:
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self.cooldown - (self._clock() - self._opened_at))

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._opened_at, self._probing, self.last_error = 0, None, False, ""

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self.last_error = error[:120]
            self._probing = False
            self._failures += 1
            if self._opened_at is not None or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()      # (re)open: a failed probe restarts the cooldown

    def call(self, fn: Callable, *args, **kwargs):
        """Run fn through the breaker. Raises BreakerOpen without calling fn while the breaker is open."""
        if not self.allow():
            raise BreakerOpen(self.name, self.retry_in())
        try:
            out = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - any failure of the service counts
            self.record_failure(type(e).__name__)
            raise
        self.record_success()
        return out

    def status(self) -> dict:
        with self._lock:
            st = self._state_locked()
            retry = 0.0 if self._opened_at is None else max(0.0, self.cooldown - (self._clock() - self._opened_at))
            return {"name": self.name, "state": st, "failures": self._failures,
                    "retry_in_seconds": round(retry), "last_error": self.last_error}


_REGISTRY: dict[str, CircuitBreaker] = {}
_REG_LOCK = threading.Lock()


def get_breaker(name: str, **kw) -> CircuitBreaker:
    """One shared breaker per outside service, so the screener, the daily job and the API agree."""
    with _REG_LOCK:
        if name not in _REGISTRY:
            _REGISTRY[name] = CircuitBreaker(name, **kw)
        return _REGISTRY[name]


def all_status() -> list[dict]:
    with _REG_LOCK:
        return [b.status() for b in _REGISTRY.values()]


def reset_all() -> None:
    """For tests."""
    with _REG_LOCK:
        _REGISTRY.clear()
