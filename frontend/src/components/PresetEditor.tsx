import { useState } from "react";
import { api, errText } from "../api";
import type { StrategyConfig, StrategyInfo } from "../types";

interface Props {
  strategy: StrategyInfo;
  preset?: StrategyConfig;          // the saved settings being edited; undefined = start from the built-in defaults
  onSaved: (name: string) => void;
  onDeleted: () => void;
  onError: (text: string) => void;
}

/** Edit a strategy's numbers and save them under a name. Remount (via `key`) when the strategy or preset changes. */
export function PresetEditor({ strategy, preset, onSaved, onDeleted, onError }: Props) {
  const start = { ...strategy.default_params, ...(preset?.params ?? {}) };
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(start).map(([k, v]) => [k, String(v)])),
  );
  const [name, setName] = useState(preset?.name ?? "");
  const [busy, setBusy] = useState(false);

  const parsed = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, Number(v)]));
  const invalid = Object.values(parsed).some((n) => !Number.isFinite(n) || n < 0);

  async function save() {
    setBusy(true);
    try {
      const saved = await api.saveStrategyConfig({ strategy: strategy.name, name: name.trim(), params: parsed });
      onSaved(saved.name);
    } catch (e) { onError(errText(e)); } finally { setBusy(false); }
  }

  async function remove() {
    if (!preset) return;
    setBusy(true);
    try { await api.deleteStrategyConfig(preset.id); onDeleted(); }
    catch (e) { onError(errText(e)); } finally { setBusy(false); }
  }

  return (
    <div className="preset">
      <div className="form-row">
        {Object.keys(strategy.default_params).map((k) => (
          <label key={k}>{k.replace(/_/g, " ")}
            <input type="number" step="any" min={0} value={values[k]}
              onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
          </label>
        ))}
      </div>
      <div className="form-row">
        <label>Save these settings as
          <input type="text" maxLength={64} placeholder="e.g. faster cross" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <button className="btn" onClick={save} disabled={busy || invalid || name.trim() === ""}>{preset ? "Save changes" : "Save settings"}</button>
        {preset && <button className="btn ghost" onClick={remove} disabled={busy}>Delete</button>}
      </div>
      <p className="muted">Saved settings are tested, ranked and scanned separately from the built-in ones. A new setting is "Unproven" until a backtest shows it holds up.</p>
    </div>
  );
}
