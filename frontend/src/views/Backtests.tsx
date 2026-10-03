import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { Badge } from "../components/Badge";
import { EquityCurve } from "../components/EquityCurve";
import { Metric } from "../components/Metric";
import { Notice, type NoticeState } from "../components/Notice";
import { PresetEditor } from "../components/PresetEditor";
import { compact, day, inr, pct, pnlClass, profitFactor, ratio, reasonText, signedInr } from "../format";
import type { Metrics, Trade, Verdict } from "../types";

interface Result {
  title: string; verdict: Verdict; metrics: Metrics; trades: Trade[];
  early?: Metrics; late?: Metrics; skipped?: Record<string, number>;
}

const VERDICT_HELP: Record<Verdict, string> = {
  candidate: "Profitable with a profit factor above 1.2 in both the early and the late part of the history, with at least 30 trades. Worth paper trading. This is a stability check, not proof of an edge.",
  no_edge: "Enough trades, but it did not hold up in both halves of the history.",
  insufficient_data: "Fewer than 30 trades. Too little evidence either way. Try more history or more symbols.",
};

function SegmentRow({ label, m }: { label: string; m: Metrics }) {
  return (
    <tr>
      <td>{label}</td><td className="num">{m.n_trades}</td><td className="num">{pct(m.win_rate == null ? null : m.win_rate * 100, 0)}</td>
      <td className="num">{profitFactor(m)}</td><td className={`num ${pnlClass(m.expectancy)}`}>{m.expectancy == null ? "–" : signedInr(m.expectancy)}</td>
      <td className={`num ${pnlClass(m.net_pnl)}`}>{signedInr(m.net_pnl)}</td>
    </tr>
  );
}

export function Backtests({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const strategies = useLoad(api.strategies, []);
  const symbols = useLoad(api.symbols, []);
  const fits = useLoad(api.fits, [refreshKey]);
  const runs = useLoad(api.backtests, [refreshKey]);
  const configs = useLoad(api.strategyConfigs, [refreshKey]);
  const [config, setConfig] = useState("default");
  const [strategy, setStrategy] = useState("");
  const [symbol, setSymbol] = useState("");
  const [capital, setCapital] = useState(100000);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [notice, setNotice] = useState<NoticeState>(null);

  const stratList = strategies.data ?? [];
  const symList = symbols.data ?? [];
  const chosenStrategy = stratList.find((s) => s.name === (strategy || stratList[0]?.name));
  const strategyName = chosenStrategy?.name ?? "";
  const symbolName = symbol || symList[0] || "";
  const presetList = (configs.data ?? []).filter((c) => c.strategy === strategyName);
  const activePreset = presetList.find((c) => c.name === config);
  const configName = activePreset ? activePreset.name : "default";
  const shown = (name: string) => (!name || name === "default" ? "" : ` (${name})`);

  async function run() {
    setRunning(true);
    setNotice(null);
    try {
      const res = await api.runBacktest({ strategy: strategyName, symbol: symbolName, capital, config_name: configName });
      const full = await api.backtest(res.run_id);
      setResult({ title: `${strategyName}${shown(configName)} on ${symbolName}`, verdict: res.verdict, metrics: res.metrics, trades: full.trades ?? [], early: res.early, late: res.late, skipped: res.skipped });
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    } finally {
      setRunning(false);
      onChange();
    }
  }

  async function openRun(id: number) {
    try {
      const r = await api.backtest(id);
      setResult({ title: `${r.strategy}${shown(r.config_name ?? "")} on ${r.symbol}`, verdict: (r.metrics.verdict ?? "insufficient_data") as Verdict, metrics: r.metrics, trades: r.trades ?? [] });
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    }
  }

  const m = result?.metrics;

  return (
    <>
      <section className="block">
        <h2>Run a backtest</h2>
        {symbols.error && <p className="error-text">{symbols.error}</p>}
        {!symbols.error && symList.length === 0 && !symbols.loading && (
          <p className="empty">No symbols found. Open the Data page and fetch prices, or start the server with TRADELENS_DEMO=1.</p>
        )}
        <div className="form-row">
          <label>Strategy
            <select value={strategyName} onChange={(e) => { setStrategy(e.target.value); setConfig("default"); }}>
              {stratList.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select>
          </label>
          <label>Settings
            <select value={configName} onChange={(e) => setConfig(e.target.value)}>
              <option value="default">Built-in</option>
              {presetList.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
            </select>
          </label>
          <label>Symbol
            <select value={symbolName} onChange={(e) => setSymbol(e.target.value)}>
              {symList.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label>Starting capital (₹)
            <input type="number" min={1000} step={1000} value={capital} onChange={(e) => setCapital(Number(e.target.value))} />
          </label>
          <button className="btn" onClick={run} disabled={running || !strategyName || !symbolName || capital <= 0}>{running ? "Running…" : "Run backtest"}</button>
        </div>
        {chosenStrategy && <p className="muted">{chosenStrategy.description} Needs at least {chosenStrategy.min_bars} bars of history.</p>}
        <Notice notice={notice} onClose={() => setNotice(null)} />
        {chosenStrategy && (
          <details>
            <summary>Customise this strategy's settings</summary>
            <PresetEditor key={`${strategyName}|${configName}`} strategy={chosenStrategy} preset={activePreset}
              onSaved={(name) => { setConfig(name); configs.reload(); onChange(); }}
              onDeleted={() => { setConfig("default"); configs.reload(); onChange(); }}
              onError={(text) => setNotice({ tone: "bad", text })} />
          </details>
        )}
      </section>

      {result && m && (
        <section className="block result">
          <div className="block-head">
            <h2>{result.title}</h2>
            <Badge kind={result.verdict} />
          </div>
          <p className="lede">{VERDICT_HELP[result.verdict]}</p>
          <div className="metrics">
            <Metric label="Trades" value={m.n_trades} />
            <Metric label="Win rate" value={pct(m.win_rate == null ? null : m.win_rate * 100, 0)} />
            <Metric label="Profit factor" value={profitFactor(m)} />
            <Metric label="Average per trade" value={m.expectancy == null ? "–" : signedInr(m.expectancy)} tone={pnlClass(m.expectancy)} />
            <Metric label="Net profit after costs" value={signedInr(m.net_pnl)} tone={pnlClass(m.net_pnl)} />
            <Metric label="Costs paid" value={inr(m.total_costs)} />
            {m.total_return_pct != null && <Metric label="Return" value={pct(m.total_return_pct)} tone={pnlClass(m.total_return_pct)} />}
            {m.max_drawdown_pct != null && <Metric label="Worst drawdown" value={pct(m.max_drawdown_pct)} tone="loss" />}
          </div>

          <EquityCurve trades={result.trades} />

          {result.early && result.late && (
            <div className="scroll"><table className="compact">
              <caption>Does it hold up in both halves of the history?</caption>
              <thead><tr><th>Period</th><th className="num">Trades</th><th className="num">Win rate</th><th className="num">Profit factor</th><th className="num">Avg per trade</th><th className="num">Net profit</th></tr></thead>
              <tbody><SegmentRow label="First 70%" m={result.early} /><SegmentRow label="Last 30%" m={result.late} /></tbody>
            </table></div>
          )}

          {result.skipped && Object.keys(result.skipped).length > 0 && (
            <p className="muted">Signals that were not traded: {Object.entries(result.skipped).map(([k, v]) => `${reasonText(k)} (${v})`).join(", ")}.</p>
          )}

          <details>
            <summary>{result.trades.length} trades</summary>
            <div className="scroll"><table className="compact">
              <thead><tr><th>Entered</th><th>Exited</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Exit</th><th>How it ended</th><th className="num">Net</th></tr></thead>
              <tbody>{result.trades.slice(0, 200).map((t, i) => (
                <tr key={i}><td>{day(t.entry_ts)}</td><td>{day(t.exit_ts)}</td><td className="num">{t.qty}</td><td className="num">{inr(t.entry_price, 2)}</td><td className="num">{inr(t.exit_price, 2)}</td><td>{reasonText(t.reason)}</td><td className={`num ${pnlClass(t.net_pnl)}`}>{signedInr(t.net_pnl)}</td></tr>))}</tbody>
            </table></div>
          </details>
        </section>
      )}

      <section className="block">
        <h2>Strategy map</h2>
        <p className="lede">Which strategy has been tested on which symbol, over what history and volume, and how it held up.</p>
        {fits.error && <p className="error-text">{fits.error}</p>}
        {(fits.data ?? []).length === 0 ? <p className="empty">Nothing tested yet. Run a backtest above and it appears here.</p> : (
          <div className="scroll"><table>
            <thead><tr><th>Strategy</th><th>Settings</th><th>Symbol</th><th>History</th><th className="num">Bars</th><th className="num">Avg daily volume</th><th className="num">Trades</th><th className="num">Profit factor</th><th>Verdict</th></tr></thead>
            <tbody>{(fits.data ?? []).map((f) => (
              <tr key={f.id}>
                <td>{f.strategy}</td><td>{f.config_name && f.config_name !== "default" ? f.config_name : "Built-in"}</td><td><strong>{f.symbol}</strong></td><td>{day(f.start)} to {day(f.end)}</td>
                <td className="num">{f.n_bars}</td><td className="num">{compact(f.avg_volume)}</td><td className="num">{f.n_trades}</td>
                <td className="num">{f.profit_factor == null ? (f.n_trades ? "no losses" : "–") : ratio(f.profit_factor)}</td><td><Badge kind={f.verdict} /></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </section>

      <section className="block">
        <h2>Past runs</h2>
        {runs.error && <p className="error-text">{runs.error}</p>}
        {(runs.data ?? []).length === 0 ? <p className="empty">No runs yet.</p> : (
          <div className="scroll"><table className="compact">
            <thead><tr><th>Run</th><th>Strategy</th><th>Symbol</th><th className="num">Trades</th><th className="num">Net profit</th><th>Verdict</th><th /></tr></thead>
            <tbody>{(runs.data ?? []).slice(0, 15).map((r) => (
              <tr key={r.id}>
                <td>#{r.id}</td><td>{r.strategy}{shown(r.config_name ?? "")}</td><td><strong>{r.symbol}</strong></td><td className="num">{r.metrics.n_trades}</td>
                <td className={`num ${pnlClass(r.metrics.net_pnl)}`}>{signedInr(r.metrics.net_pnl)}</td>
                <td>{r.metrics.verdict ? <Badge kind={r.metrics.verdict} /> : "–"}</td>
                <td><button className="link" onClick={() => openRun(r.id)}>Open</button></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
