import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { Metric } from "../components/Metric";
import { Notice, type NoticeState } from "../components/Notice";
import { day, inr, pnlClass, reasonText, signedInr } from "../format";

export function Positions({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const all = useLoad(() => api.positions(), [refreshKey]);
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  async function checkExits() {
    setChecking(true);
    try {
      const closed = await api.checkExits();
      setNotice({ tone: "ok", text: closed.length ? `Closed ${closed.length} position${closed.length > 1 ? "s" : ""}.` : "No open position has hit its stop or target." });
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    } finally {
      setChecking(false);
      onChange();
    }
  }

  const rows = all.data ?? [];
  const open = rows.filter((p) => p.closed_at == null);
  const closed = rows.filter((p) => p.closed_at != null);
  const realized = closed.reduce((a, p) => a + (p.pnl ?? 0), 0);
  const wins = closed.filter((p) => (p.pnl ?? 0) > 0).length;

  return (
    <>
      <Notice notice={notice} onClose={() => setNotice(null)} />
      <section className="block">
        <div className="block-head">
          <h2>Results so far</h2>
          <button className="btn ghost" onClick={checkExits} disabled={checking || open.length === 0}>{checking ? "Checking…" : "Check exits"}</button>
        </div>
        {all.error && <p className="error-text">{all.error}</p>}
        <div className="metrics">
          <Metric label="Realized profit after costs" value={signedInr(realized)} tone={pnlClass(realized)} />
          <Metric label="Closed trades" value={closed.length} />
          <Metric label="Winners" value={closed.length ? `${wins} of ${closed.length}` : "–"} />
          <Metric label="Still open" value={open.length} />
        </div>
      </section>

      <section className="block">
        <h2>Closed trades</h2>
        {closed.length === 0 ? <p className="empty">No closed trades yet. Positions close automatically at their stop or target when you check exits.</p> : (
          <div className="scroll"><table>
            <thead><tr><th>Closed</th><th>Symbol</th><th>Strategy</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Exit</th><th>How it ended</th><th className="num">Profit after costs</th></tr></thead>
            <tbody>{closed.map((p) => (
              <tr key={p.id}>
                <td>{p.closed_at ? day(p.closed_at) : "–"}</td><td><strong>{p.symbol}</strong></td><td>{p.strategy}</td>
                <td className="num">{p.qty}</td><td className="num">{inr(p.entry_price, 2)}</td><td className="num">{inr(p.exit_price, 2)}</td>
                <td>{reasonText(p.exit_reason)}</td><td className={`num ${pnlClass(p.pnl)}`}>{p.pnl == null ? "–" : signedInr(p.pnl, 2)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
