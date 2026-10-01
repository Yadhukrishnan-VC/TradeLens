import type { AccountInfo, RiskConfig } from "../types";
import { inr, pnlClass, signedInr } from "../format";

const TICKS = 24;

function Meter({ label, used, cap, detail }: { label: string; used: number; cap: number; detail: string }) {
  const frac = cap > 0 ? Math.min(1, Math.max(0, used / cap)) : 0;
  const lit = Math.round(frac * TICKS);
  const level = frac >= 0.85 ? "hot" : frac >= 0.6 ? "warn" : "ok";
  return (
    <div className="meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(frac * 100)}>
      <div className="meter-head"><span>{label}</span><strong>{Math.round(frac * 100)}%</strong></div>
      <div className="ticks">
        {Array.from({ length: TICKS }, (_, i) => <i key={i} className={i < lit ? `tick ${level}` : "tick"} />)}
      </div>
      <div className="meter-detail">{detail}</div>
    </div>
  );
}

export function RiskStrip({ account, cfg }: { account: AccountInfo | null; cfg: RiskConfig | null }) {
  if (!account || !cfg) return <section className="risk skeleton" aria-busy="true"><p className="empty">Loading account…</p></section>;
  const exposureCap = cfg.max_exposure_pct * account.equity;
  const lossLimit = cfg.daily_loss_limit_pct * account.equity;
  const lossToday = Math.max(0, -account.realized_pnl_today);
  return (
    <section className={`risk ${account.kill_switch ? "halted" : ""}`} aria-label="Account and risk">
      <div className="risk-equity">
        <div className="risk-label">Account equity</div>
        <div className="risk-number">{inr(account.equity)}</div>
        <div className={`risk-today ${pnlClass(account.realized_pnl_today)}`}>
          {signedInr(account.realized_pnl_today)} realized today
        </div>
      </div>
      <div className="risk-meters">
        <Meter label="Capital deployed" used={account.exposure} cap={exposureCap} detail={`${inr(account.exposure)} of ${inr(exposureCap)} allowed`} />
        <Meter label="Loss today" used={lossToday} cap={lossLimit} detail={`${inr(lossToday)} of ${inr(lossLimit)} limit`} />
        <Meter label="Position slots" used={account.open_positions} cap={cfg.max_open_positions} detail={`${account.open_positions} of ${cfg.max_open_positions} in use`} />
      </div>
      {account.kill_switch && (
        <p className="risk-halt">Trading is halted. New entries are blocked. Open positions still exit at their stop or target when you run an exit check.</p>
      )}
    </section>
  );
}
