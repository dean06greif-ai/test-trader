import React, { useState, useEffect, useCallback } from 'react';
import { Crown, FloppyDisk, ArrowCounterClockwise, ShieldCheck, Drop } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import './AIGovernance.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const SIDES = ['LONG', 'SHORT'];

/** MasterPrompt (oberstes Gebot) + harte Regeln + Daten-Validierung. */
export const AIGovernancePanel = () => {
  const [snap, setSnap] = useState(null);
  const [text, setText] = useState('');
  const [rules, setRules] = useState(null);
  const [lessonPolicy, setLessonPolicy] = useState('');
  const [validation, setValidation] = useState(null);
  const [aiCfg, setAiCfg] = useState(null);
  const [liqSymbols, setLiqSymbols] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [mp, val, status] = await Promise.all([
        fetch(`${API_URL}/api/ai/master-prompt`).then(r => r.json()),
        fetch(`${API_URL}/api/ai/validation`).then(r => r.json()),
        fetch(`${API_URL}/api/ai/status`).then(r => r.json()),
      ]);
      const s = mp.master_prompt || null;
      setSnap(s);
      setText(s?.text || '');
      setLessonPolicy(s?.lesson_policy || '');
      setRules(s?.rules ? { ...s.rules } : null);
      setValidation(val.settings || null);
      setAiCfg(status.config || null);
      setLiqSymbols((status.config?.liquidity_symbols || []).join(', '));
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/master-prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ text, rules, lesson_policy: lessonPolicy }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Speichern fehlgeschlagen');
      setSnap(data.master_prompt);
      toast.success(`MasterPrompt gespeichert (v${data.master_prompt.version}) – die KI wird ihn kommentieren`);
    } catch (e) {
      toast.error(e.message);
    } finally {
      setSaving(false);
    }
  };

  const resetDefaults = () => {
    if (!snap?.defaults) return;
    setText(snap.defaults.text || '');
    setLessonPolicy(snap.defaults.lesson_policy || '');
    setRules({ ...snap.defaults.rules });
  };

  const saveValidation = async (patch) => {
    const next = { ...validation, ...patch };
    setValidation(next);
    try {
      const res = await fetch(`${API_URL}/api/ai/validation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(patch),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      setValidation(data.settings);
    } catch (e) { toast.error(e.message); }
  };

  // KI-Konfiguration (hier: Liquiditäts-Kontext) – wird serverseitig gespeichert.
  const saveAiCfg = async (patch) => {
    setAiCfg(prev => ({ ...prev, ...patch }));
    try {
      const res = await fetch(`${API_URL}/api/ai/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(patch),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
      setAiCfg(data.config);
      setLiqSymbols((data.config.liquidity_symbols || []).join(', '));
      toast.success('Liquiditäts-Kontext gespeichert');
    } catch (e) { toast.error(e.message); }
  };

  if (!snap || !rules) return <div className="gov-panel" data-testid="ai-governance-panel">Lade…</div>;

  const toggleSide = (side) => {
    const has = (rules.allowed_sides || []).includes(side);
    let next = has ? rules.allowed_sides.filter(s => s !== side) : [...(rules.allowed_sides || []), side];
    if (!next.length) next = [side];
    setRules({ ...rules, allowed_sides: next });
  };

  return (
    <div className="gov-panel" data-testid="ai-governance-panel">
      <div className="gov-head">
        <span className="gov-title"><Crown size={14} weight="fill" /> MasterPrompt · oberstes Gebot</span>
        <span className="gov-meta">v{snap.version}{snap.updated_at ? ` · ${String(snap.updated_at).slice(0, 16).replace('T', ' ')}` : ''}</span>
      </div>
      <p className="gov-hint">
        Nur du kannst diesen Text ändern. Er wird jeder KI-Rolle als erster Block übergeben und steht
        über allen Lektionen, Vorschlägen und Analysen. Lektionen und Trades, die den harten Regeln
        widersprechen, werden automatisch blockiert.
      </p>
      <textarea
        className="gov-textarea"
        rows={9}
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder="Grundsätze, die für die KI immer gelten…"
        data-testid="master-prompt-textarea"
      />

      <div className="gov-head gov-head-sub">
        <span className="gov-title">Grundregeln für Lektionen</span>
      </div>
      <p className="gov-hint">
        Diese Regeln gelten für JEDE gelernte Lektion. Lektionen, die ihnen (oder dem
        MasterPrompt) widersprechen, werden automatisch verworfen.
      </p>
      <textarea
        className="gov-textarea"
        rows={6}
        value={lessonPolicy}
        onChange={e => setLessonPolicy(e.target.value)}
        placeholder="Grundregeln, die jede Lektion erfüllen muss…"
        data-testid="lesson-policy-textarea"
      />

      <div className="gov-rules">
        <label>
          <span>Max. Hebel (0 = frei)</span>
          <input type="number" min="0" max="200" value={rules.max_leverage}
            onChange={e => setRules({ ...rules, max_leverage: Number(e.target.value) })}
            data-testid="master-rule-max-leverage" />
        </label>
        <label>
          <span>Mindest-Konfidenz %</span>
          <input type="number" min="0" max="100" value={rules.min_confidence}
            onChange={e => setRules({ ...rules, min_confidence: Number(e.target.value) })}
            data-testid="master-rule-min-confidence" />
        </label>
        <label>
          <span>Tages-Verlustlimit USDT (0 = aus)</span>
          <input type="number" min="0" max="100000" value={rules.max_daily_loss_usdt}
            onChange={e => setRules({ ...rules, max_daily_loss_usdt: Number(e.target.value) })}
            data-testid="master-rule-daily-loss" />
        </label>
        <label>
          <span>Max. Trades pro Tag (0 = frei)</span>
          <input type="number" min="0" max="200" value={rules.max_trades_per_day}
            onChange={e => setRules({ ...rules, max_trades_per_day: Number(e.target.value) })}
            data-testid="master-rule-trades-per-day" />
        </label>
        <label>
          <span>Max. offene KI-Trades (0 = frei)</span>
          <input type="number" min="0" max="50" value={rules.max_open_trades}
            onChange={e => setRules({ ...rules, max_open_trades: Number(e.target.value) })}
            data-testid="master-rule-max-open" />
        </label>
        <label>
          <span>Verbotene Begriffe in Lektionen (Komma)</span>
          <input type="text" value={(rules.forbidden_terms || []).join(',')}
            onChange={e => setRules({
              ...rules,
              forbidden_terms: e.target.value.split(',').map(s => s.trim()).filter(Boolean),
            })}
            placeholder="z.B. all-in, revenge"
            data-testid="master-rule-forbidden-terms" />
        </label>
        <label>
          <span>Gesperrte Coins (Komma)</span>
          <input type="text" value={(rules.blocked_symbols || []).join(',')}
            onChange={e => setRules({
              ...rules,
              blocked_symbols: e.target.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
            })}
            placeholder="z.B. DOGEUSDT"
            data-testid="master-rule-blocked" />
        </label>
        <div className="gov-sides">
          <span>Erlaubte Richtungen</span>
          <div className="gov-side-btns">
            {SIDES.map(s => (
              <button
                key={s}
                className={`gov-chip ${(rules.allowed_sides || []).includes(s) ? 'on' : ''}`}
                onClick={() => toggleSide(s)}
                data-testid={`master-rule-side-${s.toLowerCase()}`}
              >{s}</button>
            ))}
          </div>
        </div>
        <label className="gov-check">
          <input type="checkbox" checked={!!rules.require_live_approval}
            onChange={e => setRules({ ...rules, require_live_approval: e.target.checked })}
            data-testid="master-rule-live-approval" />
          <span>Neue KI-Strategien nur nach meiner Freigabe live</span>
        </label>
      </div>

      <div className="gov-actions">
        <button className="gov-btn primary" onClick={save} disabled={saving} data-testid="master-prompt-save">
          <FloppyDisk size={14} weight="bold" /> {saving ? 'Speichert…' : 'MasterPrompt speichern'}
        </button>
        <button className="gov-btn" onClick={resetDefaults} data-testid="master-prompt-reset">
          <ArrowCounterClockwise size={14} weight="bold" /> Voreinstellung
        </button>
      </div>

      {validation && (
        <>
          <div className="gov-head gov-head-sub">
            <span className="gov-title"><ShieldCheck size={14} weight="fill" /> Daten-Validierung der KI-Änderungen</span>
          </div>
          <p className="gov-hint">
            Ohne diese Mindest-Stichprobe darf die KI keine Einstellungen ändern und keine neuen
            Lektionen anlegen – ihre Wünsche werden als „warten auf Daten“ geparkt. Deine eigenen
            Änderungen gelten immer sofort; die KI sagt dir aber ihre Meinung dazu.
          </p>
          <div className="gov-rules">
            <label className="gov-check">
              <input type="checkbox" checked={!!validation.enabled}
                onChange={e => saveValidation({ enabled: e.target.checked })}
                data-testid="validation-enabled" />
              <span>Validierung aktiv</span>
            </label>
            <label>
              <span>Min. geschlossene Trades (Engine)</span>
              <input type="number" min="0" max="200" value={validation.min_closed_trades}
                onChange={e => saveValidation({ min_closed_trades: Number(e.target.value) })}
                data-testid="validation-min-closed" />
            </label>
            <label>
              <span>Min. Trades pro Coin</span>
              <input type="number" min="0" max="100" value={validation.min_symbol_trades}
                onChange={e => saveValidation({ min_symbol_trades: Number(e.target.value) })}
                data-testid="validation-min-symbol" />
            </label>
            <label>
              <span>Min. Ergebnisse für neue Lektion</span>
              <input type="number" min="0" max="200" value={validation.min_lesson_results}
                onChange={e => saveValidation({ min_lesson_results: Number(e.target.value) })}
                data-testid="validation-min-lesson" />
            </label>
            <label>
              <span>Min. Ergebnisse zum Verwerfen</span>
              <input type="number" min="0" max="300" value={validation.min_removal_results}
                onChange={e => saveValidation({ min_removal_results: Number(e.target.value) })}
                data-testid="validation-min-removal" />
            </label>
          </div>
          <p className="gov-hint">
            <b>Struktur-Parameter</b> (Stop-Loss, CRV, Hebel, Konfidenz) sind extra geschützt:
            Sie brauchen eine größere Stichprobe UND mehrere Bestätigungen derselben Richtung –
            ein einzelner Trade verschiebt nichts. Zusätzlich wandert jeder Wert nur in kleinen
            Schritten.
          </p>
          <div className="gov-rules">
            <label>
              <span>Min. Trades (Struktur)</span>
              <input type="number" min="0" max="500" value={validation.macro_min_trades}
                onChange={e => saveValidation({ macro_min_trades: Number(e.target.value) })}
                data-testid="validation-macro-min-trades" />
            </label>
            <label>
              <span>Nötige Bestätigungen</span>
              <input type="number" min="1" max="20" value={validation.macro_min_confirmations}
                onChange={e => saveValidation({ macro_min_confirmations: Number(e.target.value) })}
                data-testid="validation-macro-confirmations" />
            </label>
            <label>
              <span>Max. Schritt (% des Werts)</span>
              <input type="number" min="1" max="100" value={validation.macro_max_step_pct}
                onChange={e => saveValidation({ macro_max_step_pct: Number(e.target.value) })}
                data-testid="validation-macro-step" />
            </label>
            <label>
              <span>Bestätigungs-Fenster (Tage)</span>
              <input type="number" min="1" max="90" value={validation.macro_confirm_window_days}
                onChange={e => saveValidation({ macro_confirm_window_days: Number(e.target.value) })}
                data-testid="validation-macro-window" />
            </label>
          </div>
        </>
      )}

      {aiCfg && (
        <>
          <div className="gov-head gov-head-sub">
            <span className="gov-title"><Drop size={14} weight="fill" /> Liquiditäts-Kontext für die KI</span>
          </div>
          <p className="gov-hint">
            Liefert der KI Liquiditäts-Kontext für ihre Analysen. <b>Echte Daten</b> (Long/Short-Ratio,
            Open Interest, Orderbook-Wände, Live-Liquidationen der Börsen) sind getrennt schaltbar von der
            <b> gemessenen Liquidations-Verteilung</b>: echte Force-Orders der letzten Stunden, zu
            Preis-Zonen verdichtet (die frühere Formel-Schätzung wurde ersetzt). Die eigenen
            Liquiditäts-Level (POC/EQH/EQL/Order Blocks) bleiben immer aktiv, da sie aus echten
            Kerzendaten berechnet werden.
          </p>
          <div className="gov-rules">
            <label className="gov-check">
              <input type="checkbox" checked={!!aiCfg.liquidity_enabled}
                onChange={e => saveAiCfg({ liquidity_enabled: e.target.checked })}
                data-testid="liquidity-enabled-toggle" />
              <span>Liquiditäts-Kontext aktiv</span>
            </label>
            <label className="gov-check">
              <input type="checkbox" checked={aiCfg.use_liquidation_data !== false}
                onChange={e => saveAiCfg({ use_liquidation_data: e.target.checked })}
                data-testid="use-liquidation-data-toggle" />
              <span>Echte Liquidations-Daten (L/S-Ratio, OI, Wände, Live-Liqs)</span>
            </label>
            <label className="gov-check">
              <input type="checkbox" checked={aiCfg.use_heatmap_data === true}
                onChange={e => saveAiCfg({ use_heatmap_data: e.target.checked })}
                data-testid="use-heatmap-data-toggle" />
              <span>Gemessene Liquidations-Verteilung (echte Force-Orders, letzte 4h)</span>
            </label>
            <label>
              <span>Coins für den Liquiditäts-Kontext (max. 6, Komma)</span>
              <input type="text" value={liqSymbols}
                onChange={e => setLiqSymbols(e.target.value)}
                onBlur={() => saveAiCfg({
                  liquidity_symbols: liqSymbols.split(',')
                    .map(s => s.trim().toUpperCase()).filter(Boolean).slice(0, 6),
                })}
                placeholder="BTCUSDT, ETHUSDT, SOLUSDT"
                data-testid="liquidity-symbols-input" />
            </label>
          </div>
        </>
      )}
    </div>
  );
};

export default AIGovernancePanel;
