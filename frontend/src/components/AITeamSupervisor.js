import React, { useCallback, useEffect, useState } from 'react';
import { ShieldCheck, ArrowsClockwise, CheckCircle, ArrowUUpLeft } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const VERDICT_CLASS = {
  gut: 'ok',
  'auffällig': 'warn',
  schwach: 'bad',
  inaktiv: 'idle',
};

const ACTION_LABEL = {
  keine: 'kein Handlungsbedarf',
  modell_wechseln: 'Modellwechsel empfohlen',
  einstellungen_pruefen: 'Einstellungen prüfen',
  deaktivieren: 'Rolle deaktivieren',
};

const fmt = (ts) => {
  try {
    return new Date(ts).toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
      timeZone: 'Europe/Berlin',
    });
  } catch { return ''; }
};

/**
 * Aufsicht des Haupt-Modells über das KI-Team: manuell oder täglich automatisch
 * startbare Stichproben-Prüfung, Bericht je Rolle, Verlauf und – optional –
 * automatische Umschaltung schwacher Rollen auf ihre Fallback-KI (mit Rollback).
 */
const AITeamSupervisor = ({ roleLabels = {}, onApplyModel }) => {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState([]);
  const [showHistory, setShowHistory] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/ai/supervisor`).then(r => r.json());
      setState(data && typeof data === 'object' ? data : null);
      return data;
    } catch (e) { return null; }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/ai/supervisor/history?limit=10`)
        .then(r => r.json());
      setHistory(data.reports || []);
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Läuft eine Prüfung, alle 4s nachfragen bis der Bericht steht
  useEffect(() => {
    if (!busy && !state?.running) return undefined;
    const t = setInterval(async () => {
      const data = await load();
      if (data && !data.running) {
        setBusy(false);
        if (data.last_error) toast.error(data.last_error);
        else if (data.report) {
          toast.success(`Team-Prüfung fertig: ${data.report.roles?.length || 0} Rollen bewertet`);
          if (showHistory) loadHistory();
        }
      }
    }, 4000);
    return () => clearInterval(t);
  }, [busy, state?.running, load, loadHistory, showHistory]);

  const runReview = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/supervisor/review`, {
        method: 'POST', headers: authHeaders(),
      });
      const data = await res.json();
      if (!res.ok || (data.status !== 'started' && data.status !== 'busy')) {
        toast.error(data.detail || 'Team-Prüfung fehlgeschlagen');
        setBusy(false);
        return;
      }
      toast.message('Das Haupt-Modell prüft jetzt das KI-Team – das dauert ein bis zwei Minuten.');
      load();
    } catch (e) { toast.error('Verbindungsfehler'); setBusy(false); }
  };

  const saveSettings = async (patch) => {
    setState(s => ({ ...s, settings: { ...(s?.settings || {}), ...patch } }));
    try {
      const res = await fetch(`${API_URL}/api/ai/supervisor/settings`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error();
      load();
    } catch (e) { toast.error('Einstellung konnte nicht gespeichert werden'); load(); }
  };

  const rollback = async () => {
    try {
      const res = await fetch(`${API_URL}/api/ai/supervisor/rollback`, {
        method: 'POST', headers: authHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Rollback fehlgeschlagen');
      toast.success(`Umschaltung zurückgenommen: ${(data.restored || []).join(', ') || '—'}`);
      load();
    } catch (e) { toast.error(e.message); }
  };

  const toggleHistory = () => {
    setShowHistory(v => !v);
    if (!showHistory && !history.length) loadHistory();
  };

  const report = state?.report;
  const settings = state?.settings || {};
  const switches = state?.last_switches || [];

  return (
    <div className="ai-supervisor" data-testid="ai-supervisor-panel">
      <div className="ai-supervisor-head">
        <span className="ai-supervisor-title">
          <ShieldCheck size={14} weight="fill" /> Aufsicht des Haupt-Modells
          {report?.ts ? ` · zuletzt ${fmt(report.ts)}` : ''}
          {report?.trigger === 'auto' ? ' (automatisch)' : ''}
          {report?.model ? ` · ${report.model}` : ''}
        </span>
        <button className="ai-action-btn" onClick={runReview}
          disabled={busy || state?.running} data-testid="ai-supervisor-run-btn">
          <ArrowsClockwise size={13} weight="bold" className={busy || state?.running ? 'spin' : ''} />
          {busy || state?.running ? 'Prüft…' : 'KI-Team jetzt prüfen'}
        </button>
      </div>
      <div className="ai-team-hint">
        Das Haupt-Modell prüft stichprobenweise die Ausgaben jeder Rolle: arbeitet sie
        zuverlässig, ist die Qualität ausreichend oder sollte das Modell gewechselt werden?
      </div>
      <div className="ai-supervisor-settings">
        <label title="Prüft das KI-Team automatisch im gewählten Rhythmus">
          <input type="checkbox" checked={!!settings.auto_enabled}
            onChange={e => saveSettings({ auto_enabled: e.target.checked })}
            data-testid="ai-supervisor-auto-toggle" />
          automatisch prüfen
        </label>
        <label>
          alle
          <select value={settings.interval_hours || 24}
            onChange={e => saveSettings({ interval_hours: Number(e.target.value) })}
            data-testid="ai-supervisor-interval-select">
            {[6, 12, 24, 48, 72, 168].map(h => (
              <option key={h} value={h}>{h < 24 ? `${h} h` : `${h / 24} Tag(e)`}</option>
            ))}
          </select>
        </label>
        <label title="Rollen mit Urteil schwach werden automatisch kaskadierend umgestellt: erst auf Fallback 1, dann auf Fallback 2, zuletzt auf die Empfehlung der Aufsicht (protokolliert und umkehrbar)">
          <input type="checkbox" checked={!!settings.auto_switch}
            onChange={e => saveSettings({ auto_switch: e.target.checked })}
            data-testid="ai-supervisor-autoswitch-toggle" />
          bei „schwach“ auf Fallback 1 → 2 umschalten
        </label>
        <button className="ai-quick-tool" onClick={toggleHistory}
          title="Verlauf der Prüfberichte" data-testid="ai-supervisor-history-btn">
          Verlauf
        </button>
      </div>
      {switches.length > 0 && (
        <div className="ai-supervisor-switches" data-testid="ai-supervisor-switches">
          <span>
            Automatisch umgeschaltet: {switches.map(s => (
              `${roleLabels[s.role] || s.role}: ${s.from?.model || 'Haupt-Modell'} → ${s.to?.model}`
            )).join(' · ')}
          </span>
          <button className="ai-sup-apply" onClick={rollback}
            data-testid="ai-supervisor-rollback-btn">
            <ArrowUUpLeft size={12} weight="bold" /> Umschaltung zurücknehmen
          </button>
        </div>
      )}
      {state?.last_error && (
        <div className="ai-warning" data-testid="ai-supervisor-error">⚠ {state.last_error}</div>
      )}
      {report?.summary && (
        <div className="ai-supervisor-summary" data-testid="ai-supervisor-summary">
          {String(report.summary).replace(/\*\*/g, '').replace(/^#+\s*/gm, '')}
        </div>
      )}
      {(report?.roles || []).length > 0 && (
        <div className="ai-supervisor-rows">
          {report.roles.map(r => (
            <div className={`ai-supervisor-row ${VERDICT_CLASS[r.verdict] || 'ok'}`}
              key={r.role} data-testid={`ai-supervisor-row-${r.role}`}>
              <span className="ai-sup-role">{roleLabels[r.role] || r.role}</span>
              <span className={`ai-sup-verdict ${VERDICT_CLASS[r.verdict] || 'ok'}`}>
                {r.verdict} · {r.score}
              </span>
              <span className="ai-sup-reason">{r.reason}</span>
              <span className="ai-sup-action">{ACTION_LABEL[r.action] || r.action}</span>
              {r.suggested_model && (
                <button className="ai-sup-apply"
                  onClick={() => onApplyModel && onApplyModel(r.role, {
                    provider: r.suggested_provider, model: r.suggested_model,
                  })}
                  title={`Modell ${r.suggested_provider}/${r.suggested_model} für diese Rolle übernehmen`}
                  data-testid={`ai-supervisor-apply-${r.role}`}>
                  <CheckCircle size={12} weight="bold" /> {r.suggested_model}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {(report?.recommendations || []).length > 0 && (
        <ul className="ai-lesson-list" data-testid="ai-supervisor-recommendations">
          {report.recommendations.map((rec, i) => <li key={i}>{rec}</li>)}
        </ul>
      )}
      {showHistory && (
        <div className="ai-supervisor-history" data-testid="ai-supervisor-history">
          {history.length === 0 && <div className="ai-learn-empty">Noch kein Verlauf.</div>}
          {history.map((h, i) => (
            <div className="ai-supervisor-hist-item" key={h.id || i}>
              <b>{fmt(h.ts)}</b> · {h.trigger === 'auto' ? 'automatisch' : 'manuell'} · {h.model}
              {' · '}
              {(h.roles || []).filter(r => r.verdict !== 'gut').length} Auffälligkeiten
              {(h.switches || []).length > 0 &&
                ` · ${h.switches.length} Umschaltung(en)`}
              <div className="ai-sup-reason">{h.summary}</div>
            </div>
          ))}
        </div>
      )}
      {!report && (
        <div className="ai-learn-empty">
          Noch keine Prüfung gelaufen – starte sie über „KI-Team jetzt prüfen“.
        </div>
      )}
    </div>
  );
};

export default AITeamSupervisor;
