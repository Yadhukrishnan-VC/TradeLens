import type { Trade } from "../types";
import { signedInr } from "../format";

export function EquityCurve({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) return <p className="empty">No trades to chart.</p>;
  const sorted = [...trades].sort((a, b) => a.exit_ts.localeCompare(b.exit_ts));
  const pts = [0];
  for (const t of sorted) pts.push(pts[pts.length - 1] + t.net_pnl);
  const W = 720, H = 200, P = 8;
  const lo = Math.min(0, ...pts), hi = Math.max(0, ...pts);
  const span = hi - lo || 1;
  const x = (i: number) => P + (i / (pts.length - 1 || 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - lo) / span) * (H - 2 * P);
  const path = pts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <figure className="curve">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Cumulative net profit after ${trades.length} trades: ${signedInr(last)}`}>
        <line x1={P} x2={W - P} y1={y(0)} y2={y(0)} className="curve-zero" />
        <path d={path} className={last >= 0 ? "curve-line gain" : "curve-line loss"} />
        <circle cx={x(pts.length - 1)} cy={y(last)} r="4" className={last >= 0 ? "curve-dot gain" : "curve-dot loss"} />
      </svg>
      <figcaption>Cumulative net profit after costs, trade by trade. Ends at <strong className={last >= 0 ? "gain" : "loss"}>{signedInr(last)}</strong>.</figcaption>
    </figure>
  );
}
