import { useState } from "react";
import { api, useLoad } from "./api";
import { KillSwitch } from "./components/KillSwitch";
import { Notice, type NoticeState } from "./components/Notice";
import { RiskStrip } from "./components/RiskStrip";
import { Backtests } from "./views/Backtests";
import { Data } from "./views/Data";
import { TrackRecord } from "./views/TrackRecord";
import { Desk } from "./views/Desk";
import { Positions } from "./views/Positions";
import { Screener } from "./views/Screener";
import { Signals } from "./views/Signals";

const VIEWS = ["Desk", "Screener", "Backtests", "Track Record", "Signals", "Positions", "Data"] as const;
type View = (typeof VIEWS)[number];

export default function App() {
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
        <div className="brand">tradelite</div>
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
        <h1 className="page-title">{view}</h1>
        {view === "Desk" && <Desk refreshKey={refreshKey} onChange={bump} />}
        {view === "Backtests" && <Backtests refreshKey={refreshKey} onChange={bump} />}
        {view === "Screener" && <Screener refreshKey={refreshKey} />}
        {view === "Track Record" && <TrackRecord refreshKey={refreshKey} onChange={bump} />}
        {view === "Signals" && <Signals refreshKey={refreshKey} onChange={bump} serverMode={health.data?.mode ?? "semi_auto"} />}
        {view === "Positions" && <Positions refreshKey={refreshKey} onChange={bump} />}
        {view === "Data" && <Data refreshKey={refreshKey} onChange={bump} />}
      </main>
    </div>
  );
}
