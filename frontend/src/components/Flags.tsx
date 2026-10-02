import { reasonList } from "../format";

const FLAG: Record<string, string> = {
  STALE: "stale prices", GAP: "missing days", JUMP: "sudden price jump", OLD_JUMP: "past price jump",
  ZERO_VOLUME: "zero-volume day", LOW_LIQUIDITY: "thinly traded", OFF_REGIME: "market regime unsuited", NO_DATA: "no data",
};
const BAD = new Set(["STALE", "JUMP", "NO_DATA"]);
const label = (f: string) => (f.startsWith("EVENT:") ? `${f.slice(6)} soon` : FLAG[f] ?? f);

/** Plain-language chips for the data, event and regime flags on a signal or live match. */
export function Flags({ flags }: { flags: string | null | undefined }) {
  const list = (flags ?? "").split(",").filter(Boolean);
  if (list.length === 0) return <span className="muted">–</span>;
  return (
    <>{list.map((f) => (
      <span key={f} className={`badge ${BAD.has(f) ? "loss" : "wait"}`} style={{ marginRight: 4 }}>{label(f)}</span>
    ))}</>
  );
}

/** What the go/no-go gate decided, and why. */
export function Gate({ gate, reason }: { gate: string; reason: string }) {
  if (!gate || gate === "off") return <span className="muted">–</span>;
  const tone = gate === "pass" ? "gain" : gate === "warn" ? "wait" : "loss";
  const text = gate === "pass" ? "Passed" : gate === "warn" ? "Caution" : "Held back";
  return (
    <>
      <span className={`badge ${tone}`}>{text}</span>
      {reason && <div className="muted small">{reasonList(reason)}</div>}
    </>
  );
}
