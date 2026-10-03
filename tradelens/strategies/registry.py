from __future__ import annotations

import importlib
import inspect
import pkgutil

from .base import Strategy

_SKIP = {"base", "registry"}


def discover() -> dict[str, type[Strategy]]:
    """Import every module in this package and collect Strategy subclasses by meta.name."""
    import tradelens.strategies as pkg

    found: dict[str, type[Strategy]] = {}
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name in _SKIP:
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
        for obj in vars(mod).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, Strategy)
                and obj is not Strategy
                and obj.__module__ == mod.__name__
            ):
                if obj.meta.name in found:
                    raise RuntimeError(f"duplicate strategy name: {obj.meta.name}")
                found[obj.meta.name] = obj
    return found


def get(name: str, **params) -> Strategy:
    strategies = discover()
    if name not in strategies:
        raise KeyError(f"unknown strategy '{name}'. available: {sorted(strategies)}")
    return strategies[name](**params)
