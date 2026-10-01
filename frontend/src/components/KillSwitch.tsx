import { useState } from "react";
import { api, errText } from "../api";
import type { AccountInfo } from "../types";

export function KillSwitch({ account, onChange, onError }: { account: AccountInfo | null; onChange: () => void; onError: (m: string) => void }) {
  const [busy, setBusy] = useState(false);
  if (!account) return null;
  const halted = account.kill_switch;

  async function toggle() {
    if (halted && !window.confirm("Resume trading? New entries will be allowed again.")) return;
    setBusy(true);
    try { await api.setKillSwitch(!halted); onChange(); }
    catch (e) { onError(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <div className={`kill ${halted ? "on" : ""}`}>
      <div className="kill-state">{halted ? "Trading halted" : "Trading allowed"}</div>
      <button className={halted ? "btn light" : "btn stop"} onClick={toggle} disabled={busy}>
        {halted ? "Resume trading" : "Halt all trading"}
      </button>
    </div>
  );
}
