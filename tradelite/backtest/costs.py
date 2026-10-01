"""Transaction cost model. Rates are APPROXIMATE defaults for Indian equities —
verify against Zerodha's brokerage calculator and the current SEBI/exchange schedule
before trusting net results. Costs matter most on small accounts."""
from __future__ import annotations

from dataclasses import dataclass

from ..domain import Side


@dataclass(frozen=True)
class CostModel:
    brokerage_pct: float = 0.0
    brokerage_cap: float = 0.0      # per-order cap in rupees (0 = no cap)
    stt_buy_pct: float = 0.001
    stt_sell_pct: float = 0.001
    exchange_pct: float = 0.0000297
    sebi_pct: float = 0.000001
    stamp_buy_pct: float = 0.00015
    gst_pct: float = 0.18

    def cost(self, side: Side, price: float, qty: int) -> float:
        turnover = price * qty
        brokerage = turnover * self.brokerage_pct
        if self.brokerage_cap:
            brokerage = min(brokerage, self.brokerage_cap)
        exchange = turnover * self.exchange_pct
        sebi = turnover * self.sebi_pct
        stt = turnover * (self.stt_buy_pct if side is Side.BUY else self.stt_sell_pct)
        stamp = turnover * self.stamp_buy_pct if side is Side.BUY else 0.0
        gst = (brokerage + exchange + sebi) * self.gst_pct
        return brokerage + exchange + sebi + stt + stamp + gst


DELIVERY_EQUITY = CostModel()
INTRADAY_EQUITY = CostModel(
    brokerage_pct=0.0003, brokerage_cap=20.0, stt_buy_pct=0.0, stt_sell_pct=0.00025, stamp_buy_pct=0.00003
)
# Exchange-traded funds (e.g. an index ETF used as a benchmark): no STT. Approximate; verify.
ETF_DELIVERY = CostModel(stt_buy_pct=0.0, stt_sell_pct=0.0)
