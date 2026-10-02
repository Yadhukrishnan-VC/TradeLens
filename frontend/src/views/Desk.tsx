import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { Badge } from "../components/Badge";
import { PositionAdvice } from "../components/PositionAdvice";
import { Notice, type NoticeState } from "../components/Notice";
import { day, inr, reasonText } from "../format";
import type { SignalRow } from "../types";

export function Desk({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const orders = useLoad(() => api.orders("PENDING_APPROVAL"), [refreshKey]);
  const signals = useLoad(() => api.signals(), [refreshKey]);
  const positions = useLoad(() => api.positions(true), [refreshKey]);
  const [busy, setBusy] = useState<number | null>(null);
  const [notice, setNotice] = useState<NoticeState>(null);
  const [checking, setChecking] = useState(false);

  const sigById = new Map<number, SignalRow>((signals.data ?? []).map((s) => [s.id, s]));

  async function decide(id: number, kind: "approve" | "reject") {
    setBusy(id);
    try {
      const o = kind === "approve" ? await api.approve(id) : await api.reject(id);
      if (o.status === "FILLED") {
        setNotice({ tone: "ok", text: `${o.side === "BUY" ? "Bought" : "Sold"} ${o.qty} ${o.symbol} at ${inr(o.price, 2)} (paper).` });
      } else if (o.status === "CANCELLED") {
        setNotice({ tone: "ok", text: `Rejected ${o.symbol}. Nothing was sent to the broker.` });
      } else {
        setNotice({ tone: "bad", text: `${o.symbol} was not placed: ${reasonText(o.reason)}.` });
      }
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    } finally {
      setBusy(null);
      onChange();
    }
  }

  async function checkExits() {
    setChecking(true);
    try {
      const closed = await api.checkExits();
      setNotice({
        tone: "ok",
        text: closed.length ? `Closed ${closed.length} position${closed.length > 1 ? "s" : ""}: ${closed.map((p) => `${p.symbol} (${reasonText(p.exit_reason)})`).join(", ")}.` : "No open position has hit its stop or target.",
      });
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    } finally {
      setChecking(false);
      onChange();
    }
  }

  const pending = orders.data ?? [];
  const open = positions.data ?? [];

  return (
    <>
      <Notice notice={notice} onClose={() => setNotice(null)} />

      <section className="block">
        <h2>Waiting for your approval</h2>
        {orders.error && <p className="error-text">{orders.error}</p>}
        {!orders.error && pending.length === 0 && (
          <p className="empty">Nothing to approve. Run a scan on the Signals page; in semi-auto mode, each approved-by-risk signal waits here for you.</p>
        )}
        <ul className="approvals">
          {pending.map((o) => {
            const s = sigById.get(o.signal_id);
            const risk = s ? o.qty * Math.abs(s.entry - s.stop) : null;
            const rr = s && s.target != null ? Math.abs(s.target - s.entry) / Math.abs(s.entry - s.stop) : null;
            return (
              <li key={o.id} className="approval">
                <div className="approval-main">
                  <h3>{o.side === "BUY" ? "Buy" : "Sell"} {o.qty} {o.symbol} <Badge kind={o.rank} /></h3>
                  <p className="muted">{s ? `${s.strategy} · signal from the ${day(s.ts)} close` : `Order #${o.id}`}</p>
                </div>
                {s && (
                  <dl className="levels">
                    <div><dt>Entry (about)</dt><dd>{inr(s.entry, 2)}</dd></div>
                    <div><dt>Stop</dt><dd className="loss">{inr(s.stop, 2)}</dd></div>
                    <div><dt>Target</dt><dd className="gain">{inr(s.target, 2)}</dd></div>
                    <div><dt>Risk if stopped</dt><dd>{inr(risk, 0)}</dd></div>
                    <div><dt>Reward : risk</dt><dd>{rr == null ? "–" : rr.toFixed(2)}</dd></div>
                  </dl>
                )}
                <div className="actions">
                  <button className="btn" disabled={busy === o.id} onClick={() => decide(o.id, "approve")}>{busy === o.id ? "Working…" : "Approve and place"}</button>
                  <button className="btn ghost" disabled={busy === o.id} onClick={() => decide(o.id, "reject")}>Reject</button>
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="block">
        <div className="block-head">
          <h2>Open positions</h2>
          <button className="btn ghost" onClick={checkExits} disabled={checking || open.length === 0}>{checking ? "Checking…" : "Check exits"}</button>
        </div>
        {positions.error && <p className="error-text">{positions.error}</p>}
        {open.length === 0 ? <p className="empty">No open positions.</p> : (
          <div className="scroll"><table>
            <thead><tr><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop</th><th className="num">Target</th><th className="num">Capital in trade</th></tr></thead>
            <tbody>{open.map((p) => (
              <tr key={p.id}>
                <td><strong>{p.symbol}</strong><div className="muted small">{p.strategy}</div></td>
                <td>{p.side}</td><td className="num">{p.qty}</td><td className="num">{inr(p.entry_price, 2)}</td>
                <td className="num loss">{inr(p.stop, 2)}</td><td className="num gain">{inr(p.target, 2)}</td>
                <td className="num">{inr(p.qty * p.entry_price)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </section>

      <PositionAdvice refreshKey={refreshKey} onChange={onChange} />

      <section className="block">
        <h2>Latest signals</h2>
        {signals.error && <p className="error-text">{signals.error}</p>}
        {(signals.data ?? []).length === 0 ? <p className="empty">No signals yet.</p> : (
          <div className="scroll"><table>
            <thead><tr><th>Bar</th><th>Symbol</th><th>Strategy</th><th>Status</th><th>Why</th></tr></thead>
            <tbody>{(signals.data ?? []).slice(0, 6).map((s) => (
              <tr key={s.id}><td>{day(s.ts)}</td><td><strong>{s.symbol}</strong></td><td>{s.strategy}</td><td><Badge kind={s.status} /></td><td className="muted">{reasonText(s.reason) || (s.suggested_qty ? `Sized at ${s.suggested_qty} shares` : "")}</td></tr>))}</tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
