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
from ..models import Account, OrderRow, PositionAlertRow, PositionRow, SignalRow, StrategyFitRow, WatchRow
from ..risk.engine import RiskEngine
from ..strategies import registry
from . import context, events, gate, lifecycle, presets, quality, ranking
from .alerts import Alert
from .gate import GateConfig

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
        gate: GateConfig | None = None,
        benchmark: str = "",
        stale_check: bool = False,
    ) -> None:
        self.s, self.provider, self.broker = session, provider, broker
        self.risk = risk or RiskEngine()
        self.cost_model = cost_model or DELIVERY_EQUITY
        self.mode, self.require_fit, self.clock = mode, require_fit, clock
        self._starting_capital = starting_capital
        self.notifier = notifier
        self.gate_cfg = gate or GateConfig()      # default: off, so a bare Pipeline behaves exactly as before
        self.benchmark = benchmark
        self.stale_check = stale_check            # flag symbols whose last bar is older than the universe's newest bar
        self.last_watch: list[dict] = []          # setups close to triggering, found by the last scan

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
        """Run every switched-on strategy (built-in settings + the user's saved presets) on every symbol.
        Signals whose strategy+config has PROVEN itself on that symbol are ranked first, so they also get
        first claim on limited position slots and capital. Every signal records the data quality, the market
        regime and the gate's verdict; setups one step from triggering are saved as the watchlist."""
        catalog = registry.discover()
        names = strategy_names or sorted(catalog)
        presets_by = presets.for_strategies(self.s, names) if use_presets else {n: [] for n in names}
        states = lifecycle.states_for(self.s)
        syms = symbols or self.provider.symbols()
        frames = {sym: self.provider.get_bars(sym, timeframe) for sym in syms}
        latest = max((df.index[-1].date() for df in frames.values() if len(df)), default=None) if self.stale_check else None
        regime = context.compute_regime(self.provider, self.benchmark, syms, frames=frames)
        held = self.account_state().open_symbols
        found: list = []     # (signal, config_name, rank, score, quality, meta)
        watched: list = []
        for symbol, df in frames.items():
            qual = quality.assess(df, latest_date=latest)
            for name in names:
                configs = [(presets.DEFAULT, {})] + [(r.name, r.params) for r in presets_by.get(name, [])]
                for cfg_name, params in configs:
                    if not lifecycle.is_scanned(states, name, cfg_name):     # draft and retired are never scanned
                        continue
                    strat = catalog[name](**params)
                    if timeframe not in strat.meta.timeframes or len(df) < strat.meta.min_bars + 2:
                        continue
                    verdict, score, adjusted, reason = self.rank_for(name, cfg_name, symbol, timeframe)
                    if self.require_fit and verdict != "proven":
                        continue
                    prep = strat.prepare(df)
                    last = len(prep) - 1
                    sig = strat.on_bar(prep, last, symbol)
                    if sig is not None:
                        found.append((sig, cfg_name, rank, score, qual, strat.meta))
                    elif symbol not in held:
                        w = strat.watch(prep, last, symbol)
                        if w is not None:
                            watched.append((name, cfg_name, symbol, df.index[-1].to_pydatetime(), float(df["close"].iloc[-1]), w, rank, score))
        found.sort(key=lambda f: (f[2] != "proven", -f[3]))   # proven first, best profit factor first
        created: list[SignalRow] = []
        for sig, cfg_name, rank, score, qual, meta in found:
            row = self._store_signal(sig, cfg_name, rank, score)
            if row is None:      # already seen this exact signal
                continue
            self._decorate(row, sig, qual, meta, regime)
            self._route(row, sig)
            created.append(row)
        self.last_watch = self._store_watch(watched)
        self.s.commit()
        return created

    def _decorate(self, row: SignalRow, sig: Signal, qual, meta, regime) -> None:
        """Attach what a human needs to judge the signal: data quality, upcoming events, regime, gate verdict."""
        evs = events.upcoming(self.s, sig.symbol, sig.ts.date(), self.gate_cfg.event_window_days)
        off_regime = bool(meta.regimes) and regime.trend != "unknown" and regime.trend not in meta.regimes
        flags = list(qual.flags) + [f"EVENT:{e.kind}" for e in evs[:2]] + (["OFF_REGIME"] if off_regime else [])
        row.quality, row.flags = qual.level, ",".join(flags)[:120]
        row.regime = regime.label if regime.label != "unknown" else ""
        fit_at = self.s.scalar(select(StrategyFitRow.updated_at).where(
            StrategyFitRow.strategy == sig.strategy, StrategyFitRow.config_name == row.config_name,
            StrategyFitRow.symbol == sig.symbol))
        g = gate.evaluate(self.gate_cfg, state=lifecycle.state_of(self.s, sig.strategy, row.config_name), rank=row.rank,
                          fit_updated_at=fit_at, today=self.clock().date(), has_event=bool(evs), off_regime=off_regime)
        row.gate, row.gate_reason = g.verdict, g.text[:80]

    def _store_watch(self, watched: list) -> list[dict]:
        out: list[dict] = []
        for name, cfg, sym, bar_ts, close, w, rank, score in watched:
            if self.s.scalar(select(WatchRow.id).where(WatchRow.strategy == name, WatchRow.config_name == cfg,
                                                       WatchRow.symbol == sym, WatchRow.bar_ts == bar_ts)):
                continue
            self.s.add(WatchRow(strategy=name, config_name=cfg, symbol=sym, bar_ts=bar_ts, close=close,
                                trigger=w.trigger, distance_pct=w.distance_pct, note=w.note[:200], created_at=self.clock()))
            out.append({"symbol": sym, "strategy": name, "config_name": cfg, "trigger": w.trigger,
                        "distance_pct": w.distance_pct, "note": w.note, "rank": rank, "score": score})
        out.sort(key=lambda d: (d["rank"] != "proven", abs(d["distance_pct"]) if d["distance_pct"] is not None else 99.0))
        return out

    def regime_now(self) -> context.Regime:
        return context.compute_regime(self.provider, self.benchmark)

    def rank_for(self, strategy: str, config_name: str, symbol: str, timeframe: str = "1d") -> tuple[str, float, bool, str]:
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
        if row.quality == "bad":     # never open a trade from data we do not trust (the signal stays visible)
            row.status, row.reason = "rejected", "DATA_QUALITY"
            return
        if row.gate == "block":      # the gate holds it back: recorded and shown, but no order follows
            row.status, row.reason = "gated", (row.gate_reason.split(",")[0] or "GATE")
            return
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

    # ---------- strategy advice on open positions (EXIT / REDUCE; never sends an order) ----------
    def review_positions(self) -> list[PositionAlertRow]:
        """Ask each strategy whether the idea behind an open position still holds. Stops and targets keep closing
        positions on their own; this only tells a human when a strategy would get out earlier. Idempotent per bar."""
        catalog = registry.discover()
        created: list[PositionAlertRow] = []
        for pos in self.s.scalars(select(PositionRow).where(PositionRow.closed_at.is_(None))).all():
            cls = catalog.get(pos.strategy)
            if cls is None or Side(pos.side) is not Side.BUY:
                continue
            try:
                df = self.provider.get_bars(pos.symbol)
            except (FileNotFoundError, ValueError, KeyError):
                continue
            if df.index[-1].to_pydatetime() <= pos.signal_bar_ts:     # no bar since the entry signal
                continue
            strat = cls()
            prep = strat.prepare(df)
            hint = strat.exit_hint(prep, len(prep) - 1, pos.entry_price)
            if hint is None:
                continue
            bar_ts = df.index[-1].to_pydatetime()
            if self.s.scalar(select(PositionAlertRow.id).where(PositionAlertRow.position_id == pos.id,
                                                               PositionAlertRow.bar_ts == bar_ts,
                                                               PositionAlertRow.action == hint.action)):
                continue
            price = float(df["close"].iloc[-1])
            row = PositionAlertRow(position_id=pos.id, symbol=pos.symbol, strategy=pos.strategy, action=hint.action,
                                   reason=hint.reason[:200], price=price, bar_ts=bar_ts, created_at=self.clock())
            self.s.add(row)
            created.append(row)
            pnl = (price - pos.entry_price) * pos.qty
            self._alert("warning" if hint.action == "EXIT" else "info",
                        f"{pos.strategy} says {hint.action} {pos.symbol} (~{price:.2f})",
                        f"{hint.reason}. Open P&L about {pnl:+,.0f}. Your stop {pos.stop:.2f} is still in force; "
                        "nothing was sold. Decide yourself.")
        self.s.commit()
        return created
