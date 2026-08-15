import React, { useState, useEffect, useCallback } from 'react';
import { Flask, CheckCircle, XCircle, ArrowCounterClockwise, ChartLine, Ghost, SlidersHorizontal, Sparkle, Check } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import './AIGovernance.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const STAGE_LABEL = {
  ghost: 'Ghost-Test (nur Simulation)',
  live_pending: 'wartet auf deine Freigabe',
  paper: 'freigegeben – Paper',
  live: 'freigegeben – Live erlaubt',
  rejected: 'abgelehnt',
};

/** Strategie-Labor: eigene Strategien der KI von Ghost bis Live begleiten. */
export const AIStrategyLabPanel = () => {
  const [candidates, setCandidates] = useState([]);
  const [settings, setSettings] = useState(null);
  const [ghosts, setGhosts] = useState({});
  const [form, setForm] = useState({ name: '', thesis: '', symbols: '' });
  const [macroEdit, setMacroEdit] = useState(null);   // {cid, params}
  const [busy, setBusy] = useState(false);
  // KI-Assistent: Feedback + maschinenlesbare Backtest-Regeln zur eigenen Strategie
  const [assist, setAssist] = useState(null);
  const [assistBusy, setAssistBusy] = useState(false);
  const [assistCid, setAssistCid] = useState(null);
  const [applyBusy, setApplyBusy] = useState(false);

  // Verbesserungs-Vorschläge der KI in die Strategie übernehmen
  const applyAssist = async (cid, fields) => {
    setApplyBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/${cid}/apply-assist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(fields ? { fields } : {}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Übernahme fehlgeschlagen');
      toast.success(`Übernommen: ${(data.applied || []).join(', ')}`
        + (data.registered?.status === 'ok' ? ' · für Backtest registriert' : ''));
      setAssist(null);
      load();
    } catch (e) { toast.error(e.message); } finally { setApplyBusy(false); }
  };

  const load = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/ai/strategies`).then(r => r.json());
      setCandidates(data.candidates || []);
      setSettings(data.status?.settings || null);
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 20000);
    return () => clearInterval(iv);
  }, [load]);

  const saveSettings = async (patch) => {
    setSettings(prev => ({ ...prev, ...patch }));
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(patch),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      setSettings(data.settings);
    } catch (e) { toast.error(e.message); }
  };

  const decide = async (cid, action) => {
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/${cid}/decide`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ action }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      toast.success(`Strategie → ${STAGE_LABEL[data.candidate.stage] || data.candidate.stage}`);
      load();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  const deleteCandidate = async (cid) => {
    if (!window.confirm('Strategie endgültig löschen? Die Custom-Registrierung wird entfernt und offene Trades des Kandidaten werden geschlossen.')) return;
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/${cid}`, {
        method: 'DELETE', headers: authHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      toast.success(`Strategie gelöscht${data.closed_trades ? ` – ${data.closed_trades} offene Trades geschlossen` : ''}`);
      load();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  const registerTest = async (cid) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/${cid}/register-test`, {
        method: 'POST', headers: authHeaders(),
      });
      const data = await res.json();
      if (data.status === 'ok') toast.success('Für Backtester/Optimizer registriert');
      else if (data.status === 'not_testable') {
        toast.message('Noch keine maschinenlesbaren Regeln – nutze „KI: Backtest-Regeln ableiten“, '
          + 'damit die KI die Strategie für den Backtester übersetzt (falls möglich).');
      } else toast.message(data.detail || 'Nicht testbar');
      load();
    } catch (e) { toast.error(e.message); }
  };

  // KI-Assistent für die Eingabe-Form (ohne cid) oder einen Kandidaten (mit cid)
  const runAssist = async (cid = null, applyRules = false) => {
    if (!cid && !form.thesis.trim()) { toast.error('Bitte zuerst die Strategie beschreiben'); return; }
    setAssistBusy(true);
    // BUGFIX: alte Antwort sofort verwerfen – sonst zeigt die Karte während des
    // Nachdenkens kurz die Einschätzung einer ANDEREN Strategie.
    setAssist(null);
    setAssistCid(cid);
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/assist`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(cid ? { cid, apply_rules: applyRules } : {
          name: form.name, thesis: form.thesis,
          symbols: form.symbols.split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'KI-Assistent fehlgeschlagen');
      // Nur übernehmen, wenn die Antwort noch zur aktuell angefragten Strategie
      // gehört (Schutz gegen überholte Antworten bei mehreren Klicks).
      setAssist({ ...data, candidate_id: data.candidate_id ?? cid });
      if (cid) {
        if (applyRules) {
          if (data.registered?.status === 'ok') {
            toast.success('Regeln abgeleitet und für Backtester/Optimizer registriert');
          } else if (!data.backtestable) {
            toast.message(data.backtest_note || 'Strategie ist nicht sinnvoll backtestbar');
          }
        } else {
          toast.success('Einschätzung aktualisiert – sie bleibt im Verlauf der Strategie');
        }
        load();
      }
    } catch (e) { toast.error(e.message); } finally { setAssistBusy(false); }
  };

  const saveMacro = async () => {
    if (!macroEdit) return;
    const params = {};
    Object.entries(macroEdit.params).forEach(([k, v]) => {
      if (v !== '' && v !== null && !Number.isNaN(Number(v))) params[k] = Number(v);
    });
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies/${macroEdit.cid}/macro`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ macro_params: params }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      toast.success('Makro-Parameter dieser Strategie gespeichert');
      setMacroEdit(null);
      load();
    } catch (e) { toast.error(e.message); }
  };

  const loadGhosts = async (cid) => {
    if (ghosts[cid]) { setGhosts({ ...ghosts, [cid]: null }); return; }
    try {
      const data = await fetch(`${API_URL}/api/ai/strategies/ghost-trades?candidate_id=${cid}&limit=25`)
        .then(r => r.json());
      setGhosts({ ...ghosts, [cid]: data.ghost_trades || [] });
    } catch (e) { toast.error('Ghost-Trades konnten nicht geladen werden'); }
  };

  const createCandidate = async () => {
    if (!form.name.trim()) { toast.error('Name fehlt'); return; }
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/strategies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          name: form.name,
          thesis: form.thesis,
          symbols: form.symbols.split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
          source: 'trader',
          // Vom KI-Assistenten abgeleitete Backtest-Regeln direkt mitgeben
          ...(assist && !assist.candidate_id && assist.rule_definition
            ? { rule_definition: assist.rule_definition, rules_text: assist.improved_rules_text || undefined }
            : {}),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      toast.success('Strategie-Vorgabe angelegt – die KI verfolgt sie jetzt zusätzlich'
        + (assist?.rule_definition && !assist?.candidate_id ? ' (inkl. Backtest-Regeln)' : ''));
      setForm({ name: '', thesis: '', symbols: '' });
      setAssist(null);
      load();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="gov-panel" data-testid="ai-strategy-lab-panel">
      <div className="gov-head">
        <span className="gov-title"><Flask size={14} weight="fill" /> Strategie-Labor der KI</span>
        <span className="gov-meta">{candidates.length} Kandidaten</span>
      </div>
      <p className="gov-hint">
        Neue Strategien der KI laufen zuerst als <b>Ghost-Trades</b> (reine Simulation ohne Kapital).
        Erst wenn die Schwellen erreicht sind und <b>du freigibst</b>, darf die Strategie handeln.
        News-getriebene und live nachjustierte Trades sind bewusst nicht backtestbar – dort bleibt
        die KI dynamisch.
      </p>

      {settings && (
        <div className="gov-rules">
          <label className="gov-check">
            <input type="checkbox" checked={!!settings.allow_ai_create}
              onChange={e => saveSettings({ allow_ai_create: e.target.checked })}
              data-testid="strategy-allow-ai-create" />
            <span>KI darf neue Strategien vorschlagen</span>
          </label>
          <label>
            <span>Min. Ghost-Trades</span>
            <input type="number" min="3" max="200" value={settings.min_ghost_trades}
              onChange={e => saveSettings({ min_ghost_trades: Number(e.target.value) })}
              data-testid="strategy-min-ghost-trades" />
          </label>
          <label>
            <span>Min. Ghost-Winrate %</span>
            <input type="number" min="30" max="95" value={settings.min_ghost_winrate}
              onChange={e => saveSettings({ min_ghost_winrate: Number(e.target.value) })}
              data-testid="strategy-min-ghost-winrate" />
          </label>
          <label>
            <span>Nach Freigabe</span>
            <select value={settings.promote_to}
              onChange={e => saveSettings({ promote_to: e.target.value })}
              data-testid="strategy-promote-to">
              <option value="live">Live erlaubt</option>
              <option value="paper">Paper handeln</option>
            </select>
          </label>
        </div>
      )}

      <div className="gov-cands">
        {candidates.length === 0 && (
          <div className="gov-empty">Noch keine Kandidaten – die KI legt bei einer neuen Idee
            automatisch einen an, oder du gibst ihr unten selbst eine Strategie vor.</div>
        )}
        {candidates.map(c => {
          const g = c.stats?.ghost || {};
          return (
            <div className={`gov-card stage-${c.stage}`} key={c.id} data-testid={`strategy-card-${c.id}`}>
              <div className="gov-card-head">
                <b>{c.name}</b>
                <span className={`gov-stage stage-${c.stage}`}>{STAGE_LABEL[c.stage] || c.stage}</span>
              </div>
              {c.thesis && <div className="gov-card-text">{c.thesis}</div>}
              {c.rules_text && <div className="gov-card-rules">Regeln: {c.rules_text}</div>}
              {c.learned_from && <div className="gov-card-src">Gelernt von: {c.learned_from}</div>}
              <div className="gov-card-stats">
                <span><Ghost size={12} weight="fill" /> {g.trades || 0} Ghost-Trades</span>
                <span>Winrate <b>{g.win_rate || 0}%</b></span>
                <span>Summe <b>{g.pnl_pct || 0}%</b></span>
                <span>offen {g.open || 0}</span>
                <span>{(c.symbols || []).join(', ') || 'alle Assets'}</span>
              </div>
              <div className="gov-card-actions">
                {c.stage !== 'live' && (
                  <button className="gov-btn primary" disabled={busy}
                    onClick={() => decide(c.id, 'approve')}
                    data-testid={`strategy-approve-${c.id}`}>
                    <CheckCircle size={13} weight="bold" /> Freigeben ({settings?.promote_to || 'paper'})
                  </button>
                )}
                {c.stage !== 'live' && (
                  <button className="gov-btn" disabled={busy}
                    onClick={() => decide(c.id, 'approve_live')}
                    data-testid={`strategy-approve-live-${c.id}`}>
                    Live freigeben
                  </button>
                )}
                {c.stage !== 'rejected' && (
                  <button className="gov-btn danger" disabled={busy}
                    onClick={() => decide(c.id, 'reject')}
                    data-testid={`strategy-reject-${c.id}`}>
                    <XCircle size={13} weight="bold" /> Ablehnen
                  </button>
                )}
                {c.stage === 'rejected' && (
                  <button className="gov-btn" disabled={busy}
                    onClick={() => decide(c.id, 'reset')}
                    data-testid={`strategy-reset-${c.id}`}>
                    <ArrowCounterClockwise size={13} weight="bold" /> Zurück in Ghost
                  </button>
                )}
                <button className="gov-btn danger" disabled={busy}
                  onClick={() => deleteCandidate(c.id)}
                  data-testid={`strategy-delete-${c.id}`}>
                  <XCircle size={13} weight="bold" /> Endgültig löschen
                </button>
                <button className="gov-btn" onClick={() => registerTest(c.id)}
                  data-testid={`strategy-register-test-${c.id}`}>
                  <ChartLine size={13} weight="bold" /> Für Backtest registrieren
                </button>
                {!c.rule_definition && (
                  <button className="gov-btn" disabled={assistBusy}
                    onClick={() => runAssist(c.id, true)}
                    data-testid={`strategy-assist-${c.id}`}>
                    <Sparkle size={13} weight="bold" />
                    {assistBusy && assistCid === c.id ? 'KI denkt…' : 'KI: Backtest-Regeln ableiten'}
                  </button>
                )}
                <button className="gov-btn" disabled={assistBusy}
                  onClick={() => runAssist(c.id, false)}
                  title="Die KI bewertet diese Strategie anhand ihrer Backtest- und Optimierungs-Daten und macht Verbesserungs-Vorschläge"
                  data-testid={`strategy-improve-${c.id}`}>
                  <Sparkle size={13} weight="bold" />
                  {assistBusy && assistCid === c.id ? 'KI denkt…' : 'KI: Verbesserungen'}
                </button>
                <button className="gov-btn" onClick={() => loadGhosts(c.id)}
                  data-testid={`strategy-ghosts-${c.id}`}>
                  Ghost-Trades
                </button>
                <button className="gov-btn" data-testid={`strategy-macro-${c.id}`}
                  onClick={() => setMacroEdit(macroEdit?.cid === c.id ? null : {
                    cid: c.id,
                    params: {
                      sl_fixed_percent: c.macro_params?.sl_fixed_percent ?? '',
                      tp1_crv: c.macro_params?.tp1_crv ?? '',
                      tpf_crv: c.macro_params?.tpf_crv ?? '',
                      leverage: c.macro_params?.leverage ?? '',
                      tp1_close_percent: c.macro_params?.tp1_close_percent ?? '',
                    },
                  })}>
                  <SlidersHorizontal size={13} weight="bold" /> Makro-Parameter
                </button>
              </div>
              {Object.keys(c.macro_params || {}).length > 0 && (
                <div className="gov-card-rules" data-testid={`strategy-macro-view-${c.id}`}>
                  Eigene Parameter: {Object.entries(c.macro_params)
                    .map(([k, v]) => `${k}=${v}`).join(', ')}
                </div>
              )}
              {macroEdit?.cid === c.id && (
                <div className="gov-rules" data-testid={`strategy-macro-form-${c.id}`}>
                  {[['sl_fixed_percent', 'Stop-Loss %'], ['tp1_crv', 'TP1 als CRV'],
                    ['tpf_crv', 'Final-TP als CRV'], ['leverage', 'Hebel'],
                    ['tp1_close_percent', 'TP1 Teil-Close %']].map(([key, label]) => (
                    <label key={key}>
                      <span>{label}</span>
                      <input type="number" step="0.05" value={macroEdit.params[key]}
                        placeholder="Standard"
                        onChange={e => setMacroEdit({
                          ...macroEdit,
                          params: { ...macroEdit.params, [key]: e.target.value },
                        })}
                        data-testid={`strategy-macro-${key}-${c.id}`} />
                    </label>
                  ))}
                  <div className="gov-actions">
                    <button className="gov-btn primary" onClick={saveMacro}
                      data-testid={`strategy-macro-save-${c.id}`}>Übernehmen</button>
                  </div>
                </div>
              )}
              {assistBusy && assistCid === c.id && (
                <div className="gov-card-rules" data-testid={`strategy-assist-busy-${c.id}`}>
                  <b>KI-Assistent:</b> denkt über „{c.name}“ nach…
                </div>
              )}
              {assist && !assistBusy && assist.candidate_id === c.id && (
                <div className="gov-card-rules" data-testid={`strategy-assist-result-${c.id}`}>
                  <b>KI-Assistent:</b> {assist.feedback}
                  {(assist.data_findings || []).length > 0 && (
                    <ul className="gov-ghost-list" data-testid={`strategy-assist-data-${c.id}`}>
                      {assist.data_findings.map((d, i) => <li key={i}>{d}</li>)}
                    </ul>
                  )}
                  {(assist.suggestions || []).length > 0 && (
                    <ul className="gov-ghost-list" data-testid={`strategy-assist-suggestions-${c.id}`}>
                      {assist.suggestions.map((s, i) => <li key={i}>{s}</li>)}
                    </ul>
                  )}
                  {assist.backtest_note && <div><i>Backtest: {assist.backtest_note}</i></div>}
                  <div className="gov-card-actions">
                    {assist.rule_definition && (
                      <button className="gov-btn primary" disabled={applyBusy}
                        onClick={() => applyAssist(c.id, ['rule_definition'])}
                        title="Abgeleitete Backtest-Regeln übernehmen und für Backtester/Optimizer registrieren"
                        data-testid={`strategy-apply-rules-${c.id}`}>
                        <Check size={13} weight="bold" /> Regeln übernehmen
                      </button>
                    )}
                    {(assist.improved_thesis || assist.improved_rules_text) && (
                      <button className="gov-btn" disabled={applyBusy}
                        onClick={() => applyAssist(c.id, ['thesis', 'rules_text'])}
                        title="Verbesserte Idee und Regel-Beschreibung übernehmen"
                        data-testid={`strategy-apply-text-${c.id}`}>
                        <Check size={13} weight="bold" /> Beschreibung übernehmen
                      </button>
                    )}
                    {(assist.rule_definition || assist.improved_thesis) && (
                      <button className="gov-btn" disabled={applyBusy}
                        onClick={() => applyAssist(c.id, null)}
                        title="Alle Vorschläge der KI übernehmen"
                        data-testid={`strategy-apply-all-${c.id}`}>
                        <Check size={13} weight="bold" /> Alles übernehmen
                      </button>
                    )}
                  </div>
                </div>
              )}
              {(c.assist_history || []).length > 0 && (
                <details className="gov-card-src" data-testid={`strategy-assist-history-${c.id}`}>
                  <summary>Verlauf der KI-Einschätzungen ({c.assist_history.length})</summary>
                  <ul className="gov-ghost-list">
                    {[...c.assist_history].reverse().map((h, i) => (
                      <li key={i}>
                        <b>{String(h.ts || '').slice(0, 16).replace('T', ' ')}</b> · {h.feedback}
                        {(h.data_findings || []).length > 0 && (
                          <div><i>Daten: {h.data_findings.join(' · ')}</i></div>
                        )}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {ghosts[c.id] && (
                <ul className="gov-ghost-list" data-testid={`strategy-ghost-list-${c.id}`}>
                  {ghosts[c.id].length === 0 && <li>(noch keine Ghost-Trades)</li>}
                  {ghosts[c.id].map(t => (
                    <li key={t.id}>
                      {t.symbol} {t.side} @ {t.entry} → {t.status === 'closed'
                        ? `${t.result} (${t.pnl_pct}%)` : 'offen'} · SL {t.sl} / TP {t.tp}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>

      <div className="gov-head gov-head-sub">
        <span className="gov-title">Eigene Strategie vorgeben</span>
      </div>
      <div className="gov-rules">
        <label>
          <span>Name</span>
          <input type="text" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
            data-testid="strategy-form-name" />
        </label>
        <label>
          <span>Assets (Komma, leer = alle)</span>
          <input type="text" value={form.symbols} onChange={e => setForm({ ...form, symbols: e.target.value })}
            placeholder="BTCUSDT,ETHUSDT" data-testid="strategy-form-symbols" />
        </label>
      </div>
      <textarea className="gov-textarea" rows={4} value={form.thesis}
        placeholder="Was soll die KI zusätzlich verfolgen? (Idee, Regeln, Bedingungen)"
        onChange={e => setForm({ ...form, thesis: e.target.value })}
        data-testid="strategy-form-thesis" />
      <div className="gov-actions">
        <button className="gov-btn primary" onClick={createCandidate} disabled={busy}
          data-testid="strategy-form-submit">Strategie anlegen</button>
        <button className="gov-btn" onClick={() => runAssist(null)} disabled={assistBusy}
          data-testid="strategy-form-assist">
          <Sparkle size={13} weight="bold" /> {assistBusy && !assistCid ? 'KI denkt…' : 'KI-Hilfe zur Strategie'}
        </button>
      </div>

      {assist && !assist.candidate_id && (
        <div className="gov-card" data-testid="strategy-assist-panel">
          <div className="gov-card-head">
            <b><Sparkle size={13} weight="fill" /> KI-Feedback zu deiner Strategie</b>
            <span className={`gov-stage ${assist.backtestable ? 'stage-live' : 'stage-rejected'}`}
              data-testid="strategy-assist-backtestable">
              {assist.backtestable ? 'Backtest möglich' : 'nicht direkt backtestbar'}
            </span>
          </div>
          {assist.feedback && <div className="gov-card-text">{assist.feedback}</div>}
          {(assist.suggestions || []).length > 0 && (
            <ul className="gov-ghost-list" data-testid="strategy-assist-suggestions">
              {assist.suggestions.map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          )}
          {assist.backtest_note && (
            <div className="gov-card-src" data-testid="strategy-assist-note">
              Backtest-Hinweis: {assist.backtest_note}
            </div>
          )}
          {assist.improved_rules_text && (
            <div className="gov-card-rules">Präzisierte Regeln: {assist.improved_rules_text}</div>
          )}
          <div className="gov-card-actions">
            {assist.improved_thesis && (
              <button className="gov-btn primary"
                onClick={() => setForm({ ...form, thesis: assist.improved_thesis })}
                data-testid="strategy-assist-apply-thesis">
                <CheckCircle size={13} weight="bold" /> Verbesserte Beschreibung übernehmen
              </button>
            )}
            {assist.rule_definition && (
              <span className="gov-card-src">
                Beim „Strategie anlegen“ werden die abgeleiteten Backtest-Regeln automatisch
                mitgespeichert und im Backtester/Optimizer wählbar.
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default AIStrategyLabPanel;
