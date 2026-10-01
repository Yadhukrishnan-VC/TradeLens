"""signal -> risk -> (record | wait for approval | broker) -> position -> automatic exits."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtest.costs import DELIVERY_EQUITY, CostModel
from ..broker.base import Broker
from ..data.base import DataProvider
from ..domain import AccountState, Mode, Side, Signal
from ..exits import check_exit
from ..models import Account, OrderRow, PositionRow, SignalRow, StrategyFitRow
from ..risk.engine import RiskEngine
from ..strategies import registry
from . import presets, ranking

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


class Pipeline:
    def __init__(
        self,
        session: Session,
        provider: DataProvider,
        broker: Broker,
        *,
        risk: RiskEngine | None = None,
        cost_model: CostModel | None = None,
        mode: Mode = Mode.SEMI_AUTO,
        starting_capital: float = 100_000.0,
        require_fit: bool = False,
        clock=ist_now,
        notifier: Notifier | None = None,
    ) -> None:
        self.s, self.provider, self.broker = session, provider, broker
        self.risk = risk or RiskEngine()
        self.cost_model = cost_model or DELIVERY_EQUITY
        self.mode, self.require_fit, self.clock = mode, require_fit, clock
        self._starting_capital = starting_capital
        self.notifier = notifier

    # ---------- account ----------
    def account(self) -> Account:
        acct = self.s.get(Account, 1)
        if acct is None:
            acct = Account(id=1, starting_capital=self._starting_capital, kill_switch=False)
            self.s.add(acct)
            self.s.flush()
        return acct

    def set_kill_switch(self, active: bool) -> None:
        self.account().kill_switch = active
        self.s.commit()

    def _marks(self, open_: list[PositionRow]) -> dict[str, float]:
        """Latest close per open symbol. A symbol that cannot be priced is simply absent."""
        marks: dict[str, float] = {}
        for sym in {p.symbol for p in open_}:
            try:
                marks[sym] = float(self.provider.get_bars(sym)["close"].iloc[-1])
            except (FileNotFoundError, ValueError, KeyError, IndexError):
                continue
        return marks

    def account_state(self) -> AccountState:
        """Equity is marked to market: closed P&L + open P&L at the latest close - entry costs.
        (It used to count closed P&L only, so a losing open position did not shrink new position sizes.)"""
        acct = self.account()
        rows = self.s.scalars(select(PositionRow)).all()
        open_ = [p for p in rows if p.closed_at is None]
        closed = [p for p in rows if p.closed_at is not None]
        marks = self._marks(open_)
        unrealized = sum((marks.get(p.symbol, p.entry_price) - p.entry_price) * p.qty * Side(p.side).sign
                         for p in open_)
        unpriced = sum(1 for p in open_ if p.symbol not in marks)
        equity = (acct.starting_capital + sum(p.pnl or 0.0 for p in closed)
                  + unrealized - sum(p.entry_costs for p in open_))
        exposure = sum(p.qty * p.entry_price for p in open_)
        today = self.clock().date()
        pnl_today = sum(p.pnl or 0.0 for p in closed if p.closed_at and p.closed_at.date() == today)
        return AccountState(
            equity=equity, cash=max(0.0, equity - exposure), open_positions=len(open_),
            exposure=exposure, realized_pnl_today=pnl_today,
            open_symbols=frozenset(p.symbol for p in open_), kill_switch=acct.kill_switch,
            unrealized_pnl=unrealized, unpriced_positions=unpriced,
        )

    def _alert(self, level: str, title: str, body: str = "") -> None:
        if self.notifier is not None:
            self.notifier.send(Alert(level, title, body))

    # ---------- scan ----------
    def scan(self, symbols: list[str] | None = None, strategy_names: list[str] | None = None,
             timeframe: str = "1d", use_presets: bool = True) -> list[SignalRow]:
        """Run every strategy (default params + the user's saved presets) on every symbol. Signals
        whose strategy+config has PROVEN itself on that symbol are ranked first, so they also get
        first claim on limited position slots and capital."""
        catalog = registry.discover()
        names = strategy_names or sorted(catalog)
        presets_by = presets.for_strategies(self.s, names) if use_presets else {n: [] for n in names}
        found: list[tuple[Signal, str, str, float]] = []   # (signal, config_name, rank, score)
        for symbol in symbols or self.provider.symbols():
            df = self.provider.get_bars(symbol, timeframe)
            for name in names:
                configs = [(presets.DEFAULT, {})] + [(r.name, r.params) for r in presets_by.get(name, [])]
                for cfg_name, params in configs:
                    strat = catalog[name](**params)
                    if timeframe not in strat.meta.timeframes or len(df) < strat.meta.min_bars + 2:
                        continue
                    rank, score = self.rank_for(name, cfg_name, symbol, timeframe)
                    if self.require_fit and rank != "proven":
                        continue
                    prep = strat.prepare(df)
                    sig = strat.on_bar(prep, len(prep) - 1, symbol)
                    if sig is not None:
                        found.append((sig, cfg_name, rank, score))
        found.sort(key=lambda f: (f[2] != "proven", -f[3]))   # proven first, best profit factor first
        created: list[SignalRow] = []
        for sig, cfg_name, rank, score in found:
            row = self._store_signal(sig, cfg_name, rank, score)
            if row is None:      # already seen this exact signal
                continue
            self._route(row, sig)
            created.append(row)
        self.s.commit()
        return created

    def rank_for(self, strategy: str, config_name: str, symbol: str, timeframe: str = "1d") -> tuple[str, float]:
        return ranking.rank_for(self.s, strategy, config_name, symbol, timeframe)

    def _store_signal(self, sig: Signal, config_name: str = "default", rank: str = "unproven",
                      score: float = 0.0) -> SignalRow | None:
        ts = sig.ts.replace(tzinfo=None)
        exists = self.s.scalar(select(SignalRow.id).where(
            SignalRow.strategy == sig.strategy, SignalRow.config_name == config_name,
            SignalRow.symbol == sig.symbol, SignalRow.ts == ts))
        if exists:
            return None
        row = SignalRow(strategy=sig.strategy, config_name=config_name, symbol=sig.symbol, side=sig.side.value,
                        ts=ts, entry=sig.entry, stop=sig.stop, target=sig.target, status="new",
                        rank=rank, rank_score=score, created_at=self.clock())
        self.s.add(row)
        self.s.flush()
        return row

    @staticmethod
    def _to_signal(r: SignalRow) -> Signal:
        return Signal(r.strategy, r.symbol, Side(r.side), r.ts, r.entry, r.stop, r.target)

    @staticmethod
    def _tag(sig: Signal, config_name: str = "default") -> str:
        key = f"{sig.strategy}|{sig.symbol}|{sig.ts.isoformat()}"
        if config_name != "default":   # keeps tags of default-config orders identical to before
            key += f"|{config_name}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    def _route(self, row: SignalRow, sig: Signal) -> None:
        decision = self.risk.evaluate(sig, self.account_state())
        if not decision.approved:
            row.status, row.reason = "rejected", decision.reason
            return
        row.suggested_qty = decision.qty
        if self.mode is Mode.SIGNAL_ONLY:
            row.status = "signal"
            return
        order = OrderRow(signal_id=row.id, symbol=sig.symbol, side=sig.side.value, qty=decision.qty,
                         status="PENDING_APPROVAL", mode=self.mode.value, tag=self._tag(sig, row.config_name),
                         rank=row.rank, rank_score=row.rank_score,
                         created_at=self.clock())
        self.s.add(order)
        self.s.flush()
        if self.mode is Mode.SEMI_AUTO:
            row.status = "proposed"
        else:  # AUTO
            self._execute(order, row)

    # ---------- orders ----------
    def _execute(self, order: OrderRow, sig_row: SignalRow) -> None:
        side = Side(order.side)
        fill = self.broker.place_order(symbol=order.symbol, side=side, qty=order.qty,
                                       price=sig_row.entry, tag=order.tag)
        if fill.status != "FILLED":
            order.status, order.reason = "REJECTED", "BROKER_REJECTED"
            sig_row.status, sig_row.reason = "rejected", "BROKER_REJECTED"
            self._alert("critical", f"Broker rejected {order.side} {order.qty} {order.symbol}",
                        f"strategy={sig_row.strategy} order tag={order.tag}")
            return
        order.status, order.price = "FILLED", fill.price
        self.s.add(PositionRow(
            symbol=order.symbol, strategy=sig_row.strategy, side=order.side, qty=fill.qty,
            entry_price=fill.price, stop=sig_row.stop, target=sig_row.target,
            entry_costs=self.cost_model.cost(side, fill.price, fill.qty),
            signal_bar_ts=sig_row.ts, opened_at=self.clock()))
        sig_row.status = "executed"

    def approve(self, order_id: int) -> OrderRow:
        order = self.s.get(OrderRow, order_id)
        if order is None:
            raise KeyError(f"order {order_id} not found")
        if order.status != "PENDING_APPROVAL":
            raise ValueError(f"order {order_id} is {order.status}, not pending approval")
        sig_row = self.s.get(SignalRow, order.signal_id)
        latest = self.provider.get_bars(order.symbol).index[-1].to_pydatetime()
        if latest > sig_row.ts:   # a newer bar exists: the setup this signal describes is stale
            return self._reject_order(order, sig_row, "STALE_SIGNAL")
        decision = self.risk.evaluate(self._to_signal(sig_row), self.account_state())  # re-check NOW
        if not decision.approved:
            return self._reject_order(order, sig_row, decision.reason)
        order.qty = decision.qty
        self._execute(order, sig_row)
        self.s.commit()
        return order

    def reject(self, order_id: int) -> OrderRow:
        order = self.s.get(OrderRow, order_id)
        if order is None:
            raise KeyError(f"order {order_id} not found")
        if order.status != "PENDING_APPROVAL":
            raise ValueError(f"order {order_id} is {order.status}, not pending approval")
        return self._reject_order(order, self.s.get(SignalRow, order.signal_id), "USER_REJECTED", "CANCELLED")

    def _reject_order(self, order: OrderRow, sig_row: SignalRow, reason: str, status: str = "REJECTED") -> OrderRow:
        order.status, order.reason = status, reason
        sig_row.status, sig_row.reason = "rejected", reason
        self.s.commit()
        return order

    # ---------- exits (stop/target are ALWAYS automatic, in every mode) ----------
    def check_exits(self) -> list[PositionRow]:
        closed: list[PositionRow] = []
        for pos in self.s.scalars(select(PositionRow).where(PositionRow.closed_at.is_(None))).all():
            side = Side(pos.side)
            bars = self.provider.get_bars(pos.symbol)
            bars = bars[bars.index > pos.signal_bar_ts]
            for _, bar in bars.iterrows():
                px, reason = check_exit(side, pos.stop, pos.target, bar["open"], bar["high"], bar["low"])
                if px is None:
                    continue
                fill = self.broker.place_order(symbol=pos.symbol, side=side.opposite, qty=pos.qty,
                                               price=px, tag=f"x{pos.id}")
                if fill.status != "FILLED":
                    # the position is still open although its exit was triggered: a human must look
                    self._alert("critical", f"EXIT ORDER FAILED for {pos.symbol}",
                                f"{reason} triggered at {px:.2f} but the broker rejected the {side.opposite.value} of {pos.qty}. "
                                f"The position is still open; the next check will retry.")
                    break
                gross = (fill.price - pos.entry_price) * pos.qty * side.sign
                pos.exit_price, pos.exit_reason = fill.price, reason
                pos.pnl = gross - pos.entry_costs - self.cost_model.cost(side.opposite, fill.price, pos.qty)
                pos.closed_at = self.clock()
                closed.append(pos)
                break
        self.s.commit()
        return closed
