import { useState } from "react";
import { api, errText, useLoad } from "../api";
import { Notice, type NoticeState } from "../components/Notice";
import { Badge } from "../components/Badge";
import { compact, day, ratio } from "../format";
import type { TrackItem } from "../types";

const label = (t: TrackItem) => (t.config_name === "default" ? t.strategy : `${t.strategy} · ${t.config_name}`);
const pf = (v: number | null, trades: number) => (v == null ? (trades ? "no losses" : "–") : ratio(v));

export function TrackRecord({ refreshKey, onChange }: { refreshKey: number; onChange: () => void }) {
  const tr = useLoad(api.trackRecord, [refreshKey]);
  const [rr, setRr] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  async function testAll() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.backtestBatch(rr ? { rr: Number(rr) } : {});
      setNotice({ tone: "ok", text: `Tested ${r.ran} strategy and stock pairs (${r.skipped} skipped). ${r.proven} proven${r.config_name === "default" ? "" : ` at settings "${r.config_name}"`}.` });
    } catch (e) { setNotice({ tone: "bad", text: errText(e) }); } finally { setBusy(false); onChange(); }
  }
  const proven = tr.data?.proven ?? [];
  const rest = tr.data?.not_proven ?? [];

  return (
    <>
      <section className="block">
        <h2>Build the track record</h2>
        <p className="lede">Backtests every strategy on every stored stock in one go. Choose a reward-to-risk target to test that too: a 1:5 target
          is far harder to reach than 1:2, so most pairs will not hold up at it. That is exactly what this test shows. Can take a few minutes.</p>
        <div className="form-row">
          <label>Target
            <select value={rr} onChange={(e) => setRr(e.target.value)}>
              <option value="">Each strategy's own</option><option value="2">1 : 2</option><option value="3">1 : 3</option><option value="5">1 : 5</option>
            </select>
          </label>
          <button className="btn" onClick={testAll} disabled={busy}>{busy ? "Testing… this can take minutes" : "Test all strategies on all stocks"}</button>
        </div>
        <Notice notice={notice} onClose={() => setNotice(null)} />
      </section>

      <section className="block">
        <h2>What worked, on which stock</h2>
        <p className="lede">
          Every strategy and stock pair that passed the stability check: profitable with a profit factor above 1.2 in both the
          early and the late part of its tested history, with at least 30 trades. When a new signal or order matches one of
          these pairs it is marked <Badge kind="proven" /> and ranked above the rest. It still goes through the risk engine
          like everything else. This is evidence from the past, not a promise.
        </p>
        {tr.error && <p className="error-text">{tr.error}</p>}
        {proven.length === 0 ? (
          <p className="empty">Nothing proven yet. Run backtests on the Backtests page. A pair lands here once it holds up in both halves of its history.</p>
        ) : (
          <div className="scroll"><table>
            <thead><tr>
              <th>Stock</th><th>Strategy that worked</th><th>Tested from</th><th>Tested to</th><th>Last winning trade</th>
              <th className="num">Trades</th><th className="num">Profit factor</th><th className="num">Early / late</th><th className="num">Avg daily volume</th>
            </tr></thead>
            <tbody>{proven.map((t) => (
              <tr key={t.id}>
                <td><strong>{t.symbol}</strong></td><td>{label(t)}</td><td>{day(t.tested_from)}</td><td>{day(t.tested_to)}</td>
                <td>{t.last_win ? day(t.last_win) : "–"}</td><td className="num">{t.n_trades}</td>
                <td className="num gain">{pf(t.profit_factor, t.n_trades)}</td>
                <td className="num">{pf(t.early_pf, 1)} / {pf(t.late_pf, 1)}</td><td className="num">{compact(t.avg_volume)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </section>

      <section className="block">
        <h2>Tested, not proven</h2>
        <p className="lede">Did not hold up in both halves of history, or too few trades to judge. Signals from these are not ranked up.</p>
        {rest.length === 0 ? <p className="empty">Nothing else tested yet.</p> : (
          <div className="scroll"><table className="compact">
            <thead><tr><th>Stock</th><th>Strategy</th><th className="num">Trades</th><th className="num">Profit factor</th><th>Verdict</th></tr></thead>
            <tbody>{rest.map((t) => (
              <tr key={t.id}><td><strong>{t.symbol}</strong></td><td>{label(t)}</td><td className="num">{t.n_trades}</td>
                <td className="num">{pf(t.profit_factor, t.n_trades)}</td><td><Badge kind={t.verdict} /></td></tr>))}</tbody>
          </table></div>
        )}
      </section>
    </>
  );
}
