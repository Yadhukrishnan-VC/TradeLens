import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { Badge } from "../components/Badge";
import { Notice, type NoticeState } from "../components/Notice";
import type { LifecycleRow, LifecycleState } from "../types";

const NEXT: Record<LifecycleState, { to: LifecycleState; label: string }[]> = {
  draft: [{ to: "retired", label: "Retire" }],
  validated: [{ to: "active", label: "Switch on" }, { to: "retired", label: "Retire" }],
  active: [{ to: "validated", label: "Pause" }, { to: "retired", label: "Retire" }],
  retired: [{ to: "draft", label: "Restore as draft" }],
};

export function Strategies({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const rows = useLoad(api.lifecycle, [refreshKey]);
  const [notice, setNotice] = useState<NoticeState>(null);

  async function move(r: LifecycleRow, to: LifecycleState) {
    try { await api.setLifecycle({ strategy: r.strategy, config_name: r.config_name, state: to }); setNotice(null); rows.reload(); onChange(); }
    catch (e) { setNotice({ tone: "bad", text: errText(e) }); }
  }

  return (
    <section className="block">
      <h2>Where each strategy stands</h2>
      <p className="lede">
        A new setting you save starts as a <strong>draft</strong>: backtest it, nothing more. When a backtest passes the stability check it becomes
        <strong> validated</strong> by itself and is scanned and screened. Only you switch it <strong>active</strong>, and only active strategies may
        create orders. <strong>Retired</strong> ones are never scanned. The built-in strategies start active.
      </p>
      <Notice notice={notice} onClose={() => setNotice(null)} />
      {rows.error && <p className="error-text">{rows.error}</p>}
      <div className="scroll"><table>
        <thead><tr><th>Strategy</th><th>Settings</th><th>State</th><th className="num">Proven / tested</th><th>Made for</th><th>Note</th><th /></tr></thead>
        <tbody>{(rows.data ?? []).map((r) => (
          <tr key={`${r.strategy}|${r.config_name}`}>
            <td><strong>{r.strategy}</strong></td><td>{r.config_name === "default" ? "Built-in" : r.config_name}</td>
            <td><Badge kind={r.state} /></td><td className="num">{r.proven} / {r.tested}</td>
            <td className="muted">{r.regimes.length ? r.regimes.map((x) => ({ up: "uptrend", down: "downtrend", sideways: "sideways" }[x] ?? x)).join(", ") : "any market"}</td>
            <td className="muted">{r.note}</td>
            <td>{NEXT[r.state].map((n) => <button key={n.to} className="btn ghost" style={{ marginRight: 6 }} onClick={() => move(r, n.to)}>{n.label}</button>)}</td>
          </tr>))}</tbody>
      </table></div>
    </section>
  );
}
