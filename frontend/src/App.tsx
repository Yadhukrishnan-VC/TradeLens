import { useEffect, useState } from "react";
import { AUTH_EVENT, api, setToken, useLoad } from "./api";
import { KillSwitch } from "./components/KillSwitch";
import { Notice, type NoticeState } from "./components/Notice";
import { RiskStrip } from "./components/RiskStrip";
import { Backtests } from "./views/Backtests";
import { ContextBar } from "./components/ContextBar";
import { Data } from "./views/Data";
import { TrackRecord } from "./views/TrackRecord";
import { Desk } from "./views/Desk";
import { Positions } from "./views/Positions";
import { Screener } from "./views/Screener";
import { Signals } from "./views/Signals";
import { Strategies } from "./views/Strategies";

const VIEWS = ["Desk", "Screener", "Backtests", "Track Record", "Strategies", "Signals", "Positions", "Data"] as const;
type View = (typeof VIEWS)[number];

function TokenGate() {
  const [value, setValue] = useState("");
  return (
    <div className="app" style={{ display: "grid", placeItems: "center", minHeight: "100vh" }}>
      <form onSubmit={(e) => { e.preventDefault(); setToken(value.trim()); window.location.reload(); }} style={{ display: "grid", gap: 12, width: 360 }}>
        <div className="brand">tradelens</div>
        <label htmlFor="tok">API token</label>
        <input id="tok" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="the API_TOKEN from your .env" />
        <button type="submit" className="nav active" disabled={value.trim().length === 0}>Unlock</button>
        <p style={{ fontSize: 13 }}>Kept in this browser only. Generate one with <code>python -m tradelens token</code>.</p>
      </form>
    </div>
  );
}

export default function App() {
  const [needsToken, setNeedsToken] = useState(false);
  useEffect(() => {
    const on = () => { setToken(""); setNeedsToken(true); };
    window.addEventListener(AUTH_EVENT, on);
    return () => window.removeEventListener(AUTH_EVENT, on);
  }, []);
  return needsToken ? <TokenGate /> : <Dashboard />;
}

function Dashboard() {
  const [view, setView] = useState<View>("Desk");
  const [refreshKey, setRefreshKey] = useState(0);
  const [banner, setBanner] = useState<NoticeState>(null);
  const bump = () => setRefreshKey((k) => k + 1);

  const health = useLoad(api.health, []);
  const account = useLoad(api.account, [refreshKey], 15000);
  const cfg = useLoad(api.riskConfig, []);

  const serverDown = health.error ?? account.error;

  return (
    <div className="app">
      <aside className="rail">
        <div className="brand">tradelens</div>
        <nav aria-label="Main">
          {VIEWS.map((v) => (
            <button key={v} className={v === view ? "nav active" : "nav"} aria-current={v === view ? "page" : undefined} onClick={() => setView(v)}>{v}</button>
          ))}
        </nav>
        <div className="rail-foot">
          <div className="mode">
            Paper broker<br />
            <span>{health.data ? health.data.mode.replace("_", "-") : "…"} mode</span>
          </div>
          <KillSwitch account={account.data} onChange={bump} onError={(text) => setBanner({ tone: "bad", text })} />
        </div>
      </aside>

      <main className="main">
        {serverDown && <Notice notice={{ tone: "bad", text: serverDown }} />}
        {health.data?.demo && (
          <Notice notice={{ tone: "ok", text: "Demo data: prices are synthetic. Everything here shows the pipeline working, not a real edge." }} />
        )}
        <Notice notice={banner} onClose={() => setBanner(null)} />
        <RiskStrip account={account.data} cfg={cfg.data} />
        <ContextBar refreshKey={refreshKey} />
        <h1 className="page-title">{view}</h1>
        {view === "Desk" && <Desk refreshKey={refreshKey} onChange={bump} />}
        {view === "Backtests" && <Backtests refreshKey={refreshKey} onChange={bump} />}
        {view === "Screener" && <Screener refreshKey={refreshKey} />}
        {view === "Track Record" && <TrackRecord refreshKey={refreshKey} onChange={bump} />}
        {view === "Strategies" && <Strategies refreshKey={refreshKey} onChange={bump} />}
        {view === "Signals" && <Signals refreshKey={refreshKey} onChange={bump} serverMode={health.data?.mode ?? "semi_auto"} />}
        {view === "Positions" && <Positions refreshKey={refreshKey} onChange={bump} />}
        {view === "Data" && <Data refreshKey={refreshKey} onChange={bump} />}
      </main>
    </div>
  );
}
