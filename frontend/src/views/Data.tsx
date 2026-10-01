import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { compact, day } from "../format";
import { Notice, type NoticeState } from "../components/Notice";
import type { FetchResult } from "../types";

export function Data({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const coverage = useLoad(api.coverage, [refreshKey]);
  const [symbols, setSymbols] = useState("");
  const [years, setYears] = useState(5);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<FetchResult[]>([]);
  const [notice, setNotice] = useState<NoticeState>(null);

  async function fetchNow() {
    setBusy(true);
    setNotice(null);
    try {
      const list = symbols.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
      const res = await api.fetchData({ symbols: list.length ? list : undefined, years });
      setResults(res);
      const ok = res.filter((r) => r.ok).length;
      setNotice({ tone: ok === res.length ? "ok" : "bad", text: `${ok} of ${res.length} symbols stored.` });
    } catch (e) {
      setNotice({ tone: "bad", text: errText(e) });
    } finally {
      setBusy(false);
      onChange();
    }
  }

  const rows = coverage.data ?? [];
  return (
    <>
      <section className="block">
        <h2>Fetch historical prices</h2>
        <p className="lede">Downloads daily prices (adjusted for splits and dividends) from Yahoo Finance, a free source, into the database.
          Leave symbols empty for a list of liquid NSE large caps. Fetching again refreshes and never duplicates. This can take a minute.</p>
        <div className="form-row">
          <label>NSE symbols (comma separated)
            <input type="text" placeholder="RELIANCE, TCS, INFY" value={symbols} onChange={(e) => setSymbols(e.target.value)} />
          </label>
          <label>Years of history
            <input type="number" min={1} max={25} step={1} value={years} onChange={(e) => setYears(Number(e.target.value))} />
          </label>
          <button className="btn" onClick={fetchNow} disabled={busy || years <= 0}>{busy ? "Fetching…" : "Fetch prices"}</button>
        </div>
        <Notice notice={notice} onClose={() => setNotice(null)} />
        {results.some((r) => !r.ok) && (
          <ul className="muted">{results.filter((r) => !r.ok).map((r) => <li key={r.symbol}>{r.symbol}: {r.error}</li>)}</ul>
        )}
      </section>

      <section className="block">
        <h2>Stored prices</h2>
        {coverage.error && <p className="error-text">{coverage.error}</p>}
        {rows.length === 0 ? <p className="empty">No prices stored yet. Fetch some above, or load CSV files with: python -m tradelite import-csv</p> : (
          <div className="scroll"><table className="compact">
            <thead><tr><th>Symbol</th><th>From</th><th>To</th><th className="num">Bars</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.symbol}><td><strong>{r.symbol}</strong></td><td>{day(r.start)}</td><td>{day(r.end)}</td><td className="num">{compact(r.bars)}</td></tr>))}</tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
