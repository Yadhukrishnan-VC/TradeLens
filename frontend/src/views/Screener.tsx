import { useEffect, useRef, useState } from "react";
import { api, errText, useLoad } from "../api";
import { Badge } from "../components/Badge";
import { Flags } from "../components/Flags";
import { Notice, type NoticeState } from "../components/Notice";
import { inr, reasonText } from "../format";
import type { WatchItem } from "../types";
import type { LiveMatch } from "../types";

const clock = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
const canNotify = () => typeof Notification !== "undefined";

function state(m: LiveMatch) {
  if (m.confirmed) return <span className="badge gain">Confirmed</span>;
  return m.status === "live" ? <span className="badge wait">Live, provisional</span> : <span className="badge loss">Faded</span>;
}

export function Screener({ refreshKey }: { refreshKey: number }) {
  const data = useLoad(api.screener, [refreshKey], 30000);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);
  const [alerts, setAlerts] = useState(canNotify() && Notification.permission === "granted");
  const seen = useRef<Set<number> | null>(null);

  const status = data.data?.status;
  const strategies = data.data?.strategies ?? [];

  // browser alert for each newly appearing live match (free, no account needed)
  useEffect(() => {
    if (!data.data) return;
    const live = data.data.strategies.flatMap((s) => s.matches).filter((m) => m.status === "live");
    if (seen.current && alerts && canNotify()) {
      for (const m of live) {
        if (!seen.current.has(m.id)) {
          new Notification(`${m.symbol}: ${m.strategy}${m.config_name !== "default" ? ` [${m.config_name}]` : ""}`, {
            body: `Entry ${inr(m.entry, 2)}  Stop ${inr(m.stop, 2)}  Target ${inr(m.target, 2)}. Provisional.`,
          });
        }
      }
    }
    seen.current = new Set(live.map((m) => m.id));
  }, [data.data, alerts]);

  async function checkNow() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.runScreener(false);
      if (!r.ran) setNotice({ tone: "bad", text: r.reason ?? "Nothing was checked." });
      else setNotice({ tone: r.errors?.length ? "bad" : "ok", text: `Checked ${r.priced} priced stocks, ${r.new} new match${r.new === 1 ? "" : "es"}.${r.errors?.length ? " " + r.errors[0] : ""}` });
      data.reload();
    } catch (e) { setNotice({ tone: "bad", text: errText(e) }); } finally { setBusy(false); }
  }

  async function enableAlerts() {
    if (!canNotify()) { setNotice({ tone: "bad", text: "This browser does not support notifications." }); return; }
    setAlerts((await Notification.requestPermission()) === "granted");
  }

  const total = strategies.reduce((n, s) => n + s.matches.filter((m) => m.status === "live").length, 0);
  const r = status?.last_result;

  return (
    <>
      <section className="block">
        <h2>Live screener</h2>
        <p className="lede">
          During market hours (Mon to Fri, 9:15 to 15:30 IST) every stored stock is checked against every strategy, using free
          intraday prices to build today's bar so far. A match here is <strong>provisional</strong>: the bar is still forming, so it can fade
          before the close. Entry is the current price, and you exit at the target or the stop, whichever comes first. Nothing here places an
          order: the confirmed signal after the close goes to the Desk for your approval.
        </p>
        <div className="form-row">
          <span className={`badge ${status?.market_open ? "gain" : "wait"}`}>{status ? (status.market_open ? "Market open" : "Market closed") : "…"}</span>
          <span className="muted">
            {status?.enabled ? `Checks every ${Math.round(status.interval_seconds / 60)} min. ` : "Live checks are off (demo mode or LIVE_SCREENER=0). "}
            {status?.last_run ? `Last check ${clock(status.last_run)}${r ? `: ${r.priced} of ${r.symbols} stocks priced` : ""}.` : "Not checked yet today."}
            {status?.telegram ? " Telegram alerts on." : ""}
          </span>
          <button className="btn" onClick={checkNow} disabled={busy || !status?.enabled}>{busy ? "Checking…" : "Check now"}</button>
          {canNotify() && <button className="btn ghost" onClick={enableAlerts} disabled={alerts}>{alerts ? "Browser alerts on" : "Turn on browser alerts"}</button>}
        </div>
        <Notice notice={notice} onClose={() => setNotice(null)} />
        {(status?.breakers ?? []).filter((b) => b.state !== "closed").map((b) => (
          <p key={b.name} className="error-text">
            {b.name} is paused after repeated failures{b.last_error ? ` (${b.last_error})` : ""}. It will be tried again in about {Math.max(1, Math.ceil(b.retry_in_seconds / 60))} min.
          </p>
        ))}
        {r?.errors?.length ? <p className="error-text">{r.errors[0]}</p> : null}
        {data.error && <p className="error-text">{data.error}</p>}
        <p className="muted">{total} live match{total === 1 ? "" : "es"} right now across {strategies.length} strategies.</p>
      </section>

      <Watchlist refreshKey={refreshKey} />

      {strategies.map((s) => (
        <section className="block" key={s.name}>
          <h2>{s.name} <span className="muted">({s.matches.filter((m) => m.status === "live").length} live)</span></h2>
          <p className="lede">{s.description}</p>
          {s.matches.length === 0 ? <p className="empty">No stock matches right now.</p> : (
            <div className="scroll"><table>
              <thead><tr>
                <th>Stock</th><th>Settings</th><th>State</th><th>Track record</th><th className="num">Entry (buy near)</th>
                <th className="num">Stop loss (exit)</th><th className="num">Target (exit)</th><th className="num">Reward : risk</th>
                <th className="num">Risk / share</th><th className="num">Qty for your capital</th><th>Flags</th><th>First seen</th>
              </tr></thead>
              <tbody>{s.matches.map((m) => (
                <tr key={m.id}>
                  <td><strong>{m.symbol}</strong></td><td>{m.config_name === "default" ? "Built-in" : m.config_name}</td><td>{state(m)}</td>
                  <td><Badge kind={m.rank} /></td>
                  <td className="num">{inr(m.entry, 2)}</td><td className="num loss">{inr(m.stop, 2)}</td><td className="num gain">{inr(m.target, 2)}</td>
                  <td className="num">{m.rr ? `1 : ${m.rr.toFixed(1)}` : "–"}</td><td className="num">{inr(m.risk_per_share, 2)}</td>
                  <td className="num">{m.suggested_qty > 0 ? m.suggested_qty : <span className="muted">{reasonText(m.fit)}</span>}</td>
                  <td><Flags flags={m.flags} /></td><td>{clock(m.first_seen)}</td>
                </tr>))}</tbody>
            </table></div>
          )}
        </section>
      ))}
    </>
  );
}

function Watchlist({ refreshKey }: { refreshKey: number }) {
  const wl = useLoad(api.watchlist, [refreshKey], 60000);
  const items: WatchItem[] = wl.data?.items ?? [];
  return (
    <section className="block">
      <h2>Watchlist: close to triggering <span className="muted">({items.length})</span></h2>
      <p className="lede">Setups one step away from a signal, from the latest daily scan{wl.data?.as_of ? ` (bar of ${wl.data.as_of})` : ""}. Where there is a trigger
        level, a close beyond it on the next bar completes the setup. These are not signals yet and may never become one.</p>
      {wl.error && <p className="error-text">{wl.error}</p>}
      {items.length === 0 ? <p className="empty">Nothing is close to triggering. Run a scan on the Signals page.</p> : (
        <div className="scroll"><table className="compact">
          <thead><tr><th>Stock</th><th>Strategy</th><th>Track record</th><th className="num">Last close</th><th className="num">Trigger level</th><th className="num">Away</th><th>What to watch</th></tr></thead>
          <tbody>{items.map((w) => (
            <tr key={w.id}>
              <td><strong>{w.symbol}</strong></td><td>{w.strategy}{w.config_name !== "default" && <span className="muted"> · {w.config_name}</span>}</td>
              <td><Badge kind={w.rank} /></td><td className="num">{inr(w.close, 2)}</td><td className="num">{inr(w.trigger, 2)}</td>
              <td className="num">{w.distance_pct == null ? "–" : `${w.distance_pct > 0 ? "+" : ""}${w.distance_pct.toFixed(1)}%`}</td><td className="muted">{w.note}</td>
            </tr>))}</tbody>
        </table></div>
      )}
    </section>
  );
}
