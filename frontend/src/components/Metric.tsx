import type { ReactNode } from "react";

export function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="metric">
      <div className={`metric-value ${tone ?? ""}`}>{value}</div>
      <div className="metric-label">{label}</div>
    </div>
  );
}
