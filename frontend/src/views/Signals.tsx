import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { Badge } from "../components/Badge";
import { Notice, type NoticeState } from "../components/Notice";
import { day, inr, reasonText } from "../format";
import type { ScanMode } from "../types";

export function Signals({ refreshKey, onChange, serverMode }: { refreshKey: number; onChange: () => void; serverMode: string }) {
  const signals = useLoad(() => api.signals(), [refreshKey]);
  const [mode, setMode] = useState<ScanMode | "">("");
  const [requireFit, setRequireFit] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  async function scan() {
    setScanning(true);
    try {
      const created = await api.scan({ mode: mode || undefined, require_fit: requireFit });
      const placed = created.filter((s) => s.status === "executed").length;
      const waiting = created.filter((s) => s.status === "proposed").length;
      setNotice({
        tone: "ok",
        text: created.length === 0
          ? "No new signals on the latest bars."
          : `${created.length} new signal${created.length > 1 ? "s" : ""}: ${waiting} waiting for approval, ${placed} placed, ${created.length - waiting - placed} recorded or rejected.`,
      });
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    } finally {
      setScanning(false);
      onChange();
    }
  }

  return (
    <>
      <section className="block">
        <h2>Scan for signals</h2>
        <p className="lede">Checks the latest bar of every symbol against every strategy. Each signal is run through the risk engine before anything is proposed.</p>
        <div className="form-row">
          <label>What happens to approved signals
            <select value={mode} onChange={(e) => setMode(e.target.value as ScanMode | "")}>
              <option value="">Server default ({serverMode.replace("_", "-")})</option>
              <option value="signal_only">Signal only: record, never create orders</option>
              <option value="semi_auto">Semi-auto: wait for my approval</option>
              <option value="auto">Auto: place immediately (paper broker)</option>
            </select>
          </label>
          <label className="check"><input type="checkbox" checked={requireFit} onChange={(e) => setRequireFit(e.target.checked)} />
            Only strategy and symbol pairs marked Candidate
          </label>
          <button className="btn" onClick={scan} disabled={scanning}>{scanning ? "Scanning…" : "Scan all symbols"}</button>
        </div>
        <Notice notice={notice} onClose={() => setNotice(null)} />
      </section>

      <section className="block">
        <h2>Signal history</h2>
        {signals.error && <p className="error-text">{signals.error}</p>}
        {(signals.data ?? []).length === 0 ? <p className="empty">No signals yet. Scan above; signals only appear when a strategy fires on the latest bar.</p> : (
          <div className="scroll"><table>
            <thead><tr><th>Bar</th><th>Symbol</th><th>Strategy</th><th className="num">Entry</th><th className="num">Stop</th><th className="num">Target</th><th className="num">Sized</th><th>Track record</th><th>Status</th><th>Why</th></tr></thead>
            <tbody>{(signals.data ?? []).map((s) => (
              <tr key={s.id}>
                <td>{day(s.ts)}</td><td><strong>{s.symbol}</strong></td><td>{s.strategy}{s.config_name !== "default" && <span className="muted"> · {s.config_name}</span>}</td>
                <td className="num">{inr(s.entry, 2)}</td><td className="num loss">{inr(s.stop, 2)}</td><td className="num gain">{inr(s.target, 2)}</td>
                <td className="num">{s.suggested_qty || "–"}</td><td><Badge kind={s.rank} /></td><td><Badge kind={s.status} /></td><td className="muted">{reasonText(s.reason)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
