"""Live Zerodha adapter — INTENTIONALLY NOT IMPLEMENTED YET.

Build it only after the paper pipeline has run cleanly for weeks. Requirements when you do:
  * implement Broker.place_order via kiteconnect (order tag <= 20 chars, idempotent on `tag`)
  * daily access-token flow, static IP whitelisted on the Kite developer console
  * reconcile positions/orders from the broker after every restart (broker is source of truth)
  * hard caps + kill switch enforced BEFORE this class is ever called
Failing loudly is deliberate: a fake fallback that pretends to trade is the worst possible bug.
"""
from __future__ import annotations

from ..domain import Side
from .base import Broker, Fill


class ZerodhaBroker(Broker):
    def place_order(self, *, symbol: str, side: Side, qty: int, price: float, tag: str) -> Fill:
        raise NotImplementedError("ZerodhaBroker is not implemented. Use BROKER=paper.")
