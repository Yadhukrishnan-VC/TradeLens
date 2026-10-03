import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { compact, day } from "../format";
import { Notice, type NoticeState } from "../components/Notice";
import type { EventItem, FetchResult } from "../types";

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
        {rows.length === 0 ? <p className="empty">No prices stored yet. Fetch some above, or load CSV files with: python -m tradelens import-csv</p> : (
          <div className="scroll"><table className="compact">
            <thead><tr><th>Symbol</th><th>From</th><th>To</th><th className="num">Bars</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.symbol}><td><strong>{r.symbol}</strong></td><td>{day(r.start)}</td><td>{day(r.end)}</td><td className="num">{compact(r.bars)}</td></tr>))}</tbody>
          </table></div>
        )}
      </section>
      <Events refreshKey={refreshKey} onChange={onChange} />
    </>
  );
}

function Events({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const list = useLoad(api.events, [refreshKey]);
  const [symbol, setSymbol] = useState("");
  const [dayValue, setDayValue] = useState("");
  const [kind, setKind] = useState("earnings");
  const [csv, setCsv] = useState("");
  const [notice, setNotice] = useState<NoticeState>(null);

  async function add() {
    try {
      await api.addEvent({ symbol: symbol.trim() || "*", day: dayValue, kind });
      setSymbol(""); setNotice(null); list.reload(); onChange();
    } catch (e) { setNotice({ tone: "bad", text: errText(e) }); }
  }
  async function importCsv() {
    try {
      const r = await api.importEvents(csv);
      setNotice({ tone: r.problems.length ? "bad" : "ok", text: `${r.imported} imported${r.problems.length ? `. Skipped: ${r.problems.join("; ")}` : "."}` });
      setCsv(""); list.reload(); onChange();
    } catch (e) { setNotice({ tone: "bad", text: errText(e) }); }
  }
  async function remove(e: EventItem) {
    try { await api.deleteEvent(e.id); list.reload(); onChange(); } catch (err) { setNotice({ tone: "bad", text: errText(err) }); }
  }

  return (
    <section className="block">
      <h2>Event calendar</h2>
      <p className="lede">Results, board meetings or policy days make a new trade riskier: a breakout the day before results is a coin flip with a gap
        attached. A signal with an event within a couple of days is flagged, and held back when the gate is enforcing. Use symbol <code>*</code> for the whole market.
        Free lookups are patchy, so an empty calendar does not mean no events. Add the ones you know.</p>
      <div className="form-row">
        <label>Symbol (or * for all)<input type="text" value={symbol} placeholder="TCS" onChange={(e) => setSymbol(e.target.value)} /></label>
        <label>Date<input type="date" value={dayValue} onChange={(e) => setDayValue(e.target.value)} /></label>
        <label>Kind<input type="text" value={kind} onChange={(e) => setKind(e.target.value)} /></label>
        <button className="btn" onClick={add} disabled={!dayValue}>Add event</button>
      </div>
      <details>
        <summary>Import a CSV (symbol,date,kind,note)</summary>
        <textarea rows={4} style={{ width: "100%" }} value={csv} onChange={(e) => setCsv(e.target.value)} placeholder={"TCS,2026-10-09,earnings\n*,2026-12-05,policy"} />
        <button className="btn ghost" onClick={importCsv} disabled={!csv.trim()}>Import</button>
      </details>
      <Notice notice={notice} onClose={() => setNotice(null)} />
      {(list.data ?? []).length === 0 ? <p className="empty">No upcoming events.</p> : (
        <div className="scroll"><table className="compact">
          <thead><tr><th>Date</th><th>Symbol</th><th>Kind</th><th>Source</th><th /></tr></thead>
          <tbody>{(list.data ?? []).map((e) => (
            <tr key={e.id}><td>{day(e.day)}</td><td><strong>{e.symbol === "*" ? "Whole market" : e.symbol}</strong></td><td>{e.kind}</td><td className="muted">{e.source}</td>
              <td><button className="btn ghost" onClick={() => remove(e)}>Remove</button></td></tr>))}</tbody>
        </table></div>
      )}
    </section>
  );
}
