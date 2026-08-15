import React, { useState, useEffect, useCallback } from 'react';
import { ShieldCheck, Cpu, CheckCircle, XCircle } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import { fmtShort } from '../lib/time';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const CRITERIA_LABELS = {
  min_samples_150: '≥150 bewertete Entscheidungen',
  losers_blocked_ge_35: '≥35% der Verlierer geblockt',
  winners_blocked_le_15: '≤15% der Gewinner geblockt',
  uplift_ge_20: 'Ø-R-Uplift ≥ +20% (inkl. Fees)',
  brier_beats_baseline: 'Brier besser als Baseline',
};

/** Gate v1 (Shadow): Status, Kriterien-Ampel, Kalibrierung, Auto-Retrain. */
const GateShadowPanel = () => {
  const [status, setStatus] = useState(null);
  const [report, setReport] = useState(null);
  const [models, setModels] = useState([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, r, m] = await Promise.all([
        fetch(`${API_URL}/api/ml/gate/status`).then(res => res.json()),
        fetch(`${API_URL}/api/ml/gate/report?days=28`).then(res => res.json()),
        fetch(`${API_URL}/api/ml/gate/models?limit=6`).then(res => res.json()),
      ]);
      setStatus(s || null);
      setReport(r || null);
      setModels(m?.models || []);
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 30000);
    return () => clearInterval(iv);
  }, [load]);

  const saveSettings = async (updates) => {
    const res = await fetch(`${API_URL}/api/ml/gate/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(updates),
    });
    if (!res.ok) { toast.error('Admin-Login erforderlich'); return; }
    load();
  };

  const train = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ml/gate/train`, {
        method: 'POST', headers: authHeaders(),
      });
      if (res.status === 401 || res.status === 403) { toast.error('Admin-Login erforderlich'); return; }
      const data = await res.json();
      if (data.status === 'ok') toast.success(`Gate v${data.version} trainiert`);
      else toast.error(data.detail || 'Training fehlgeschlagen');
      load();
    } catch (e) { toast.error('Verbindungsfehler'); }
    finally { setBusy(false); }
  };

  const settings = status?.settings || {};
  const metrics = status?.metrics || {};
  const dataset = status?.dataset || {};
  const criteria = report?.criteria || {};
  const bins = metrics.calibration_bins || [];
  const allCriteria = Object.keys(CRITERIA_LABELS).every(k => criteria[k]);

  return (
    <div className="ai-lab-body" data-testid="gate-panel">
      <div className="gate-head-row">
        <span className="gate-mode-badge" data-testid="gate-mode-badge">
          <ShieldCheck size={12} weight="fill" /> SHADOW – blockt nichts, loggt nur
        </span>
        <button className="ai-action-btn" disabled={busy || status?.training_now}
          onClick={train} data-testid="gate-train-btn">
          <Cpu size={13} weight="bold" className={busy || status?.training_now ? 'spin' : ''} />
          {status?.training_now ? 'Trainiert…' : 'Gate jetzt trainieren'}
        </button>
      </div>

      <div className="ai-lab-meta" data-testid="gate-model-meta">
        {status?.model_loaded ? (
          <>Modell <b>v{status.version}</b> · trainiert {fmtShort(status.trained_at, '—')}
            {status.trigger ? ` · ${status.trigger}` : ''} ·
            {' '}<b>{dataset.samples ?? '—'}</b> Samples ({dataset.source === 'prod_readonly' ? 'Prod, nur lesend' : 'lokal'}, krypto-only) ·
            {' '}AUC <b>{metrics.oos_auc ?? '—'}</b> ·
            {' '}Brier kal. <b>{metrics.oos_brier_calibrated ?? '—'}</b> vs. Baseline <b>{metrics.baseline_brier ?? '—'}</b>
            {metrics.beats_baseline != null && (
              <span className={`gate-chip ${metrics.beats_baseline ? 'ok' : 'bad'}`}>
                {metrics.beats_baseline ? 'schlägt Baseline' : 'unter Baseline'}
              </span>
            )}
          </>
        ) : 'Noch kein Gate-Modell trainiert.'}
      </div>
      {status?.last_error && <div className="ai-lab-warn">⚠ {status.last_error}</div>}

      <div className="ai-lab-setup">
        <label title="Ab welcher Gewinnwahrscheinlichkeit würde das Gate durchlassen (nur Logging, blockt nicht)">
          <span>Schwelle p(win)</span>
          <select value={settings.threshold ?? 0.45}
            onChange={e => saveSettings({ threshold: Number(e.target.value) })}
            data-testid="gate-threshold-select">
            {[0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6].map(v => <option key={v} value={v}>{v.toFixed(2)}</option>)}
          </select>
        </label>
        <label className="ai-lab-check" title="Shadow-Predictions an jede KI-Entscheidung loggen">
          <span>Shadow-Logging</span>
          <input type="checkbox" checked={settings.shadow_enabled !== false}
            onChange={e => saveSettings({ shadow_enabled: e.target.checked })}
            data-testid="gate-shadow-toggle" />
        </label>
        <label className="ai-lab-check" title="Automatisch neu trainieren (täglich + nach neuen gelabelten Ergebnissen)">
          <span>Auto-Retrain</span>
          <input type="checkbox" checked={settings.auto_retrain !== false}
            onChange={e => saveSettings({ auto_retrain: e.target.checked })}
            data-testid="gate-autoretrain-toggle" />
        </label>
        <label title="Retrain, sobald so viele neue gelabelte Samples da sind">
          <span>Retrain ab neuen Samples</span>
          <select value={settings.retrain_min_new ?? 50}
            onChange={e => saveSettings({ retrain_min_new: Number(e.target.value) })}
            data-testid="gate-retrain-min-select">
            {[25, 50, 100, 200].map(v => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
      </div>

      <div className="ai-lab-sub">Aktivierungskriterien (rollierende 28 Tage)</div>
      <div className="gate-criteria" data-testid="gate-criteria">
        {Object.entries(CRITERIA_LABELS).map(([k, label]) => (
          <div key={k} className={`gate-crit ${criteria[k] ? 'ok' : 'bad'}`}
            data-testid={`gate-criteria-item-${k}`}>
            {criteria[k]
              ? <CheckCircle size={13} weight="fill" />
              : <XCircle size={13} weight="fill" />}
            <span>{label}</span>
          </div>
        ))}
      </div>
      <div className={`gate-activation-note ${allCriteria ? 'ok' : ''}`} data-testid="gate-activation-note">
        {allCriteria
          ? 'Alle Kriterien erfüllt – Aktivierung trotzdem NUR mit expliziter Freigabe des Users.'
          : 'Gate bleibt im Shadow-Modus, bis alle Kriterien über 4 Wochen erfüllt sind (+ User-Freigabe).'}
      </div>

      <div className="ai-lab-sub">Kontrafaktik: „Was hätte das Gate getan?“</div>
      {report?.evaluated > 0 ? (
        <div className="ai-lab-meta" data-testid="gate-report-stats">
          <b>{report.evaluated}</b> bewertete Entscheidungen ({report.wins} Gewinner / {report.losses} Verlierer) ·
          {' '}geblockte Verlierer <b>{report.pct_losers_blocked}%</b> ·
          {' '}geblockte Gewinner <b>{report.pct_winners_blocked}%</b> ·
          {' '}Ø-R alle <b>{report.avg_r_all ?? '—'}</b> → durchgelassen <b>{report.avg_r_passed ?? '—'}</b>
          {report.economic_uplift_pct != null && <> (Uplift <b>{report.economic_uplift_pct}%</b>)</>} ·
          {' '}Brier <b>{report.brier}</b> vs. Baseline <b>{report.baseline_brier}</b>
        </div>
      ) : (
        <div className="ai-lab-empty" data-testid="gate-report-empty">
          Noch keine bewerteten Shadow-Entscheidungen. Sobald der KI-Trader läuft (in Prod nach
          dem Deploy), bekommt jede LONG/SHORT-Entscheidung eine Gate-Prediction – nach dem
          Trade-Close wird hier ausgewertet, was das Gate geblockt hätte.
        </div>
      )}
      {(report?.threshold_sweep || []).length > 0 && (
        <table className="ai-lab-table gate-sweep" data-testid="gate-sweep-table">
          <thead>
            <tr><th>Schwelle</th><th>geblockt</th><th>Verlierer ✕</th><th>Gewinner ✕</th><th>Uplift</th></tr>
          </thead>
          <tbody>
            {report.threshold_sweep.map(s => (
              <tr key={s.threshold} className={s.threshold === report.threshold ? 'active' : ''}>
                <td>{s.threshold.toFixed(2)}</td>
                <td>{s.blocked}/{s.evaluated}</td>
                <td>{s.pct_losers_blocked}%</td>
                <td>{s.pct_winners_blocked}%</td>
                <td>{s.economic_uplift_pct != null ? `${s.economic_uplift_pct}%` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {bins.length > 0 && (
        <>
          <div className="ai-lab-sub">Kalibrierung (Out-of-Sample) – Vorhersage vs. Realität</div>
          <div className="gate-calibration" data-testid="gate-calibration">
            {bins.map(b => (
              <div className="gate-cal-row" key={b.bin}>
                <span className="gate-cal-label">{b.bin} <i>({b.n})</i></span>
                <span className="gate-cal-bars">
                  <span className="gate-cal-bar pred" style={{ width: `${Math.round(b.predicted * 100)}%` }}
                    title={`vorhergesagt ${Math.round(b.predicted * 100)}%`} />
                  <span className="gate-cal-bar actual" style={{ width: `${Math.round(b.actual * 100)}%` }}
                    title={`tatsächlich ${Math.round(b.actual * 100)}%`} />
                </span>
                <span className="gate-cal-val">{Math.round(b.predicted * 100)}% → {Math.round(b.actual * 100)}%</span>
              </div>
            ))}
            <div className="gate-cal-legend">
              <span><i className="gate-cal-dot pred" /> vorhergesagt</span>
              <span><i className="gate-cal-dot actual" /> tatsächliche Win-Rate</span>
            </div>
          </div>
        </>
      )}

      {models.length > 0 && (
        <>
          <div className="ai-lab-sub">Modell-Versionen (nie überschrieben)</div>
          <ul className="ai-lab-list" data-testid="gate-models-list">
            {models.map(m => (
              <li key={m.version}>
                <b>v{m.version}</b>
                <span className="ai-lab-ts"> · {fmtShort(m.trained_at, '—')} · {m.trigger || 'manuell'} ·
                  {' '}{m.samples} Samples · Brier kal. {m.metrics?.oos_brier_calibrated ?? '—'} · AUC {m.metrics?.oos_auc ?? '—'}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
};

export default GateShadowPanel;
