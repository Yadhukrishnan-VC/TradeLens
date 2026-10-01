import type { Metrics } from "./types";

export const inr = (n: number | null | undefined, digits = 0) =>
  n == null ? "–" : "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const signedInr = (n: number, digits = 0) =>
  (n > 0 ? "+" : n < 0 ? "−" : "") + inr(Math.abs(n), digits);

export const pct = (n: number | null | undefined, digits = 1) => (n == null ? "–" : `${n.toFixed(digits)}%`);
export const ratio = (n: number | null | undefined) => (n == null ? "–" : n.toFixed(2));
export const profitFactor = (m: Metrics) => (m.n_trades === 0 ? "–" : m.profit_factor == null ? "no losses" : m.profit_factor.toFixed(2));

export const day = (iso: string) =>
  new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });

export const pnlClass = (n: number | null | undefined) => (n == null || n === 0 ? "" : n > 0 ? "gain" : "loss");
export const compact = (n: number) => n.toLocaleString("en-IN", { notation: "compact", maximumFractionDigits: 1 });

const REASONS: Record<string, string> = {
  KILL_SWITCH: "Trading is halted",
  ALREADY_IN_POSITION: "You already hold this symbol",
  INVALID_STOP: "The stop is on the wrong side of the entry",
  RR_TOO_LOW: "Reward is too small for the risk",
  DAILY_LOSS_LIMIT: "Today's loss limit has been reached",
  MAX_POSITIONS: "All position slots are in use",
  SIZE_ZERO_RISK_BUDGET: "The stop is too wide for your per-trade risk budget",
  SIZE_ZERO_CAPITAL: "Not enough capital for one share",
  STALE_SIGNAL: "A newer bar has arrived, so this signal is out of date",
  USER_REJECTED: "You rejected it",
  BROKER_REJECTED: "The broker rejected the order",
  stop: "Stop hit", stop_gap: "Gapped through the stop", target: "Target hit",
  target_gap: "Gapped past the target", end_of_data: "Closed at end of data",
};
export const reasonText = (code: string | null | undefined) => (code ? REASONS[code] ?? code : "");
