import { api, useLoad } from "../api";

const TREND: Record<string, string> = { up: "Uptrend", down: "Downtrend", sideways: "Sideways", unknown: "Unknown" };
const VOL: Record<string, string> = { calm: "calm", normal: "normal volatility", volatile: "volatile", unknown: "" };

/** One line on every page: what the market is doing and how the gate is set. */
export function ContextBar({ refreshKey }: { refreshKey: number }) {
  const ctx = useLoad(api.context, [refreshKey], 60000);
  const c = ctx.data;
  if (!c) return ctx.error ? <p className="muted small">Market context unavailable: {ctx.error}</p> : null;
  const r = c.regime;
  const tone = r.trend === "up" ? "gain" : r.trend === "down" ? "loss" : "wait";
  return (
    <div className="form-row" aria-label="Market context" style={{ alignItems: "center", marginBottom: 8 }}>
      <span className={`badge ${tone}`}>Market: {TREND[r.trend] ?? r.trend}{VOL[r.vol] ? `, ${VOL[r.vol]}` : ""}</span>
      {r.breadth_pct != null && <span className="muted small">{r.breadth_pct.toFixed(0)}% of stocks above their 50-day average</span>}
      {r.trend === "unknown" && r.note && <span className="muted small">{r.note}</span>}
      <span className="muted small">Gate: {c.gate.mode}{c.gate.enforce_regime ? " + regime" : ""}</span>
      {c.portfolio.drawdown_pct < -0.05 && <span className="muted small">Drawdown {c.portfolio.drawdown_pct.toFixed(1)}%</span>}
    </div>
  );
}
