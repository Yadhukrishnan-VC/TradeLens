import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { inr } from "../format";

/** The strategies' own opinion about what you hold. Advice only: nothing is sold from here. */
export function PositionAdvice({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const alerts = useLoad(api.positionAlerts, [refreshKey]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const open = (alerts.data ?? []).filter((a) => !a.acknowledged);

  async function ask() {
    setBusy(true); setMsg(null);
    try {
      const made = await api.reviewPositions();
      setMsg(made.length ? `${made.length} new opinion${made.length === 1 ? "" : "s"}.` : "No strategy wants out of anything you hold.");
      alerts.reload(); onChange();
    } catch (e) { setMsg(errText(e)); } finally { setBusy(false); }
  }
  async function ack(id: number) {
    try { await api.ackPositionAlert(id); alerts.reload(); } catch (e) { setMsg(errText(e)); }
  }

  return (
    <section className="block">
      <div className="block-head">
        <h2>Strategy advice on open positions</h2>
        <button className="btn ghost" onClick={ask} disabled={busy}>{busy ? "Asking…" : "Ask the strategies now"}</button>
      </div>
      <p className="lede">When the idea behind an entry stops working, the strategy says EXIT or REDUCE. This is advice: your stop and target still
        close positions on their own, and nothing is sold from here. You decide.</p>
      {msg && <p className="muted">{msg}</p>}
      {open.length === 0 ? <p className="empty">No open advice.</p> : (
        <div className="scroll"><table className="compact">
          <thead><tr><th>Stock</th><th>Strategy says</th><th>Why</th><th className="num">Price then</th><th /></tr></thead>
          <tbody>{open.map((a) => (
            <tr key={a.id}>
              <td><strong>{a.symbol}</strong><div className="muted small">{a.strategy}</div></td>
              <td><span className={`badge ${a.action === "EXIT" ? "loss" : "wait"}`}>{a.action === "EXIT" ? "Exit" : "Reduce"}</span></td>
              <td className="muted">{a.reason}</td><td className="num">{inr(a.price, 2)}</td>
              <td><button className="btn ghost" onClick={() => ack(a.id)}>Got it</button></td>
            </tr>))}</tbody>
        </table></div>
      )}
    </section>
  );
}
