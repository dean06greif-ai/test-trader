import React, { useState, useEffect, useCallback } from 'react';
import { Flask, Brain, ChartLine, Database, ArrowsClockwise, Lightbulb, Cpu, Trash } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import { fmtShort } from '../lib/time';
import GateShadowPanel from './GateShadowPanel';
import './AILabPanel.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmt = (ts) => fmtShort(ts, '—');
const pct = (v) => (v === null || v === undefined ? '—' : `${v}%`);

/**
 * KI-Labor: Forschungs-Analyst (Backtest-/Optimizer-Auswertung), ML-Labor
 * (Optuna + XGBoost), Markt-Beobachter und KI-Gedächtnis (MongoDB/Supabase).
 * Liest ausschließlich die neuen /api/ai/... Lab-Endpunkte – der bestehende
 * KI-Trader-Flow bleibt unberührt.
 */
const AILabPanel = () => {
  const [lab, setLab] = useState(null);
  const [report, setReport] = useState(null);
  const [entries, setEntries] = useState([]);
  const [busy, setBusy] = useState('');
  const [tab, setTab] = useState('research');
  const [health, setHealth] = useState(null);
  const [trades, setTrades] = useState([]);
  const [actions, setActions] = useState([]);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([
        fetch(`${API_URL}/api/ai/lab/status`).then(res => res.json()),
        fetch(`${API_URL}/api/ai/research/report`).then(res => res.json()),
      ]);
      setLab(s || null);
      setReport(r?.report || null);
    } catch (e) { /* silent */ }
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      const d = await fetch(`${API_URL}/api/ai/memory/stats?health=true`).then(r => r.json());
      setHealth(d?.mirror || null);
    } catch (e) { /* silent */ }
  }, []);

  const loadEntries = useCallback(async () => {
    try {
      const d = await fetch(`${API_URL}/api/ai/memory/entries?limit=25`).then(r => r.json());
      setEntries(d.entries || []);
    } catch (e) { /* silent */ }
  }, []);

  const loadTrades = useCallback(async () => {
    try {
      const [t, a] = await Promise.all([
        fetch(`${API_URL}/api/autotrade/trades?status=open`).then(r => r.json()),
        fetch(`${API_URL}/api/ai/trade/status?limit=15`).then(r => r.json()),
      ]);
      setTrades(t.trades || []);
      setActions(a.actions || []);
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => {
    load(); loadEntries(); loadHealth();
    const iv = setInterval(load, 20000);
    return () => clearInterval(iv);
  }, [load, loadEntries, loadHealth]);

  const post = async (path, label, body) => {
    setBusy(label);
    try {
      const res = await fetch(`${API_URL}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (res.status === 401) { toast.error('Admin-Login erforderlich'); return null; }
      const data = await res.json();
      if (data.status === 'ok') toast.success(`${label} fertig`);
      else if (data.status === 'no_data' || data.status === 'insufficient_data') toast.warning(data.detail);
      else if (data.status === 'unavailable') toast.error(data.detail);
      else if (data.detail) toast.error(data.detail);
      load(); loadEntries();
      return data;
    } catch (e) {
      toast.error('Verbindungsfehler');
      return null;
    } finally { setBusy(''); }
  };

  const research = lab?.research || {};
  const ml = lab?.ml || {};
  const model = ml.model || null;
  const observer = lab?.observer || {};
  const mem = lab?.memory || {};
  const mlSettings = ml.settings || {};
  const tm = lab?.trade_manager || {};
  const tmSettings = tm.settings || {};
  const cl = lab?.closed_loop || {};
  const clSettings = cl.settings || {};

  const saveJson = async (path, updates) => {
    const res = await fetch(`${API_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(updates),
    });
    if (!res.ok) { toast.error('Admin-Login erforderlich'); return; }
    load();
  };

  const tradeAction = async (tradeId, action, extra = {}) => {
    setBusy(action);
    try {
      const res = await fetch(`${API_URL}/api/ai/trade/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ trade_id: tradeId, action, source: 'manuell', ...extra }),
      });
      const data = await res.json();
      if (data.status === 'ok') toast.success(`${action} ausgeführt`);
      else toast.error(data.detail || 'Aktion abgelehnt');
      loadTrades();
    } catch (e) { toast.error('Verbindungsfehler'); }
    finally { setBusy(''); }
  };

  const saveMl = async (updates) => {
    const res = await fetch(`${API_URL}/api/ai/ml/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(updates),
    });
    if (!res.ok) { toast.error('Admin-Login erforderlich'); return; }
    load();
  };

  return (
    <div className="ai-lab-panel" data-testid="ai-lab-panel">
      <div className="ai-lab-head">
        <span className="ai-lab-title"><Flask size={14} weight="fill" /> KI-Labor – Forschung, ML &amp; Gedächtnis</span>
        <div className="ai-lab-actions">
          <button className="ai-action-btn" disabled={!!busy || research.running_now}
            onClick={() => post('/api/ai/research/run', 'Forschungs-Analyse')}
            data-testid="ai-lab-research-run-btn">
            <ChartLine size={13} weight="bold" className={busy === 'Forschungs-Analyse' || research.running_now ? 'spin' : ''} />
            {research.running_now ? 'Analysiert…' : 'Forschung jetzt'}
          </button>
          <button className="ai-action-btn" disabled={!!busy || ml.training_now}
            onClick={() => post('/api/ai/ml/train', 'ML-Training')}
            data-testid="ai-lab-train-btn">
            <Cpu size={13} weight="bold" className={busy === 'ML-Training' || ml.training_now ? 'spin' : ''} />
            {ml.training_now ? 'Trainiert…' : 'ML-Training'}
          </button>
          <button className="ai-action-btn" disabled={!!busy || research.running_now}
            onClick={() => { if (window.confirm('Forschungs-Daten (Report & Zustand) wirklich zurücksetzen? Der nächste Lauf startet dann frisch.')) post('/api/ai/research/reset', 'Forschung zurückgesetzt'); }}
            data-testid="ai-lab-research-reset-btn" title="Forschungs-Report & Zustand löschen"
            style={{ color: '#FF3366' }}>
            <Trash size={13} weight="bold" /> Forschung zurücksetzen
          </button>
          <button className="ai-action-btn" disabled={!!busy || ml.training_now}
            onClick={() => { if (window.confirm('ML-Trainingsdaten (gespeichertes Modell + Status) wirklich zurücksetzen? Das nächste Training baut das Modell neu auf.')) post('/api/ai/ml/reset', 'ML-Modell zurückgesetzt'); }}
            data-testid="ai-lab-ml-reset-btn" title="Gespeichertes ML-Modell & Trainings-Status löschen"
            style={{ color: '#FF3366' }}>
            <Trash size={13} weight="bold" /> ML zurücksetzen
          </button>
          <button className="ai-action-btn" disabled={!!busy}
            onClick={() => post('/api/ai/observer/run', 'Markt-Beobachtung')}
            data-testid="ai-lab-observe-btn">
            <ArrowsClockwise size={13} weight="bold" className={busy === 'Markt-Beobachtung' ? 'spin' : ''} /> Markt scannen
          </button>
        </div>
      </div>

      {/* Kennzahlen-Leiste */}
      <div className="ai-lab-stats" data-testid="ai-lab-stats">
        <span title="Erkenntnisse der letzten Forschungs-Auswertung">
          <b>{research.insights ?? 0}</b> Erkenntnisse
        </span>
        <span title="Vorhersagegüte des ML-Modells (0.5 = Zufall)">
          AUC <b>{model?.cv_auc ?? '—'}</b>
        </span>
        <span title="Trainingsdatensätze aus echten Ergebnissen">
          <b>{model?.samples ?? 0}</b> Datensätze
        </span>
        <span title="Beobachtete Coins des Markt-Beobachters">
          <b>{observer.symbols_tracked ?? 0}</b> Coins beobachtet
        </span>
        <span title="Einträge im KI-Gedächtnis">
          <Database size={11} /> <b>{mem.total ?? 0}</b> Wissenseinträge
          {mem.mirror
            ? ` · Supabase ${health ? (health.reachable ? 'verbunden' : 'nicht erreichbar') : 'prüft…'}`
            : ' · nur MongoDB'}
        </span>
      </div>
      {(health?.last_error || mem.mirror?.last_error) && (
        <div className="ai-lab-warn" data-testid="ai-lab-supabase-warning">
          ⚠ Supabase: {health?.last_error || mem.mirror?.last_error}
          <div className="ai-lab-warn-hint">Tabelle anlegen mit <code>backend/scripts/supabase_schema.sql</code> – bis dahin speichert das Gedächtnis lokal in MongoDB.</div>
        </div>
      )}

      <div className="ai-lab-tabs">
        {[['research', 'Forschung'], ['ml', 'ML-Modell'], ['gate', 'Gate v1'], ['trades', 'Trade-Steuerung'], ['memory', 'Gedächtnis'], ['market', 'Markt']].map(([k, label]) => (
          <button key={k} className={`ai-lab-tab ${tab === k ? 'active' : ''}`}
            onClick={() => { setTab(k); if (k === 'memory') loadEntries(); if (k === 'trades') loadTrades(); }}
            data-testid={`ai-lab-tab-${k}`}>{label}</button>
        ))}
      </div>

      {tab === 'research' && (
        <div className="ai-lab-body" data-testid="ai-lab-research">
          <div className="ai-lab-meta">
            Zuletzt: <b>{fmt(research.last_run)}</b>
            {research.model ? ` · ${research.model}` : ''}
            {research.counts ? ` · Datenbasis: ${research.counts.backtests || 0} Backtests, ${research.counts.optimizer_runs || 0} Optimizer, ${research.counts.regime_lab_runs || 0} Regime-Lab` : ''}
          </div>
          {research.last_error && <div className="ai-lab-warn">⚠ {research.last_error}</div>}
          {report?.summary ? (
            <>
              <div className="ai-lab-summary">{report.summary}</div>
              {(report.insights || []).length > 0 && (
                <ol className="ai-lab-list" data-testid="ai-lab-insights">
                  {report.insights.map((i, n) => (
                    <li key={n}><b>{i.title}</b>{i.confidence != null ? ` (${i.confidence}%)` : ''}: {i.detail}</li>
                  ))}
                </ol>
              )}
              {(report.strategy_ranking || []).length > 0 && (
                <div className="ai-lab-chips" data-testid="ai-lab-ranking">
                  {report.strategy_ranking.map((r, n) => (
                    <span key={n} className={`ai-lab-chip verdict-${r.verdict}`} title={r.reason}>
                      {r.strategy}: {r.verdict}
                    </span>
                  ))}
                </div>
              )}
              {(report.recommendations || []).length > 0 && (
                <>
                  <div className="ai-lab-sub">Empfehlungen an den KI Trader</div>
                  <ul className="ai-lab-list">{report.recommendations.map((r, n) => <li key={n}>{r}</li>)}</ul>
                </>
              )}
              {(report.ideas || []).length > 0 && (
                <>
                  <div className="ai-lab-sub"><Lightbulb size={12} weight="fill" /> Neue Ideen</div>
                  <ul className="ai-lab-list">{report.ideas.map((i, n) => <li key={n}><b>{i.title}</b>: {i.detail}</li>)}</ul>
                </>
              )}
            </>
          ) : (
            <div className="ai-lab-empty">
              Noch keine Forschungs-Auswertung. Der Forschungs-Analyst startet automatisch zu seinen
              Uhrzeiten und sobald neue Backtests / Optimizer- / Regime-Lab-Läufe fertig sind –
              oder jetzt manuell über „Forschung jetzt“.
            </div>
          )}
        </div>
      )}

      {tab === 'ml' && (
        <div className="ai-lab-body" data-testid="ai-lab-ml">
          {!ml.available && <div className="ai-lab-warn">⚠ ML-Bibliotheken fehlen: {ml.unavailable_reason}</div>}
          <div className="ai-lab-setup">
            <label className="ai-lab-check" title="Training automatisch (täglich + nach neuen Ergebnissen)">
              <span>Auto-Training</span>
              <input type="checkbox" checked={mlSettings.auto_train !== false}
                onChange={e => saveMl({ auto_train: e.target.checked })}
                data-testid="ai-lab-autotrain-toggle" />
            </label>
            <label>
              <span>Optuna-Trials</span>
              <select value={mlSettings.n_trials || 25}
                onChange={e => saveMl({ n_trials: Number(e.target.value) })}
                data-testid="ai-lab-trials-select">
                {[10, 25, 50, 100].map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </label>
            <label>
              <span>Datenfenster</span>
              <select value={mlSettings.lookback_days || 120}
                onChange={e => saveMl({ lookback_days: Number(e.target.value) })}
                data-testid="ai-lab-lookback-select">
                {[30, 60, 120, 365].map(v => <option key={v} value={v}>{v} Tage</option>)}
              </select>
            </label>
            <label className="ai-lab-check" title="Haupt-KI erklärt die Modell-Ergebnisse und leitet Regeln ab">
              <span>KI-Erklärung</span>
              <input type="checkbox" checked={mlSettings.explain_with_llm !== false}
                onChange={e => saveMl({ explain_with_llm: e.target.checked })}
                data-testid="ai-lab-explain-toggle" />
            </label>
          </div>
          {ml.last_error && <div className="ai-lab-warn">⚠ {ml.last_error}</div>}
          {model ? (
            <>
              <div className="ai-lab-meta">
                Trainiert: <b>{fmt(model.trained_at)}</b> · {model.trigger || 'manuell'} ·
                AUC <b>{model.cv_auc}</b> · Genauigkeit <b>{pct(Math.round((model.cv_accuracy || 0) * 1000) / 10)}</b> ·
                {' '}{model.samples} Datensätze ({model.with_market_state} mit Marktzustand) · {model.trials} Optuna-Trials
              </div>
              {model.cv_auc != null && model.cv_auc < 0.55 && (
                <div className="ai-lab-warn">
                  Vorhersagekraft noch gering (AUC &lt; 0.55) – das Modell wird dem KI Trader nur als
                  schwaches Zusatzsignal übergeben. Mit mehr abgeschlossenen Trades wird es stärker.
                </div>
              )}
              <div className="ai-lab-sub">Wichtigste Marktbedingungen (Gain-Anteil)</div>
              <div className="ai-lab-bars" data-testid="ai-lab-importances">
                {(model.importances || []).map((i, n) => (
                  <div className="ai-lab-bar-row" key={n}>
                    <span className="ai-lab-bar-label">{i.feature}</span>
                    <span className="ai-lab-bar"><span style={{ width: `${Math.min(100, i.share_pct || 0)}%` }} /></span>
                    <span className="ai-lab-bar-val">{i.share_pct}%</span>
                  </div>
                ))}
              </div>
              {model.explanation && (
                <>
                  <div className="ai-lab-sub"><Brain size={12} weight="fill" /> Erklärung der Haupt-KI</div>
                  <div className="ai-lab-summary">{model.explanation}</div>
                </>
              )}
              {(model.rules || []).length > 0 && (
                <ul className="ai-lab-list" data-testid="ai-lab-rules">
                  {model.rules.map((r, n) => <li key={n}><b>{r.title}</b>: {r.detail}</li>)}
                </ul>
              )}
              {model.best_params && (
                <div className="ai-lab-code">
                  Beste Hyperparameter (Optuna): {Object.entries(model.best_params).map(([k, v]) => `${k}=${typeof v === 'number' ? Number(v).toFixed(3) : v}`).join(', ')}
                </div>
              )}
            </>
          ) : (
            <div className="ai-lab-empty">
              Noch kein Modell trainiert. Es braucht mindestens 40 abgeschlossene Ergebnisse
              (je 8 Gewinne/Verluste). Danach sucht Optuna die besten Hyperparameter und XGBoost
              lernt, welche Marktbedingungen Gewinne liefern.
            </div>
          )}
        </div>
      )}

      {tab === 'gate' && <GateShadowPanel />}

      {tab === 'trades' && (
        <div className="ai-lab-body" data-testid="ai-lab-trades">
          <div className="ai-lab-setup">
            <label className="ai-lab-check" title="KI prüft offene Trades automatisch und passt sie an">
              <span>KI-Trade-Steuerung</span>
              <input type="checkbox" checked={tmSettings.enabled !== false}
                onChange={e => saveJson('/api/ai/trade/settings', { enabled: e.target.checked })}
                data-testid="ai-tm-enabled-toggle" />
            </label>
            <label className="ai-lab-check" title="Darf die KI eigene Trades eröffnen?">
              <span>Eigene Trades</span>
              <input type="checkbox" checked={tmSettings.allow_open !== false}
                onChange={e => saveJson('/api/ai/trade/settings', { allow_open: e.target.checked })}
                data-testid="ai-tm-open-toggle" />
            </label>
            <label className="ai-lab-check" title="Darf die KI Margin hinzufügen/entnehmen und den Hebel ändern?">
              <span>Margin &amp; Hebel</span>
              <input type="checkbox" checked={tmSettings.allow_margin !== false}
                onChange={e => saveJson('/api/ai/trade/settings', { allow_margin: e.target.checked })}
                data-testid="ai-tm-margin-toggle" />
            </label>
            <label>
              <span>Prüf-Intervall</span>
              <select value={tmSettings.interval_min || 5}
                onChange={e => saveJson('/api/ai/trade/settings', { interval_min: Number(e.target.value) })}
                data-testid="ai-tm-interval-select">
                {[1, 3, 5, 10, 15, 30].map(v => <option key={v} value={v}>{v} min</option>)}
              </select>
            </label>
            <label>
              <span>Max. Hebel</span>
              <select value={tmSettings.max_leverage || 50}
                onChange={e => saveJson('/api/ai/trade/settings', { max_leverage: Number(e.target.value) })}
                data-testid="ai-tm-maxlev-select">
                {[5, 10, 20, 50, 75, 125, 150, 200].map(v => <option key={v} value={v}>{v}x</option>)}
              </select>
            </label>
            <label className="ai-lab-check" title="Profit-Lock: bei Gewinn-Trades darf die KI einen Großteil der Margin entnehmen (Hebel steigt nachträglich) – Kapital wird frei, Restrisiko sinkt">
              <span>Profit-Lock</span>
              <input type="checkbox" checked={tmSettings.profit_lock_enabled !== false}
                onChange={e => saveJson('/api/ai/trade/settings', { profit_lock_enabled: e.target.checked })}
                data-testid="ai-tm-profitlock-toggle" />
            </label>
            <label title="Hebel-Obergrenze für Profit-Lock (gilt nur für Trades im Gewinn)">
              <span>Profit-Lock max. Hebel</span>
              <select value={tmSettings.profit_lock_max_leverage || 100}
                onChange={e => saveJson('/api/ai/trade/settings', { profit_lock_max_leverage: Number(e.target.value) })}
                data-testid="ai-tm-plmaxlev-select">
                {[50, 75, 100, 125, 150, 200].map(v => <option key={v} value={v}>{v}x</option>)}
              </select>
            </label>
            <label title="Wieviel Prozent der Margin müssen mindestens gebunden bleiben">
              <span>Min. Rest-Margin</span>
              <select value={tmSettings.profit_lock_min_margin_pct || 15}
                onChange={e => saveJson('/api/ai/trade/settings', { profit_lock_min_margin_pct: Number(e.target.value) })}
                data-testid="ai-tm-plminmargin-select">
                {[5, 10, 15, 20, 30, 50].map(v => <option key={v} value={v}>{v}%</option>)}
              </select>
            </label>
            <label>
              <span>Aktionen/Trade</span>
              <select value={tmSettings.max_actions_per_trade || 8}
                onChange={e => saveJson('/api/ai/trade/settings', { max_actions_per_trade: Number(e.target.value) })}
                data-testid="ai-tm-maxactions-select">
                {[3, 5, 8, 15, 30].map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </label>
            <button className="ai-action-btn" disabled={!!busy || tm.running_now}
              onClick={() => post('/api/ai/trade/review', 'Trade-Review')}
              data-testid="ai-tm-review-btn">
              <Brain size={13} weight="bold" className={busy === 'Trade-Review' || tm.running_now ? 'spin' : ''} />
              {tm.running_now ? 'Prüft…' : 'Trades jetzt prüfen'}
            </button>
          </div>
          {tm.last_error && <div className="ai-lab-warn">⚠ {tm.last_error}</div>}
          <div className="ai-lab-meta">
            Zuletzt geprüft: <b>{fmt(tm.last_run)}</b> · Cooldown {tmSettings.cooldown_min} min ·
            {' '}Margin-Aufschlag max. {tmSettings.max_margin_add_pct}% der Start-Margin
          </div>
          {tm.last_note && <div className="ai-lab-summary">{tm.last_note}</div>}

          <div className="ai-lab-sub">Offene Trades – manuelle Steuerung</div>
          {trades.length ? (
            <table className="ai-lab-table" data-testid="ai-lab-trades-table">
              <thead>
                <tr><th>Trade</th><th>Seite</th><th>Entry</th><th>SL</th><th>TP1</th><th>Hebel</th>
                  <th>Liq</th><th>KI-Aktionen</th><th>Steuerung</th></tr>
              </thead>
              <tbody>
                {trades.map(t => (
                  <tr key={t.id}>
                    <td>{t.symbol} <span className="ai-lab-ts">({t.mode})</span></td>
                    <td className={t.side === 'LONG' ? 'pos' : 'neg'}>{t.side}</td>
                    <td>{t.entry}</td><td>{t.sl}</td><td>{t.tp1}</td>
                    <td>{t.leverage}x</td><td>{t.liq_price}</td>
                    <td>{t.ai_actions || 0}</td>
                    <td className="ai-lab-trade-actions">
                      <button onClick={() => tradeAction(t.id, 'partial_close', { value: 50 })}
                        title="50 % der Restmenge schließen" data-testid={`ai-tm-partial-${t.id}`}>50 %</button>
                      <button onClick={() => tradeAction(t.id, 'adjust_sl', { pct: 0.3 })}
                        title="SL auf 0,3 % Abstand nachziehen" data-testid={`ai-tm-sl-${t.id}`}>SL↑</button>
                      <button onClick={() => tradeAction(t.id, 'add_margin', { value: 10 })}
                        title="10 USDT Margin hinzufügen" data-testid={`ai-tm-addmargin-${t.id}`}>+M</button>
                      <button onClick={() => tradeAction(t.id, 'remove_margin', { value: 10 })}
                        title="10 USDT Margin entnehmen" data-testid={`ai-tm-delmargin-${t.id}`}>−M</button>
                      <button onClick={() => tradeAction(t.id, 'set_leverage', { value: Math.max(1, (t.leverage || 10) - 2) })}
                        title="Hebel um 2x senken (Positionsgröße bleibt)" data-testid={`ai-tm-lev-${t.id}`}>Hebel−</button>
                      <button className="danger" onClick={() => tradeAction(t.id, 'close')}
                        title="Trade vorzeitig schließen" data-testid={`ai-tm-close-${t.id}`}>Close</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="ai-lab-empty">Keine offenen Trades.</div>}

          <div className="ai-lab-sub">Letzte Aktionen</div>
          {actions.length ? (
            <ul className="ai-lab-list" data-testid="ai-lab-actions-list">
              {actions.map(a => (
                <li key={a.id}>
                  <span className="ai-lab-kind">{a.action}</span>
                  <b>{a.symbol} {a.side}</b>
                  <span className="ai-lab-ts"> · {fmt(a.ts)} · {a.source}{a.ok ? '' : ' · abgelehnt'}</span>
                  <div className="ai-lab-entry-text">{a.reason || '—'}</div>
                </li>
              ))}
            </ul>
          ) : <div className="ai-lab-empty">Noch keine Trade-Aktionen protokolliert.</div>}

          <div className="ai-lab-sub">Closed Loop – Selbstoptimierung</div>
          <div className="ai-lab-setup">
            <label className="ai-lab-check" title="Nach jeder Forschungs-Auswertung automatisch einen Optimizer-Lauf für den stärksten Kandidaten starten">
              <span>Closed Loop</span>
              <input type="checkbox" checked={clSettings.enabled === true}
                onChange={e => saveJson('/api/ai/closed_loop/settings', { enabled: e.target.checked })}
                data-testid="ai-cl-enabled-toggle" />
            </label>
            <label>
              <span>Läufe/Tag</span>
              <select value={clSettings.max_runs_per_day || 2}
                onChange={e => saveJson('/api/ai/closed_loop/settings', { max_runs_per_day: Number(e.target.value) })}
                data-testid="ai-cl-runs-select">
                {[1, 2, 4, 6].map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </label>
            <label>
              <span>Mindestabstand</span>
              <select value={clSettings.min_gap_hours || 6}
                onChange={e => saveJson('/api/ai/closed_loop/settings', { min_gap_hours: Number(e.target.value) })}
                data-testid="ai-cl-gap-select">
                {[1, 3, 6, 12, 24].map(v => <option key={v} value={v}>{v} h</option>)}
              </select>
            </label>
            <button className="ai-action-btn" disabled={!!busy}
              onClick={() => post('/api/ai/closed_loop/run', 'Closed-Loop-Optimierung')}
              data-testid="ai-cl-run-btn">
              <ArrowsClockwise size={13} weight="bold" className={busy === 'Closed-Loop-Optimierung' ? 'spin' : ''} /> Jetzt optimieren
            </button>
          </div>
          <div className="ai-lab-meta">
            {clSettings.enabled ? 'Aktiv' : 'Aus'} · heute {cl.state?.runs_today || 0}/{clSettings.max_runs_per_day} Läufe ·
            {' '}zuletzt {fmt(cl.state?.last_run)}
          </div>
          {cl.state?.last_result && (
            <div className="ai-lab-summary" data-testid="ai-lab-cl-result">
              Letzter Lauf ({cl.state.last_result.strategy_id}):
              {' '}PnL {cl.state.last_result.metrics?.pnl} · Winrate {cl.state.last_result.metrics?.win_rate}% ·
              {' '}{cl.state.last_result.passed ? 'Validierung bestanden' : 'Validierung nicht bestanden'}
              {cl.state.last_result.params && Object.keys(cl.state.last_result.params).length > 0 && (
                <div className="ai-lab-code">Vorschlag: {Object.entries(cl.state.last_result.params).map(([k, v]) => `${k}=${v}`).join(', ')} – Übernahme im Optimizer-Panel</div>
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'memory' && (
        <div className="ai-lab-body" data-testid="ai-lab-memory">
          <div className="ai-lab-meta">
            Speicher: <b>MongoDB</b>{mem.mirror ? ` + Supabase (${mem.mirror.table}, ${mem.mirror.writes} Schreibvorgänge)` : ''} ·
            {' '}{mem.total ?? 0} Einträge
            {mem.by_kind ? ` · ${Object.entries(mem.by_kind).map(([k, v]) => `${k}: ${v}`).join(' | ')}` : ''}
          </div>
          {entries.length ? (
            <ul className="ai-lab-list" data-testid="ai-lab-memory-list">
              {entries.map(e => (
                <li key={e.id}>
                  <span className="ai-lab-kind">{e.kind}</span> <b>{e.title}</b>
                  <span className="ai-lab-ts"> · {fmt(e.ts)}{e.source ? ` · ${e.source}` : ''}</span>
                  <div className="ai-lab-entry-text">{e.content}</div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="ai-lab-empty">Das Gedächtnis ist leer – Forschungs-Analyse oder ML-Training starten.</div>
          )}
        </div>
      )}

      {tab === 'market' && (
        <div className="ai-lab-body" data-testid="ai-lab-market">
          <div className="ai-lab-meta">
            Markt-Beobachter: {observer.enabled ? 'aktiv' : 'aus'} · alle {observer.interval_min} min ·
            zuletzt <b>{fmt(observer.last_run)}</b> · {observer.symbols_tracked} Coins
          </div>
          {observer.last_summary?.summary && (
            <div className="ai-lab-summary">{observer.last_summary.summary}</div>
          )}
          <MarketSnapshots />
        </div>
      )}
    </div>
  );
};

const MarketSnapshots = () => {
  const [rows, setRows] = useState([]);
  useEffect(() => {
    fetch(`${API_URL}/api/ai/observer/snapshots?limit=1`)
      .then(r => r.json()).then(d => setRows(d.latest || [])).catch(() => { });
  }, []);
  if (!rows.length) return <div className="ai-lab-empty">Noch keine Markt-Snapshots gesammelt.</div>;
  return (
    <table className="ai-lab-table" data-testid="ai-lab-market-table">
      <thead>
        <tr><th>Coin</th><th>Regime</th><th>RSI</th><th>Trend %</th><th>Vola %</th><th>ATR %</th><th>Vol ×</th><th>Range-Pos</th></tr>
      </thead>
      <tbody>
        {rows.map(s => (
          <tr key={s.symbol}>
            <td>{s.symbol}</td>
            <td>{s.features?.regime}</td>
            <td>{s.features?.rsi}</td>
            <td className={s.features?.trend_pct >= 0 ? 'pos' : 'neg'}>{s.features?.trend_pct}</td>
            <td>{s.features?.volatility_pct}</td>
            <td>{s.features?.atr_pct}</td>
            <td>{s.features?.volume_ratio}</td>
            <td>{s.features?.range_pos}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
};

export default AILabPanel;
