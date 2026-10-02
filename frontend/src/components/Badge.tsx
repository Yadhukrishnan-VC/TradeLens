const LABEL: Record<string, string> = {
  candidate: "Candidate", no_edge: "No edge", insufficient_data: "Too few trades",
  signal: "Signal", proposed: "Awaiting you", executed: "Executed", rejected: "Rejected", new: "New",
  PENDING_APPROVAL: "Awaiting you", FILLED: "Filled", REJECTED: "Rejected", CANCELLED: "Cancelled",
  proven: "Proven", unproven: "Unproven", gated: "Held back",
  draft: "Draft", validated: "Validated", active: "Active", retired: "Retired",
};
const TONE: Record<string, string> = {
  candidate: "gain", executed: "gain", FILLED: "gain", proven: "gain",
  no_edge: "loss", rejected: "loss", REJECTED: "loss",
  insufficient_data: "wait", proposed: "wait", PENDING_APPROVAL: "wait", gated: "wait", validated: "wait", active: "gain",
};

export function Badge({ kind }: { kind: string }) {
  return <span className={`badge ${TONE[kind] ?? "neutral"}`}>{LABEL[kind] ?? kind}</span>;
}
