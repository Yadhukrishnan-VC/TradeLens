import { useCallback, useEffect, useState } from "react";
import type {
  AccountInfo, BacktestResponse, BacktestRun, Fit, Health, OrderRow, PositionRow, RiskConfig,
  ScanMode, SignalRow, StrategyConfig, StrategyInfo, TrackRecord, Coverage, FetchResult, ScreenerData, ScreenerRun, BatchResult,
  ContextData, WatchItem, PositionAlert, LifecycleRow, LifecycleState, EventItem,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

const TOKEN_KEY = "tradelens_api_token";
export const getToken = (): string => { try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; } };
export const setToken = (t: string) => { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ } };
export const AUTH_EVENT = "tradelens-auth-required";

export const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    const token = getToken();
    res = await fetch(path, {
      headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) }, ...init });
  } catch {
    throw new ApiError(0, "Can't reach the tradelens server. Start it with: uvicorn tradelens.api.main:create_app --factory");
  }
  if (res.status === 401) {
    window.dispatchEvent(new Event(AUTH_EVENT));
    throw new ApiError(401, "API token needed");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      const d = body.detail;
      detail = typeof d === "string" ? d : Array.isArray(d) ? d.map((x: { msg?: string }) => x.msg ?? "").join("; ") : detail;
    } catch { /* keep statusText */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

const post = <T,>(path: string, body?: unknown) =>
  req<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => req<Health>("/health"),
  symbols: () => req<string[]>("/symbols"),
  riskConfig: () => req<RiskConfig>("/risk-config"),
  strategies: () => req<StrategyInfo[]>("/strategies"),
  account: () => req<AccountInfo>("/account"),
  signals: () => req<SignalRow[]>("/signals"),
  orders: (status?: string) => req<OrderRow[]>(status ? `/orders?status=${status}` : "/orders"),
  positions: (open?: boolean) => req<PositionRow[]>(open === undefined ? "/positions" : `/positions?open=${open}`),
  fits: () => req<Fit[]>("/fits"),
  backtests: () => req<BacktestRun[]>("/backtests"),
  backtest: (id: number) => req<BacktestRun>(`/backtests/${id}`),
  runBacktest: (b: { strategy: string; symbol: string; capital: number; config_name?: string }) => post<BacktestResponse>("/backtests", b),
  strategyConfigs: () => req<StrategyConfig[]>("/strategy-configs"),
  saveStrategyConfig: (b: { strategy: string; name: string; params: Record<string, number> }) => post<StrategyConfig>("/strategy-configs", b),
  deleteStrategyConfig: (id: number) => req<{ deleted: number }>(`/strategy-configs/${id}`, { method: "DELETE" }),
  trackRecord: () => req<TrackRecord>("/track-record"),
  screener: () => req<ScreenerData>("/screener"),
  context: () => req<ContextData>("/context"),
  watchlist: () => req<{ as_of: string | null; items: WatchItem[] }>("/watchlist"),
  positionAlerts: () => req<PositionAlert[]>("/position-alerts"),
  reviewPositions: () => post<PositionAlert[]>("/position-alerts/review"),
  ackPositionAlert: (id: number) => post<PositionAlert>(`/position-alerts/${id}/ack`),
  lifecycle: () => req<LifecycleRow[]>("/lifecycle"),
  setLifecycle: (b: { strategy: string; config_name: string; state: LifecycleState; note?: string }) => post<unknown>("/lifecycle", b),
  events: () => req<EventItem[]>("/events"),
  addEvent: (b: { symbol: string; day: string; kind: string; note?: string }) => post<EventItem>("/events", b),
  importEvents: (csv: string) => post<{ imported: number; problems: string[] }>("/events/import", { csv }),
  deleteEvent: (id: number) => req<{ deleted: number }>(`/events/${id}`, { method: "DELETE" }),
  runScreener: (force = false) => post<ScreenerRun>(`/screener/run?force=${force}`, {}),
  backtestBatch: (b: { rr?: number }) => post<BatchResult>("/backtests/batch", b),
  coverage: () => req<Coverage[]>("/data/coverage"),
  fetchData: (b: { symbols?: string[]; years: number }) => post<FetchResult[]>("/data/fetch", b),
  scan: (b: { mode?: ScanMode; require_fit: boolean }) => post<SignalRow[]>("/scan", b),
  approve: (id: number) => post<OrderRow>(`/orders/${id}/approve`),
  reject: (id: number) => post<OrderRow>(`/orders/${id}/reject`),
  checkExits: () => post<PositionRow[]>("/exits/check"),
  setKillSwitch: (active: boolean) => post<{ kill_switch: boolean }>("/kill-switch", { active }),
};

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const load = useCallback(async () => {
    try { setData(await fn()); setError(null); }
    catch (e) { setError(errText(e)); }
    finally { setLoading(false); }
  }, deps);
  useEffect(() => {
    load();
    if (!pollMs) return;
    const id = setInterval(load, pollMs);
    return () => clearInterval(id);
  }, [load, pollMs]);
  return { data, error, loading, reload: load };
}
