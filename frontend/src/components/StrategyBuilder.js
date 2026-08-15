import React, { useState, useEffect } from 'react';
import { X, Plus, Trash, FloppyDisk, PencilSimple, ArrowCounterClockwise, DownloadSimple, UploadSimple, Copy } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import { RULE_TIMEFRAMES, TF_MINUTES } from '../constants/timeframes';
import SafeOverlay from './SafeOverlay';
import './StrategyBuilder.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const OP_LABELS = {
  '<': 'kleiner <', '>': 'größer >', '<=': '≤', '>=': '≥',
  '==': 'gleich =', '!=': 'ungleich ≠',
  cross_above: 'kreuzt über', cross_below: 'kreuzt unter',
  in_range: 'im Bereich (z.B. 9-17)', not_in_range: 'außerhalb Bereich',
};
const RANGE_OPS = ['in_range', 'not_in_range'];

const FALLBACK_LABELS = {
  rsi: 'RSI', ema_fast: 'EMA Fast', ema_slow: 'EMA Slow', price: 'Preis',
  ha_color: 'HA Farbe (1=grün)', ema_gap_pct: 'EMA Abstand %',
};

const emptyRule = () => ({ indicator: 'rsi', op: '<', valueType: 'number', value: 30, label: '', timeframe: '' });
const jsonHeaders = () => ({ 'Content-Type': 'application/json', ...authHeaders() });

const StrategyBuilder = ({ strategies, enabledIds, onClose, onChanged }) => {
  const [options, setOptions] = useState({ indicators: [], operators: [], indicator_meta: {}, period_fields: [] });
  const [enabled, setEnabled] = useState(enabledIds);
  const importFileRef = React.useRef(null);
  const [editingId, setEditingId] = useState(null);
  const [baseDefinition, setBaseDefinition] = useState({});
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [periods, setPeriods] = useState({});
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [longRules, setLongRules] = useState([emptyRule()]);
  const [shortRules, setShortRules] = useState([{ ...emptyRule(), op: '>', value: 70 }]);
  const [slMode, setSlMode] = useState('structure');
  const [slPercent, setSlPercent] = useState(1.5);
  const [slTicks, setSlTicks] = useState(4);
  const [crv, setCrv] = useState(2);
  const [preview, setPreview] = useState(null);
  const [previewBusy, setPreviewBusy] = useState(false);

  const runPreview = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setPreviewBusy(true); setPreview(null);
    try {
      const res = await fetch(`${API_URL}/api/strategies/rule-preview`, {
        method: 'POST', headers: jsonHeaders(),
        body: JSON.stringify({
          symbol: 'BTCUSDT', days: 7,
          definition: {
            indicators: { ...periods },
            long_rules: serializeRules(longRules),
            short_rules: serializeRules(shortRules),
          },
        }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Vorschau fehlgeschlagen'); return; }
      setPreview(d);
    } catch { toast.error('Verbindungsfehler'); }
    finally { setPreviewBusy(false); }
  };

  const indLabel = (ind) => options.indicator_meta?.[ind]?.label || FALLBACK_LABELS[ind] || ind;

  // Basis-Timeframe der Strategie (für Regel-TF-Overrides: nur >= Basis & Vielfache)
  const baseTf = baseDefinition.timeframe || '1m';
  const ruleTfValid = (tf) => {
    const b = TF_MINUTES[baseTf] || 1;
    const m = TF_MINUTES[tf] || 0;
    return m >= b && m % b === 0;
  };

  const defaultPeriods = (opts) => {
    const p = {};
    (opts.period_fields || []).forEach(f => { p[f.key] = f.default; });
    return Object.keys(p).length ? p : { ema_fast_period: 9, ema_slow_period: 50, rsi_period: 14 };
  };

  useEffect(() => {
    fetch(`${API_URL}/api/strategies/builder-options`).then(r => r.json()).then(o => {
      setOptions(o);
      setPeriods(prev => (Object.keys(prev).length ? prev : defaultPeriods(o)));
    });
  }, []);

  // Indikatoren gruppiert für die Auswahl
  const groupedIndicators = () => {
    const groups = {};
    (options.indicators || []).forEach(ind => {
      const g = options.indicator_meta?.[ind]?.group || 'Sonstige';
      (groups[g] = groups[g] || []).push(ind);
    });
    return groups;
  };

  const toggleTab = async (id) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const next = enabled.includes(id) ? enabled.filter(x => x !== id) : [...enabled, id];
    setEnabled(next);
    const res = await fetch(`${API_URL}/api/settings`, {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({ enabled_strategies: next }),
    });
    if (!res.ok) { toast.error('Nicht autorisiert – bitte als Admin anmelden'); setEnabled(enabled); return; }
    onChanged && onChanged();
  };

  const updRule = (list, setList, i, field, val) => {
    const copy = [...list];
    copy[i] = { ...copy[i], [field]: val };
    // Regel inhaltlich geändert -> gespeichertes Beschreibungs-Label verwerfen,
    // damit es beim Speichern zur neuen Regel passend neu erzeugt wird.
    if (['indicator', 'op', 'value', 'valueType', 'timeframe'].includes(field)) copy[i].label = '';
    setList(copy);
  };

  const serializeRules = (list) => list.map(r => ({
    indicator: r.indicator, op: r.op,
    value: r.valueType === 'indicator' ? r.value
      : (RANGE_OPS.includes(r.op) ? String(r.value) : parseFloat(r.value)),
    ...(r.timeframe ? { timeframe: r.timeframe } : {}),
    label: r.label || `${indLabel(r.indicator)} ${OP_LABELS[r.op]} ${r.valueType === 'indicator' ? indLabel(r.value) : r.value}${r.timeframe ? ` @${r.timeframe}` : ''}`,
  }));

  const deserializeRules = (list) => (list || []).map(r => {
    const isInd = typeof r.value === 'string' && (options.indicators || []).includes(r.value);
    return { indicator: r.indicator, op: r.op, valueType: isInd ? 'indicator' : 'number', value: r.value, label: r.label || '', timeframe: r.timeframe || '' };
  });

  const resetForm = () => {
    setEditingId(null); setBaseDefinition({}); setName(''); setDescription('');
    setPeriods(defaultPeriods(options));
    setLongRules([emptyRule()]); setShortRules([{ ...emptyRule(), op: '>', value: 70 }]);
    setSlMode('structure'); setSlPercent(1.5); setSlTicks(4); setCrv(2);
  };

  const startEdit = (s) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const d = s.definition || {};
    setEditingId(s.id);
    setBaseDefinition(d);
    setName(s.name || '');
    setDescription(s.description || '');
    setPeriods({ ...defaultPeriods(options), ...(d.indicators || {}) });
    setLongRules(d.long_rules?.length ? deserializeRules(d.long_rules) : [emptyRule()]);
    setShortRules(d.short_rules?.length ? deserializeRules(d.short_rules) : [{ ...emptyRule(), op: '>', value: 70 }]);
    setSlMode(d.sl_mode || 'structure');
    setSlPercent(d.sl_percent ?? 1.5);
    setSlTicks(d.sl_ticks ?? 4);
    setCrv(d.crv_target ?? 2);
    const el = document.querySelector('.sb-form-anchor');
    if (el) el.scrollIntoView({ behavior: 'smooth' });
    toast.info(`Bearbeite "${s.name}"`);
  };

  const save = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (!name.trim()) { toast.error('Name fehlt'); return; }
    if (!longRules.length && !shortRules.length) { toast.error('Mind. eine Regel'); return; }
    // Nur Perioden speichern, die vom Default abweichen ODER schon in der
    // Original-Definition standen – hält die Definition schlank & verlustfrei.
    const defaults = defaultPeriods(options);
    const origInd = baseDefinition.indicators || {};
    const indicators = {};
    Object.entries(periods).forEach(([k, v]) => {
      if (k in origInd || v !== defaults[k]) indicators[k] = v;
    });
    // Verlustfreies Speichern: Original-Definition (timeframe, seeded, ...)
    // bleibt erhalten, nur die im Builder editierbaren Felder werden ersetzt.
    const def = {
      ...baseDefinition,
      name, description,
      indicators,
      long_rules: serializeRules(longRules),
      short_rules: serializeRules(shortRules),
      sl_mode: slMode, sl_percent: slPercent, sl_ticks: slTicks,
      structure_lookback: baseDefinition.structure_lookback ?? 10, crv_target: crv,
    };
    if (editingId) def.id = editingId; else delete def.id;
    const res = await fetch(`${API_URL}/api/strategies/custom`, {
      method: 'POST', headers: jsonHeaders(), body: JSON.stringify(def),
    });
    if (res.ok) {
      toast.success(editingId ? `Strategie "${name}" aktualisiert` : `Strategie "${name}" erstellt`);
      resetForm();
      onChanged && onChanged();
    } else if (res.status === 401) toast.error('Nicht autorisiert – bitte als Admin anmelden');
    else {
      let msg = 'Fehler beim Speichern';
      try {
        const d = await res.json();
        const det = d.detail;
        if (det && Array.isArray(det.problems) && det.problems.length) {
          msg = `Abgewiesen: ${det.problems.slice(0, 3).join(' · ')}`
            + (det.problems.length > 3 ? ` (+${det.problems.length - 3} weitere)` : '');
        } else if (typeof det === 'string') msg = det;
      } catch { /* Antwort ohne JSON */ }
      toast.error(msg, { duration: 10000 });
    }
  };

  const deleteStrategy = async (s) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const label = s.is_custom ? 'Custom-Strategie' : 'voreingestellte Strategie';
    if (!window.confirm(`"${s.name}" (${label}) dauerhaft löschen?`)) return;
    const res = await fetch(`${API_URL}/api/strategies/${s.id}`, { method: 'DELETE', headers: authHeaders() });
    if (res.ok) {
      toast.success(`"${s.name}" gelöscht`);
      if (editingId === s.id) resetForm();
      onChanged && onChanged();
    } else if (res.status === 401) toast.error('Nicht autorisiert – bitte als Admin anmelden');
    else toast.error('Fehler beim Löschen');
  };

  const restoreDefaults = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const res = await fetch(`${API_URL}/api/strategies/restore-defaults`, { method: 'POST', headers: authHeaders() });
    if (res.ok) { toast.success('Voreingestellte Strategien wiederhergestellt'); onChanged && onChanged(); }
    else if (res.status === 401) toast.error('Nicht autorisiert – bitte als Admin anmelden');
    else toast.error('Fehler');
  };

  // ---- Komplettes Strategie-Backup: Download & Wiederherstellung ----
  const duplicateStrategy = async (s) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    try {
      const res = await fetch(`${API_URL}/api/strategies/${s.id}/duplicate`, {
        method: 'POST', headers: jsonHeaders(), body: '{}',
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Duplizieren fehlgeschlagen'); return; }
      toast.success(`Kopie erstellt: ${d.name}`);
      onChanged && onChanged();
    } catch { toast.error('Verbindungsfehler'); }
  };

  const exportStrategy = async (s) => {
    try {
      const res = await fetch(`${API_URL}/api/strategies/${s.id}/export`);
      if (!res.ok) { toast.error('Export fehlgeschlagen'); return; }
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const safe = (s.name || s.id).replace(/[^a-z0-9äöüß_-]+/gi, '_');
      a.download = `strategie-backup-${safe}-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast.success(`"${s.name}" komplett exportiert (Regeln, Parameter, Trade- & Backtest-Einstellungen)`);
    } catch { toast.error('Verbindungsfehler beim Export'); }
  };

  const importStrategyFile = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); e.target.value = ''; return; }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const d = JSON.parse(reader.result);
        if (d.type !== 'strategy_backup') { toast.error('Keine gültige Strategie-Backup-Datei'); return; }
        const res = await fetch(`${API_URL}/api/strategies/import`, {
          method: 'POST', headers: jsonHeaders(), body: JSON.stringify(d),
        });
        const out = await res.json();
        if (!res.ok) { toast.error(out.detail || 'Import fehlgeschlagen'); return; }
        toast.success(`Strategie "${d.name || out.id}" 1:1 wiederhergestellt${out.coin_configs ? ` (inkl. ${out.coin_configs} Coin-Trade-Configs)` : ''}`);
        onChanged && onChanged();
      } catch { toast.error('Datei konnte nicht gelesen werden'); }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const IndicatorSelect = ({ value, onChange }) => (
    <select value={value} onChange={onChange}>
      {Object.entries(groupedIndicators()).map(([group, inds]) => (
        <optgroup key={group} label={group}>
          {inds.map(ind => <option key={ind} value={ind}>{indLabel(ind)}</option>)}
        </optgroup>
      ))}
    </select>
  );

  const RuleEditor = ({ list, setList, color }) => (
    <div className="sb-rules">
      {list.map((r, i) => (
        <div className="sb-rule" key={i} data-testid={`rule-row-${color}-${i}`}>
          <IndicatorSelect value={r.indicator} onChange={e => updRule(list, setList, i, 'indicator', e.target.value)} />
          <select value={r.op} onChange={e => updRule(list, setList, i, 'op', e.target.value)}>
            {(options.operators || []).map(op => <option key={op} value={op}>{OP_LABELS[op] || op}</option>)}
          </select>
          <select value={r.valueType} onChange={e => updRule(list, setList, i, 'valueType', e.target.value)}>
            <option value="number">Zahl</option>
            <option value="indicator">Indikator</option>
          </select>
          {r.valueType === 'indicator' ? (
            <IndicatorSelect value={r.value} onChange={e => updRule(list, setList, i, 'value', e.target.value)} />
          ) : (
            <input type="number" value={r.value} onChange={e => updRule(list, setList, i, 'value', e.target.value)} />
          )}
          <select value={r.timeframe || ''} onChange={e => updRule(list, setList, i, 'timeframe', e.target.value)}
            title={`Timeframe nur für diese Regel (Multi-Timeframe-Filter, z.B. Trend auf 1h prüfen). Standard = Strategie-Timeframe (${baseTf}). Nur Vielfache des Strategie-TF wählbar. Cross-/Trigger-Regeln besser auf dem Strategie-TF lassen.`}
            data-testid={`rule-tf-${color}-${i}`}>
            <option value="">TF: Strategie ({baseTf})</option>
            {RULE_TIMEFRAMES.map(t => (
              <option key={t.v} value={t.v} disabled={!ruleTfValid(t.v)}>TF: {t.l}</option>
            ))}
          </select>
          <button className="sb-rule-del" onClick={() => setList(list.filter((_, x) => x !== i))}><Trash size={14} /></button>
          {r.label ? (
            <div style={{ flexBasis: '100%', fontSize: 11, opacity: 0.65, paddingLeft: 2 }}
              data-testid={`rule-label-${color}-${i}`}>„{r.label}"</div>
          ) : null}
        </div>
      ))}
      <button className="sb-add-rule" style={{ color }} onClick={() => setList([...list, color === 'long' ? emptyRule() : { ...emptyRule(), op: '>', value: 70 }])} data-testid={`add-rule-${color}`}>
        <Plus size={13} weight="bold" /> Regel
      </button>
    </div>
  );

  return (
    <SafeOverlay className="sb-overlay" onClose={onClose}>
      <div className="sb-panel" onClick={e => e.stopPropagation()} data-testid="strategy-builder">
        <div className="sb-header">
          <h2>STRATEGIEN VERWALTEN</h2>
          <button className="sb-close" onClick={onClose} data-testid="builder-close"><X size={22} weight="bold" /></button>
        </div>

        <div className="sb-content">
          <div className="sb-section">
            <h3>Reiter im Dashboard</h3>
            <div className="sb-tab-toggles">
              {strategies.map(s => (
                <label key={s.id} className={`sb-tab-toggle ${enabled.includes(s.id) ? 'on' : ''}`} data-testid={`tab-toggle-${s.id}`}>
                  <input type="checkbox" checked={enabled.includes(s.id)} onChange={() => toggleTab(s.id)} />
                  <span>{s.name}</span>
                  {s.is_custom && <span className="sb-badge">CUSTOM</span>}
                </label>
              ))}
            </div>
          </div>

          <div className="sb-section">
            <h3>Alle Strategien
              <button className="sb-restore-btn" onClick={() => importFileRef.current?.click()} data-testid="import-strategy-btn" title="Strategie-Backup-Datei laden – stellt eine gelöschte/verstellte Strategie 1:1 wieder her">
                <UploadSimple size={13} weight="bold" /> Strategie importieren
              </button>
              <input ref={importFileRef} type="file" accept=".json,application/json"
                style={{ display: 'none' }} onChange={importStrategyFile} data-testid="import-strategy-file" />
              <button className="sb-restore-btn" onClick={restoreDefaults} data-testid="restore-defaults-btn" title="Gelöschte voreingestellte Strategien wiederherstellen">
                <ArrowCounterClockwise size={13} weight="bold" /> Voreingestellte wiederherstellen
              </button>
            </h3>
            {strategies.length === 0 && <div className="sb-empty">Keine Strategien vorhanden.</div>}
            {strategies.map(s => (
              <div key={s.id} className="sb-custom-item" data-testid={`strategy-item-${s.id}`}>
                <div>
                  <b>{s.name}</b>
                  {s.is_custom ? <span className="sb-badge">CUSTOM</span> : <span className="sb-badge sb-badge-preset">VOREINGESTELLT</span>}
                  <span className="sb-custom-desc">{s.description}</span>
                </div>
                <div className="sb-item-actions">
                  <button className="sb-edit" onClick={() => exportStrategy(s)} data-testid={`export-strategy-${s.id}`} title="Komplette Strategie als Backup-Datei herunterladen (Regeln, Parameter, Trade-Einstellungen)">
                    <DownloadSimple size={15} />
                  </button>
                  {s.is_custom && (
                    <button className="sb-edit" onClick={() => duplicateStrategy(s)} data-testid={`duplicate-strategy-${s.id}`} title="Strategie duplizieren – Kopie zum Weiterentwickeln, Original bleibt erhalten">
                      <Copy size={15} />
                    </button>
                  )}
                  {s.is_custom && (
                    <button className="sb-edit" onClick={() => startEdit(s)} data-testid={`edit-strategy-${s.id}`} title="Bearbeiten">
                      <PencilSimple size={15} />
                    </button>
                  )}
                  <button className="sb-del" onClick={() => deleteStrategy(s)} data-testid={`delete-strategy-${s.id}`} title="Dauerhaft löschen">
                    <Trash size={15} />
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="sb-section sb-form-anchor">
            <h3>{editingId ? 'Custom-Strategie bearbeiten' : 'Neue Custom-Strategie erstellen'}
              {editingId && <button className="sb-restore-btn" onClick={resetForm} data-testid="cancel-edit-btn">Abbrechen / Neu</button>}
            </h3>
            <div className="sb-form-row">
              <input className="sb-input" placeholder="Name" value={name} onChange={e => setName(e.target.value)} data-testid="custom-name" />
              <input className="sb-input" placeholder="Beschreibung" value={description} onChange={e => setDescription(e.target.value)} data-testid="custom-desc" />
            </div>

            <div className="sb-form-row indicators">
              <label>EMA Fast<input type="number" value={periods.ema_fast_period ?? 9} onChange={e => setPeriods(p => ({ ...p, ema_fast_period: parseInt(e.target.value) || 9 }))} /></label>
              <label>EMA Slow<input type="number" value={periods.ema_slow_period ?? 50} onChange={e => setPeriods(p => ({ ...p, ema_slow_period: parseInt(e.target.value) || 50 }))} /></label>
              <label>RSI Periode<input type="number" value={periods.rsi_period ?? 14} onChange={e => setPeriods(p => ({ ...p, rsi_period: parseInt(e.target.value) || 14 }))} /></label>
            </div>

            <button className="sb-restore-btn" style={{ marginBottom: 10 }} onClick={() => setShowAdvanced(v => !v)} data-testid="toggle-advanced-periods">
              {showAdvanced ? '▲ Erweiterte Indikator-Einstellungen ausblenden' : '▼ Erweiterte Indikator-Einstellungen (MACD, Bollinger, ATR, Stochastik, Volumen ...)'}
            </button>
            {showAdvanced && (
              <div className="sb-form-row indicators" style={{ flexWrap: 'wrap' }}>
                {(options.period_fields || [])
                  .filter(f => !['ema_fast_period', 'ema_slow_period', 'rsi_period'].includes(f.key))
                  .map(f => (
                    <label key={f.key}>{f.label}
                      <input type="number" step={f.key === 'bb_std' ? 0.1 : 1}
                        value={periods[f.key] ?? f.default}
                        onChange={e => setPeriods(p => ({ ...p, [f.key]: f.key === 'bb_std' ? (parseFloat(e.target.value) || f.default) : (parseInt(e.target.value) || f.default) }))} />
                    </label>
                  ))}
              </div>
            )}

            <div className="sb-rule-group">
              <div className="sb-rule-label long">LONG Regeln (alle müssen zutreffen)</div>
              <RuleEditor list={longRules} setList={setLongRules} color="long" />
            </div>
            <div className="sb-rule-group">
              <div className="sb-rule-label short">SHORT Regeln (alle müssen zutreffen)</div>
              <RuleEditor list={shortRules} setList={setShortRules} color="short" />
            </div>

            <div className="sb-form-row">
              <label className="sb-sl">Stop Loss
                <select value={slMode} onChange={e => setSlMode(e.target.value)}>
                  <option value="structure">Struktur</option>
                  <option value="percent">Fest %</option>
                </select>
              </label>
              {slMode === 'percent'
                ? <label className="sb-sl">SL %<input type="number" step={0.1} value={slPercent} onChange={e => setSlPercent(parseFloat(e.target.value))} /></label>
                : <label className="sb-sl">SL Ticks<input type="number" value={slTicks} onChange={e => setSlTicks(parseInt(e.target.value))} /></label>}
              <label className="sb-sl">CRV Ziel<input type="number" step={0.1} value={crv} onChange={e => setCrv(parseFloat(e.target.value))} /></label>
            </div>

            <button className="sb-restore-btn" style={{ marginBottom: 8 }} onClick={runPreview} disabled={previewBusy} data-testid="rule-preview-btn"
              title="Mini-Backtest über die letzten 7 Tage (BTCUSDT): Wie oft feuert jede Regel? Sofort-Feedback, ob eine Regel überhaupt greift.">
              {previewBusy ? '⏳ Prüfe Regeln…' : '🔍 Regel-Vorschau (7 Tage)'}
            </button>
            {preview && (
              <div className="sb-preview" data-testid="rule-preview-result" style={{ fontSize: 12, background: 'rgba(255,255,255,0.04)', borderRadius: 6, padding: 10, marginBottom: 10 }}>
                <b>Vorschau {preview.symbol} · {preview.days} Tage · {preview.bars} Kerzen ({preview.timeframe})</b>
                {(preview.problems || []).length > 0 && (
                  <div style={{ color: '#FF3366', marginTop: 4 }}>
                    ⚠ {preview.problems.map((p, i) => <div key={i}>{p}</div>)}
                  </div>
                )}
                <div style={{ marginTop: 6 }}>
                  <span style={{ color: '#00FF66' }}>LONG-Signale (alle Regeln gleichzeitig): {preview.long_signals}</span>
                  {(preview.long_rules || []).map((r, i) => (
                    <div key={i} style={{ opacity: 0.85 }}>· {r.label || r.rule}: feuert {r.fires}× ({r.fire_pct}% der Kerzen){r.fires === 0 ? ' ⚠ feuert nie!' : ''}</div>
                  ))}
                </div>
                <div style={{ marginTop: 6 }}>
                  <span style={{ color: '#FF3366' }}>SHORT-Signale: {preview.short_signals}</span>
                  {(preview.short_rules || []).map((r, i) => (
                    <div key={i} style={{ opacity: 0.85 }}>· {r.label || r.rule}: feuert {r.fires}× ({r.fire_pct}%){r.fires === 0 ? ' ⚠ feuert nie!' : ''}</div>
                  ))}
                </div>
              </div>
            )}
            <button className="sb-create" onClick={save} data-testid="create-strategy-btn">
              <FloppyDisk size={16} weight="bold" /> {editingId ? 'Änderungen speichern' : 'Strategie erstellen'}
            </button>
          </div>
        </div>
      </div>
    </SafeOverlay>
  );
};

export default StrategyBuilder;
