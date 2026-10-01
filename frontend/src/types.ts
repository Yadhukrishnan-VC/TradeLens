export type Verdict = "candidate" | "no_edge" | "insufficient_data";

export type Rank = "proven" | "unproven";

export interface Health { status: string; mode: string; broker: string; demo: boolean; database?: string; data_source?: string }
export interface AccountInfo {
  equity: number; cash: number; open_positions: number; exposure: number;
  realized_pnl_today: number; open_symbols: string[]; kill_switch: boolean; starting_capital: number;
}
export interface RiskConfig {
  risk_pct: number; max_position_pct: number; max_exposure_pct: number;
  max_open_positions: number; daily_loss_limit_pct: number; min_rr: number;
}
export interface StrategyInfo {
  name: string; description: string; markets: string[]; timeframes: string[];
  min_bars: number; default_params: Record<string, number>;
}
export interface Metrics {
  n_trades: number; win_rate: number | null; profit_factor: number | null; expectancy: number | null;
  avg_win: number | null; avg_loss: number | null; net_pnl: number; total_costs: number;
  total_return_pct?: number; max_drawdown_pct?: number; verdict?: Verdict;
}
export interface BacktestResponse {
  run_id: number; verdict: Verdict; metrics: Metrics; early: Metrics; late: Metrics;
  skipped: Record<string, number>; n_trades: number; config_name?: string;
}
export interface Trade {
  symbol: string; strategy: string; side: string; entry_ts: string; entry_price: number;
  exit_ts: string; exit_price: number; qty: number; gross_pnl: number; costs: number;
  net_pnl: number; reason: string;
}
export interface BacktestRun {
  id: number; strategy: string; config_name?: string; symbol: string; timeframe: string; metrics: Metrics;
  created_at: string; trades?: Trade[];
}
export interface Fit {
  id: number; strategy: string; config_name?: string; symbol: string; timeframe: string; start: string; end: string;
  n_bars: number; avg_volume: number; n_trades: number; profit_factor: number | null;
  expectancy: number | null; verdict: Verdict;
}
export interface SignalRow {
  id: number; strategy: string; config_name: string; symbol: string; side: string; ts: string; entry: number; stop: number;
  target: number | null; status: string; reason: string | null; suggested_qty: number; rank: Rank; rank_score: number; created_at: string;
}
export interface OrderRow {
  id: number; signal_id: number; symbol: string; side: string; qty: number; price: number | null;
  status: string; mode: string; tag: string; reason: string | null; rank: Rank; rank_score: number; created_at: string;
}
export interface PositionRow {
  id: number; symbol: string; strategy: string; side: string; qty: number; entry_price: number;
  stop: number; target: number | null; entry_costs: number; opened_at: string; closed_at: string | null;
  exit_price: number | null; exit_reason: string | null; pnl: number | null;
}
export type ScanMode = "signal_only" | "semi_auto" | "auto";

export interface StrategyConfig { id: number; strategy: string; name: string; params: Record<string, number>; created_at: string }
export interface TrackItem {
  id: number; symbol: string; strategy: string; config_name: string; verdict: Verdict;
  tested_from: string; tested_to: string; n_bars: number; avg_volume: number; n_trades: number;
  profit_factor: number | null; expectancy: number | null; first_win: string | null; last_win: string | null;
  early_pf: number | null; late_pf: number | null; verified_at: string;
}
export interface TrackRecord { proven: TrackItem[]; not_proven: TrackItem[] }
export interface Coverage { symbol: string; bars: number; start: string; end: string }
export interface FetchResult { symbol: string; ok: boolean; bars?: number; start?: string; end?: string; error?: string }
