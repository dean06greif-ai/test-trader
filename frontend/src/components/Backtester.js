import React, { useState, useEffect, useRef, useCallback } from 'react';
import { X, Play, Trophy, ClockCounterClockwise, Gear, DownloadSimple, ArrowCounterClockwise, Cloud, Desktop } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import AssetPicker from './AssetPicker';
import useInstruments from '../hooks/useInstruments';
import { authHeaders, isAdmin } from '../auth';
import SafeOverlay from './SafeOverlay';
import LocalWorkerPanel from './LocalWorkerPanel';
import BenchmarkBar from './BenchmarkBar';
import EquityChart from './EquityChart';
import TIMEFRAMES from '../constants/timeframes';
import './Backtester.css';
import './BacktesterExtra.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const DAY_OPTIONS = [1, 2, 3, 5, 7, 14, 30, 60, 90, 180, 360, 540, 720, 900, 1080, 1440,
  1800, 2160, 2520, 2880, 3240, 3600, 3960, 4320, 4680, 5040, 5400];

const BE_MODES = [
  { v: 'tp1', l: 'Bei TP1 → SL auf Break-Even + Gebühren' },
  { v: 'crv', l: 'Bei frei wählbarem CRV (z.B. 1R, 2R, 3R)' },
  { v: 'profit_pct', l: 'Bei festem Gewinn-% auf die Marge' },
  { v: 'smart', l: 'Smart (Backtest: Swing-Low/High, Live: wie TP1)' },
  { v: 'off', l: 'Break-Even deaktiviert' },
];

const STATE_KEY = 'bt_ui_state_v1';

const loadState = () => {
  try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; } catch { return {}; }
};

const fmtEta = (s) => {
  if (s === null || s === undefined) return '';
  if (s >= 3600) return `~${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}min`;
  if (s >= 60) return `~${Math.floor(s / 60)}min ${s % 60}s`;
  return `~${s}s`;
};

const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

const cleanCfg = (c) => {
  const out = {};
  Object.entries(c || {}).forEach(([k, v]) => {
    if (k === 'params') {
      const p = {};
      Object.entries(v || {}).forEach(([pk, pv]) => {
        if (pv !== '' && pv !== null && pv !== undefined && !Number.isNaN(pv)) p[pk] = pv;
      });
      if (Object.keys(p).length) out.params = p;
    } else if (k === 'definition') {
      if (v && typeof v === 'object') out.definition = v;
    } else if (v !== '' && v !== null && v !== undefined && !Number.isNaN(v)) {
      out[k] = v;
    }
  });
  return out;
};

export default function Backtester({ onClose }) {
  const saved = useRef(loadState()).current;
  const [strategies, setStrategies] = useState([]);
  const [selStrats, setSelStrats] = useState(saved.selStrats || []);
  const [selCoins, setSelCoins] = useState(saved.selCoins || []);
  const { symbols: allSymbols } = useInstruments();
  const [days, setDays] = useState(saved.days ?? 3);
  const [dateMode, setDateMode] = useState(saved.dateMode || 'days'); // 'days' | 'custom'
  const [dateFrom, setDateFrom] = useState(saved.dateFrom || '');
  const [dateTo, setDateTo] = useState(saved.dateTo || '');
  const [capital, setCapital] = useState(saved.capital ?? 100);
  const [fee, setFee] = useState(saved.fee ?? 0.06);
  const [requireAll, setRequireAll] = useState(!!saved.requireAll);
  const [stratCfgs, setStratCfgs] = useState({});
  const [openCfg, setOpenCfg] = useState(null);
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [resultId, setResultId] = useState(null);
  const [fastPath, setFastPath] = useState(saved.fastPath !== false);
  const [ram, setRam] = useState(null);
  const [applyFor, setApplyFor] = useState(null);
  const [applyMode, setApplyMode] = useState('paper');
  const [applyCoins, setApplyCoins] = useState([]);
  const [applying, setApplying] = useState(false);
  const [execution, setExecution] = useState(saved.execution || 'cloud');
  const [lwOnline, setLwOnline] = useState(false);
  const [showLW, setShowLW] = useState(false);
  const pollRef = useRef(null);
  const importRef = useRef(null);

  // ---- QoL: Auswahl lokal merken (bleibt beim Schließen/Neuöffnen erhalten) ----
  useEffect(() => {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({
        selStrats, selCoins, days, dateMode, dateFrom, dateTo, capital,
        fee, requireAll, fastPath, execution,
      }));
    } catch { /* ignore */ }
  }, [selStrats, selCoins, days, dateMode, dateFrom, dateTo, capital,
    fee, requireAll, fastPath, execution]);

  // ---- Lokaler Worker: Online-Status für die Ausführungs-Auswahl ----
  useEffect(() => {
    const check = () => fetch(`${API_URL}/api/localworker/status`).then(r => r.json())
      .then(d => setLwOnline(!!d.online)).catch(() => setLwOnline(false));
    check();
    const iv = setInterval(check, 10000);
    return () => clearInterval(iv);
  }, []);

  // Vorauswahl absichern, sobald das Asset-Universum geladen ist
  useEffect(() => {
    if (!allSymbols.length) return;
    setSelCoins(prev => {
      const valid = (prev || []).filter(c => allSymbols.includes(c));
      return valid.length ? valid : allSymbols.slice(0, 3);
    });
  }, [allSymbols]);

  const loadRam = () => {
    fetch(`${API_URL}/api/system/ram`).then(r => r.json()).then(setRam).catch(() => {});
  };

  useEffect(() => {
    fetch(`${API_URL}/api/strategies`).then(r => r.json()).then(d => {
      const list = d.strategies || [];
      setStrategies(list);
      setSelStrats(prev => {
        const valid = prev.filter(id => list.some(s => s.id === id));
        return valid.length ? valid : list.slice(0, 2).map(s => s.id);
      });
    });
    fetch(`${API_URL}/api/backtest/strategy-configs`).then(r => r.json())
      .then(d => setStratCfgs(d.configs || {})).catch(() => {});
    // letztes Ergebnis laden
    fetch(`${API_URL}/api/backtest/results?limit=1`).then(r => r.json()).then(d => {
      const last = (d.results || [])[0];
      if (last?.result) { setResult(last.result); setResultId(last.id); }
    }).catch(() => {});
    // Läuft gerade ein Backtest? -> Fortschritt wieder anzeigen (persistent)
    fetch(`${API_URL}/api/backtest/active`).then(r => r.json()).then(d => {
      if (d.active) { setJob(d.active); poll(d.active.id); }
    }).catch(() => {});
    loadRam();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (list, setList, v) =>
    setList(list.includes(v) ? list.filter(x => x !== v) : [...list, v]);

  const updateCfg = (sid, key, value) =>
    setStratCfgs(prev => ({ ...prev, [sid]: { ...(prev[sid] || {}), [key]: value } }));

  const updateCfgParam = (sid, key, value) =>
    setStratCfgs(prev => ({
      ...prev,
      [sid]: { ...(prev[sid] || {}), params: { ...((prev[sid] || {}).params || {}), [key]: value } },
    }));

  // ---- Definition-Override für Custom/Discovery-Strategien (⚙-Panel) ----
  const getEffDef = (s) => {
    const ov = stratCfgs[s.id]?.definition;
    const base = s.definition || {};
    return {
      indicators: { ...(base.indicators || {}), ...((ov || {}).indicators || {}) },
      long_rules: (ov && ov.long_rules) || base.long_rules || [],
      short_rules: (ov && ov.short_rules) || base.short_rules || [],
    };
  };

  const updateDefPeriod = (s, key, value) => {
    const eff = getEffDef(s);
    const def = { ...(stratCfgs[s.id]?.definition || {}),
      indicators: { ...eff.indicators, [key]: value },
      long_rules: eff.long_rules, short_rules: eff.short_rules };
    updateCfg(s.id, 'definition', def);
  };

  const updateDefRule = (s, side, idx, value) => {
    const eff = getEffDef(s);
    const rules = eff[side].map((r, i) => (i === idx ? { ...r, value } : r));
    const def = { ...(stratCfgs[s.id]?.definition || {}),
      indicators: eff.indicators,
      long_rules: side === 'long_rules' ? rules : eff.long_rules,
      short_rules: side === 'short_rules' ? rules : eff.short_rules };
    updateCfg(s.id, 'definition', def);
  };

  const resetCfg = (sid) => {
    setStratCfgs(prev => {
      const n = { ...prev };
      delete n[sid];
      return n;
    });
    toast.success('Backtest-Einstellungen zurückgesetzt');
  };

  const persistCfgs = async (cfgs) => {
    if (!isAdmin()) return;
    try {
      await fetch(`${API_URL}/api/backtest/strategy-configs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ configs: cfgs }),
      });
    } catch { /* ignore */ }
  };

  const poll = useCallback((jobId) => {
    pollRef.current = setInterval(async () => {
      try {
        const j = await fetch(`${API_URL}/api/backtest/status/${jobId}`).then(r => r.json());
        setJob(j);
        if (j.status === 'done') {
          clearInterval(pollRef.current);
          setResult(j.result);
          setResultId(jobId);
          toast.success('Backtest abgeschlossen');
        } else if (j.status === 'error') {
          clearInterval(pollRef.current);
          toast.error(`Backtest fehlgeschlagen: ${j.error}`);
        } else if (j.status === 'cancelled') {
          clearInterval(pollRef.current);
          toast.info('Backtest abgebrochen');
        }
      } catch { /* keep polling */ }
    }, 1500);
  }, []);

  const cancel = async () => {
    if (!job?.id) return;
    try {
      await fetch(`${API_URL}/api/backtest/cancel/${job.id}`, {
        method: 'POST', headers: authHeaders(),
      });
      toast.info('Abbruch angefordert...');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const forceReset = async () => {
    try {
      await fetch(`${API_URL}/api/backtest/reset`, { method: 'POST', headers: authHeaders() });
      if (pollRef.current) clearInterval(pollRef.current);
      setJob(null);
      toast.success('Backtester zurückgesetzt – neue Läufe sind wieder möglich');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const applyFix = async (sid, fix, allFixes) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const fixes = fix ? [fix] : (allFixes || []);
    if (!fixes.length) return;
    try {
      const res = await fetch(`${API_URL}/api/strategies/${sid}/apply-fixes`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ fixes }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Fix fehlgeschlagen'); return; }
      toast.success(`Fix übernommen: ${d.applied.join(', ')}${(d.remaining_problems || []).length ? ` – noch offen: ${d.remaining_problems.length} Problem(e)` : ' – Strategie jetzt auswertbar. Backtest erneut starten!'}`);
      setResult(r => {
        if (!r || !r.strategy_fixes) return r;
        const sf = { ...r.strategy_fixes };
        if (fix) {
          const rest = (sf[sid]?.fixes || []).filter(x => x.label !== fix.label);
          if (rest.length) sf[sid] = { ...sf[sid], fixes: rest }; else delete sf[sid];
        } else delete sf[sid];
        return { ...r, strategy_fixes: sf };
      });
    } catch { toast.error('Verbindungsfehler'); }
  };

  const clearCache = async () => {
    try {
      const d = await fetch(`${API_URL}/api/system/cache/clear`, {
        method: 'POST', headers: authHeaders(),
      }).then(r => r.json());
      toast.success(`Cache geleert (${d.candles_freed || 0} Kerzen freigegeben)`);
      loadRam();
    } catch { toast.error('Verbindungsfehler'); }
  };

  const exportSettings = () => {
    const data = {
      type: 'backtest_settings', version: 1, exported_at: new Date().toISOString(),
      global: { days, dateMode, dateFrom, dateTo, capital, fee, requireAll, fastPath },
      strategy_configs: stratCfgs, selected_strategies: selStrats, selected_coins: selCoins,
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `strategie-einstellungen-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast.success('Einstellungen als Datei exportiert');
  };

  const importSettings = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const d = JSON.parse(reader.result);
        if (d.type !== 'backtest_settings') { toast.error('Keine gültige Einstellungs-Datei'); return; }
        const g = d.global || {};
        if (g.days) setDays(g.days);
        if (g.dateMode) setDateMode(g.dateMode);
        if (g.dateFrom !== undefined) setDateFrom(g.dateFrom || '');
        if (g.dateTo !== undefined) setDateTo(g.dateTo || '');
        if (g.capital) setCapital(g.capital);
        if (g.fee !== undefined) setFee(g.fee);
        setRequireAll(!!g.requireAll);
        if (g.fastPath !== undefined) setFastPath(!!g.fastPath);
        if (d.strategy_configs) { setStratCfgs(d.strategy_configs); persistCfgs(d.strategy_configs); }
        if (Array.isArray(d.selected_strategies)) setSelStrats(d.selected_strategies);
        if (Array.isArray(d.selected_coins)) setSelCoins(d.selected_coins);
        toast.success('Einstellungen aus Datei geladen');
      } catch { toast.error('Datei konnte nicht gelesen werden'); }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const applyToTrading = async () => {
    if (!applyFor) return;
    if (!applyCoins.length) { toast.error('Mind. 1 Coin wählen'); return; }
    setApplying(true);
    try {
      const cfgS = cleanCfg(stratCfgs[applyFor] || {});
      const config = {
        max_capital: capital, fee_percent: fee,
        require_all_rules: requireAll,
        ...['leverage', 'tp1_crv', 'tp_full_crv', 'tp1_close_percent', 'sl_mode', 'sl_fixed_percent',
          'sl_lookback', 'tp_mode', 'tp1_percent', 'tp_full_percent',
          'be_mode', 'be_trigger_crv', 'be_trigger_profit_pct',
          'profit_secure_enabled', 'profit_secure_trigger_pct', 'profit_lock_pct',
          'auto_leverage_enabled', 'auto_lev_mode', 'auto_lev_value', 'auto_lev_max'].reduce((o, k) => {
          if (cfgS[k] !== undefined) o[k] = cfgS[k];
          return o;
        }, {}),
      };
      const res = await fetch(`${API_URL}/api/backtest/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ strategy_id: applyFor, symbols: applyCoins, mode: applyMode, config }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Übernahme fehlgeschlagen'); return; }
      toast.success(`Einstellungen übernommen: ${d.symbols.join(', ')} → ${applyMode.toUpperCase()}`);
      setApplyFor(null);
    } catch { toast.error('Verbindungsfehler'); } finally { setApplying(false); }
  };

  const run = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (!selStrats.length || !selCoins.length) { toast.error('Mind. 1 Strategie und 1 Coin wählen'); return; }
    if (dateMode === 'custom' && !dateFrom) { toast.error('Von-Datum wählen'); return; }
    if (execution === 'local' && !lwOnline) {
      toast.error('Kein lokaler Worker verbunden – Worker starten oder Cloud wählen');
      setShowLW(true);
      return;
    }
    const strategyConfigs = {};
    selStrats.forEach(sid => {
      const c = cleanCfg(stratCfgs[sid]);
      if (Object.keys(c).length) strategyConfigs[sid] = c;
    });
    persistCfgs(stratCfgs);
    try {
      const res = await fetch(`${API_URL}/api/backtest/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          strategy_ids: selStrats, symbols: selCoins, days,
          date_from: dateMode === 'custom' ? dateFrom : undefined,
          date_to: dateMode === 'custom' ? (dateTo || undefined) : undefined,
          max_capital: capital, fee_percent: fee,
          require_all_rules: requireAll,
          use_fast_path: fastPath,
          strategy_configs: strategyConfigs,
          execution,
        }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      setResult(null);
      setResultId(null);
      setJob({ id: d.job_id, status: 'running', progress: 0, phase: 'Startet...' });
      poll(d.job_id);
    } catch { toast.error('Verbindungsfehler'); }
  };

  const running = job?.status === 'running';
  const perStrategy = result?.per_strategy || [];
  const perPair = result?.per_pair || [];
  const bestPerSymbol = result?.best_per_symbol || {};
  const resultCoins = [...new Set(perPair.map(p => p.symbol))];
  const resultStrats = [...new Set(perPair.map(p => p.strategy_id))];
  const pairMap = {};
  perPair.forEach(p => { pairMap[`${p.strategy_id}_${p.symbol}`] = p; });

  const renderCfgPanel = (s) => {
    const cfg = stratCfgs[s.id] || {};
    const params = cfg.params || {};
    const hasCustom = Object.keys(cleanCfg(cfg)).length > 0;
    const effDef = s.is_custom ? getEffDef(s) : null;
    return (
      <div className="btc-panel" key={`cfg-${s.id}`} data-testid={`bt-cfg-panel-${s.id}`}>
        <div className="btc-head">
          <span className="btc-title">{s.name} · Backtest-Einstellungen {hasCustom && <span className="btc-badge">ANGEPASST</span>}</span>
          <button className="btc-reset" onClick={() => resetCfg(s.id)} data-testid={`bt-cfg-reset-${s.id}`}>
            <ArrowCounterClockwise size={13} weight="bold" /> Standard
          </button>
        </div>
        <div className="btc-grid">
          <label>Timeframe
            <select value={cfg.timeframe || ''} onChange={e => updateCfg(s.id, 'timeframe', e.target.value)}
              data-testid={`bt-cfg-tf-${s.id}`}>
              <option value="">Standard ({s.timeframe || '1m'})</option>
              {TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}
            </select>
          </label>
          <label>Hebel (fest)
            <input type="number" min={1} max={125} placeholder="10" value={cfg.leverage ?? ''}
              onChange={e => updateCfg(s.id, 'leverage', e.target.value === '' ? '' : parseInt(e.target.value))}
              data-testid={`bt-cfg-leverage-${s.id}`} />
          </label>
          <label>TP1 bei CRV
            <input type="number" step={0.1} placeholder="1.0" value={cfg.tp1_crv ?? ''}
              onChange={e => updateCfg(s.id, 'tp1_crv', e.target.value === '' ? '' : parseFloat(e.target.value))}
              data-testid={`bt-cfg-tp1-${s.id}`} />
          </label>
          <label>TP Full bei CRV
            <input type="number" step={0.1} placeholder="2.0" value={cfg.tp_full_crv ?? ''}
              onChange={e => updateCfg(s.id, 'tp_full_crv', e.target.value === '' ? '' : parseFloat(e.target.value))}
              data-testid={`bt-cfg-tpfull-${s.id}`} />
          </label>
          <label>TP1 schließt %
            <input type="number" min={1} max={99} placeholder="50" value={cfg.tp1_close_percent ?? ''}
              onChange={e => updateCfg(s.id, 'tp1_close_percent', e.target.value === '' ? '' : parseInt(e.target.value))} />
          </label>
          <label>SL Modus
            <select value={cfg.sl_mode || ''} onChange={e => updateCfg(s.id, 'sl_mode', e.target.value)}
              data-testid={`bt-cfg-slmode-${s.id}`}>
              <option value="">Standard (Struktur)</option>
              <option value="structure">Struktur</option>
              <option value="atr">ATR</option>
              <option value="fixed">Fest %</option>
            </select>
          </label>
          {cfg.sl_mode === 'fixed' ? (
            <label>SL Abstand %
              <input type="number" step={0.1} placeholder="1.0" value={cfg.sl_fixed_percent ?? ''}
                onChange={e => updateCfg(s.id, 'sl_fixed_percent', e.target.value === '' ? '' : parseFloat(e.target.value))} />
            </label>
          ) : (
            <label>SL Lookback (Kerzen)
              <input type="number" placeholder="10" value={cfg.sl_lookback ?? ''}
                onChange={e => updateCfg(s.id, 'sl_lookback', e.target.value === '' ? '' : parseInt(e.target.value))} />
            </label>
          )}
          <label>TP Modus
            <select value={cfg.tp_mode || ''} onChange={e => updateCfg(s.id, 'tp_mode', e.target.value)}
              data-testid={`bt-cfg-tpmode-${s.id}`}>
              <option value="">Standard (CRV dynamisch)</option>
              <option value="crv">CRV (R-Vielfache)</option>
              <option value="fixed_pct">Fest % vom Entry</option>
              <option value="structure">Struktur (letztes Hoch/Tief)</option>
            </select>
          </label>
          {cfg.tp_mode === 'fixed_pct' && (
            <>
              <label>TP1 Abstand %
                <input type="number" step={0.1} placeholder="0.5" value={cfg.tp1_percent ?? ''}
                  onChange={e => updateCfg(s.id, 'tp1_percent', e.target.value === '' ? '' : parseFloat(e.target.value))} />
              </label>
              <label>TP Full Abstand %
                <input type="number" step={0.1} placeholder="1.0" value={cfg.tp_full_percent ?? ''}
                  onChange={e => updateCfg(s.id, 'tp_full_percent', e.target.value === '' ? '' : parseFloat(e.target.value))} />
              </label>
            </>
          )}
          <label>Break-Even
            <select value={cfg.be_mode || ''} onChange={e => updateCfg(s.id, 'be_mode', e.target.value)}
              data-testid={`bt-cfg-bemode-${s.id}`}>
              <option value="">Standard (global)</option>
              {BE_MODES.map(m => <option key={m.v} value={m.v}>{m.l}</option>)}
            </select>
          </label>
          {cfg.be_mode === 'crv' && (
            <label>BE ab CRV (R)
              <input type="number" step={0.1} placeholder="1.0" value={cfg.be_trigger_crv ?? ''}
                onChange={e => updateCfg(s.id, 'be_trigger_crv', e.target.value === '' ? '' : parseFloat(e.target.value))} />
            </label>
          )}
          {cfg.be_mode === 'profit_pct' && (
            <label>BE ab Gewinn %
              <input type="number" step={5} placeholder="30" value={cfg.be_trigger_profit_pct ?? ''}
                onChange={e => updateCfg(s.id, 'be_trigger_profit_pct', e.target.value === '' ? '' : parseFloat(e.target.value))} />
            </label>
          )}
          <label>Zeitfenster (nur diese Strategie)
            <input type="text" placeholder="z.B. 09:00-12:00 · leer = 24h" value={cfg.sessions ?? ''}
              onChange={e => updateCfg(s.id, 'sessions', e.target.value)}
              data-testid={`bt-cfg-sessions-${s.id}`} />
          </label>
          <label>Gewinnsicherung
            <select value={cfg.profit_secure_enabled === undefined ? '' : (cfg.profit_secure_enabled ? '1' : '0')}
              onChange={e => updateCfg(s.id, 'profit_secure_enabled', e.target.value === '' ? '' : e.target.value === '1')}
              data-testid={`bt-cfg-ps-${s.id}`}>
              <option value="">Standard (global)</option>
              <option value="1">An</option>
              <option value="0">Aus</option>
            </select>
          </label>
          {cfg.profit_secure_enabled === true && (
            <>
              <label>Auslöser: Gewinn % auf Marge
                <input type="number" step={5} placeholder="30" value={cfg.profit_secure_trigger_pct ?? ''}
                  onChange={e => updateCfg(s.id, 'profit_secure_trigger_pct', e.target.value === '' ? '' : parseFloat(e.target.value))} />
              </label>
              <label>Gesicherter Gewinn-Anteil %
                <input type="number" step={5} placeholder="50" value={cfg.profit_lock_pct ?? ''}
                  onChange={e => updateCfg(s.id, 'profit_lock_pct', e.target.value === '' ? '' : parseFloat(e.target.value))} />
              </label>
            </>
          )}
          <label>Auto-Leverage
            <select value={cfg.auto_leverage_enabled === undefined ? '' : (cfg.auto_leverage_enabled ? '1' : '0')}
              onChange={e => updateCfg(s.id, 'auto_leverage_enabled', e.target.value === '' ? '' : e.target.value === '1')}
              data-testid={`bt-cfg-autolev-${s.id}`}>
              <option value="">Standard (global)</option>
              <option value="1">An</option>
              <option value="0">Aus</option>
            </select>
          </label>
          {cfg.auto_leverage_enabled === true && (
            <>
              <label>Auto-Lev Modus
                <select value={cfg.auto_lev_mode || 'liq_pct'}
                  onChange={e => updateCfg(s.id, 'auto_lev_mode', e.target.value)}>
                  <option value="liq_pct">Liq. X% hinter Stop</option>
                  <option value="liq_ticks">Liq. X Ticks hinter Stop</option>
                </select>
              </label>
              <label>Auto-Lev Abstand
                <input type="number" step={0.05} placeholder="0.5" value={cfg.auto_lev_value ?? ''}
                  onChange={e => updateCfg(s.id, 'auto_lev_value', e.target.value === '' ? '' : parseFloat(e.target.value))} />
              </label>
              <label>Max. Hebel
                <input type="number" step={1} placeholder="50" value={cfg.auto_lev_max ?? ''}
                  onChange={e => updateCfg(s.id, 'auto_lev_max', e.target.value === '' ? '' : parseInt(e.target.value))} />
              </label>
            </>
          )}
        </div>
        {Object.keys(s.params || {}).length > 0 && (
          <>
            <div className="btc-sub">STRATEGIE-PARAMETER (nur für diesen Backtest)</div>
            <div className="btc-grid">
              {Object.entries(s.params).map(([pk, pm]) => (
                <label key={pk} title={pm.description}>{pm.label}
                  <input type="number" min={pm.min} max={pm.max} step={pm.step}
                    placeholder={String(pm.value)}
                    value={params[pk] ?? ''}
                    onChange={e => updateCfgParam(s.id, pk, e.target.value === '' ? '' : parseFloat(e.target.value))}
                    data-testid={`bt-cfg-param-${s.id}-${pk}`} />
                </label>
              ))}
            </div>
          </>
        )}
        {s.is_custom && effDef && (
          <>
            <div className="btc-sub">
              STRATEGIE-REGELN &amp; PARAMETER (Custom/Discovery · nur für diesen Backtest)
              {cfg.definition && <span className="btc-badge" style={{ marginLeft: 6 }}>ANGEPASST</span>}
            </div>
            {(effDef.long_rules.length > 0 || effDef.short_rules.length > 0) && (
              <div className="btc-grid" data-testid={`bt-cfg-rules-${s.id}`}>
                {effDef.long_rules.map((r, i) => (
                  <label key={`L${i}`} style={{ color: '#30D158' }}>
                    LONG: {r.indicator} {r.op}
                    {typeof r.value === 'number' ? (
                      <input type="number" step="any" value={r.value}
                        onChange={e => updateDefRule(s, 'long_rules', i, e.target.value === '' ? 0 : parseFloat(e.target.value))}
                        data-testid={`bt-cfg-rule-long-${s.id}-${i}`} />
                    ) : (
                      <input type="text" value={String(r.value)} disabled title="Indikator-Vergleich – im Strategie-Builder änderbar" />
                    )}
                  </label>
                ))}
                {effDef.short_rules.map((r, i) => (
                  <label key={`S${i}`} style={{ color: '#FF6482' }}>
                    SHORT: {r.indicator} {r.op}
                    {typeof r.value === 'number' ? (
                      <input type="number" step="any" value={r.value}
                        onChange={e => updateDefRule(s, 'short_rules', i, e.target.value === '' ? 0 : parseFloat(e.target.value))}
                        data-testid={`bt-cfg-rule-short-${s.id}-${i}`} />
                    ) : (
                      <input type="text" value={String(r.value)} disabled title="Indikator-Vergleich – im Strategie-Builder änderbar" />
                    )}
                  </label>
                ))}
              </div>
            )}
            {Object.keys(effDef.indicators).length > 0 && (
              <div className="btc-grid">
                {Object.entries(effDef.indicators).map(([k, v]) => (
                  typeof v === 'number' || v === '' ? (
                    <label key={k}>{k}
                      <input type="number" step="any" value={v}
                        onChange={e => updateDefPeriod(s, k, e.target.value === '' ? '' : parseFloat(e.target.value))}
                        data-testid={`bt-cfg-def-${s.id}-${k}`} />
                    </label>
                  ) : null
                ))}
              </div>
            )}
            <div className="bt-hint" style={{ marginTop: 8 }}>
              Regel-Schwellenwerte &amp; Indikator-Perioden wirken nur auf diesen Backtest.
              Dauerhaft ändern: Strategie-Builder (Stift-Symbol).
            </div>
          </>
        )}
      </div>
    );
  };

  return (
    <SafeOverlay className="bt-overlay" onClose={onClose}>
      <div className="bt-panel" onClick={e => e.stopPropagation()} data-testid="backtester-modal">
        <div className="bt-header">
          <h2><ClockCounterClockwise size={20} weight="bold" style={{ color: '#00A8FF' }} /> STRATEGIE-BACKTESTER</h2>
          <button className="bt-close" onClick={onClose} data-testid="backtester-close"><X size={22} weight="bold" /></button>
        </div>

        <div className="bt-setup">
          <div className="bt-col">
            <div className="bt-label">STRATEGIEN <span className="btc-hint-inline">(⚙ = Trade-Einstellungen: Hebel, Auto-Leverage, TP/SL, Break-Even, Gewinnsicherung, Zeitfenster & Parameter – kommen aus der Strategie)</span></div>
            <div className="bt-chips">
              {strategies.map(s => (
                <span key={s.id} className="btc-chipwrap">
                  <button
                    className={`bt-chip ${selStrats.includes(s.id) ? 'on' : ''}`}
                    onClick={() => toggle(selStrats, setSelStrats, s.id)}
                    data-testid={`bt-strat-${s.id}`}>
                    {s.name}
                    {(stratCfgs[s.id]?.timeframe) && <span className="btc-tf-tag">{stratCfgs[s.id].timeframe}</span>}
                  </button>
                  {selStrats.includes(s.id) && (
                    <button className={`btc-gear ${openCfg === s.id ? 'on' : ''}`}
                      onClick={() => setOpenCfg(openCfg === s.id ? null : s.id)}
                      title="Backtest-Einstellungen für diese Strategie"
                      data-testid={`bt-cfg-toggle-${s.id}`}>
                      <Gear size={13} weight="bold" />
                    </button>
                  )}
                </span>
              ))}
            </div>
            {openCfg && selStrats.includes(openCfg) &&
              renderCfgPanel(strategies.find(s => s.id === openCfg) || { id: openCfg, params: {} })}
          </div>
          <div className="bt-col">
            <div className="bt-label">ASSETS</div>
            <AssetPicker selected={selCoins} chipClass="bt-chip" testIdPrefix="bt-coin"
              onToggle={(c) => toggle(selCoins, setSelCoins, c)} />
          </div>
        </div>

        <div className="bt-params">
          <label>Zeitraum
            <select value={dateMode === 'custom' ? 'custom' : days}
              onChange={e => {
                if (e.target.value === 'custom') { setDateMode('custom'); }
                else { setDateMode('days'); setDays(parseInt(e.target.value)); }
              }} data-testid="bt-days">
              {DAY_OPTIONS.map(d => <option key={d} value={d}>{`${d} Tag${d > 1 ? 'e' : ''}`}</option>)}
              <option value="custom">Benutzerdefiniert (Von–Bis)</option>
            </select>
          </label>
          {dateMode === 'custom' && (
            <>
              <label>Von Datum
                <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
                  data-testid="bt-date-from" />
              </label>
              <label>Bis Datum (leer = heute)
                <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
                  data-testid="bt-date-to" />
              </label>
            </>
          )}
          <label>Kapital (USDT)
            <input type="number" min={1} value={capital} onChange={e => setCapital(parseFloat(e.target.value) || 100)} data-testid="bt-capital" />
          </label>
          <label>Gebühr % / Fill
            <input type="number" step={0.01} value={fee} onChange={e => setFee(parseFloat(e.target.value) || 0)} data-testid="bt-fee" />
          </label>
          <label className="bt-check" title="Trades nur wenn ALLE Regeln der Strategie erfüllt sind (kein 3/5-Regeln-Trade)">
            <input type="checkbox" checked={requireAll} onChange={e => setRequireAll(e.target.checked)} data-testid="bt-require-all" />
            Nur 100% Regel-Treffer
          </label>
          <label className="bt-check" title="Fast-Path: vektorisierte Signal-Berechnung (bis zu 1000x schneller, identische Trades). Ausschalten = Legacy-Pfad (langsamer, minimal weniger RAM-Spitzen)">
            <input type="checkbox" checked={fastPath} onChange={e => setFastPath(e.target.checked)} data-testid="bt-fast-path" />
            Fast-Path (schnell)
          </label>
          <div className="bt-exec" data-testid="bt-execution-toggle">
            <span className="bt-exec-label">Ausführung</span>
            <button className={`bt-exec-btn ${execution === 'cloud' ? 'on' : ''}`}
              onClick={() => setExecution('cloud')} data-testid="bt-exec-cloud"
              title="Berechnung auf dem Server (wie bisher)">
              <Cloud size={13} weight="bold" /> Cloud
            </button>
            <button className={`bt-exec-btn ${execution === 'local' ? 'on' : ''}`}
              onClick={() => setExecution('local')} data-testid="bt-exec-local"
              title="Berechnung auf deinem PC über den lokalen Worker – identische Ergebnisse, nutzt lokal gespeicherte Kerzendaten">
              <Desktop size={13} weight="bold" /> Lokal
              <span className={`bt-exec-dot ${lwOnline ? 'on' : ''}`} data-testid="bt-exec-dot" />
            </button>
            <button className="bt-exec-manage" onClick={() => setShowLW(true)}
              title="Lokale Ausführung verwalten: Worker, Einstellungen & Marktdaten"
              data-testid="bt-exec-manage">
              <Gear size={13} weight="bold" />
            </button>
          </div>
          <button className="bt-run" onClick={run} disabled={running} data-testid="bt-run">
            <Play size={15} weight="fill" /> {running ? 'Läuft...' : 'Backtest starten'}
          </button>
        </div>
        {showLW && <LocalWorkerPanel onClose={() => setShowLW(false)} />}

        <div className="bt-tools" data-testid="bt-tools">
          <span className="bt-ram" data-testid="bt-ram-info">
            {ram ? `RAM Backend: ${ram.process_rss_mb} MB · Kerzen-Cache: ${(ram.candle_cache?.total_candles || 0).toLocaleString('de-DE')} Kerzen (~${ram.candle_cache?.estimated_mb} MB) · Export-Puffer: ~${ram.backtest_exports?.estimated_mb} MB` : 'RAM-Info lädt...'}
          </span>
          <button className="bt-tool-btn" onClick={loadRam} data-testid="bt-ram-refresh">↻ RAM</button>
          <button className="bt-tool-btn" onClick={clearCache} data-testid="bt-clear-cache">Cache leeren</button>
          <button className="bt-tool-btn" onClick={exportSettings} data-testid="bt-export-settings">
            <DownloadSimple size={12} weight="bold" /> Einstellungen speichern
          </button>
          <button className="bt-tool-btn" onClick={() => importRef.current?.click()} data-testid="bt-import-settings">
            Einstellungen laden
          </button>
          <input ref={importRef} type="file" accept=".json,application/json" style={{ display: 'none' }}
            onChange={importSettings} data-testid="bt-import-file" />
        </div>

        {running && (
          <div className="bt-progress" data-testid="bt-progress">
            <div className="bt-progress-bar"><div style={{ width: `${job.progress || 0}%` }} /></div>
            <div className="bt-progress-row">
              <div className="bt-progress-text" data-testid="bt-progress-text">
                {(job.execution === 'local' || job.params?.execution === 'local') &&
                  <span className="bt-exec-tag" data-testid="bt-local-tag">💻 Lokal</span>}
                {job.phase} · {job.progress || 0}%
                {job.eta_seconds != null && <span className="bt-eta"> · Restzeit {fmtEta(job.eta_seconds)}</span>}
              </div>
              <button className="bt-cancel" onClick={cancel} data-testid="bt-cancel">
                <X size={13} weight="bold" /> Abbrechen
              </button>
              <button className="bt-cancel" onClick={forceReset} data-testid="bt-force-reset"
                title="Notfall: hängenden Backtest sofort freigeben">
                <X size={13} weight="bold" /> Zurücksetzen
              </button>
            </div>
          </div>
        )}

        {result && (
          <>
            {(result.strategy_warnings || []).length > 0 && (
              <div className="bt-rule-warnings" data-testid="bt-strategy-warnings">
                <b>Hinweis:</b> Diese Regeln konnten nicht ausgewertet werden (deshalb evtl. 0 Trades):
                <ul>
                  {result.strategy_warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}
            {result.strategy_fixes && Object.keys(result.strategy_fixes).length > 0 && (
              <div className="bt-rule-warnings bt-rule-fixes" data-testid="bt-strategy-fixes">
                <b>Auto-Fix:</b> Korrektur-Vorschläge – Klick übernimmt den Fix direkt in die Strategie:
                {Object.entries(result.strategy_fixes).map(([sid, info]) => (
                  <div key={sid} style={{ marginTop: 6 }}>
                    <span className="mono" style={{ opacity: 0.8 }}>{info.name || sid}:</span>{' '}
                    {(info.fixes || []).map((f, i) => (
                      <button key={i} className="btc-export-btn" style={{ margin: '2px 4px' }}
                        onClick={() => applyFix(sid, f)}
                        data-testid={`bt-apply-fix-${sid}-${i}`}
                        title="Fix in die gespeicherte Strategie übernehmen">
                        🔧 {f.label}
                      </button>
                    ))}
                    <button className="btc-export-btn" style={{ margin: '2px 4px', fontWeight: 700 }}
                      onClick={() => applyFix(sid, null, info.fixes)}
                      data-testid={`bt-apply-all-fixes-${sid}`}>
                      Alle Fixes übernehmen
                    </button>
                  </div>
                ))}
              </div>
            )}
            {result.benchmark && <BenchmarkBar b={result.benchmark} testid="bt-benchmark" />}
            <div className="bt-section-title">
              <Trophy size={15} weight="fill" style={{ color: '#FFD700' }} />
              GESAMT-RANKING ({result.date_from ? `${result.date_from} bis ${result.date_to || 'heute'}` : `${result.days} Tage`} · Kapital {result.config?.max_capital} USDT · {result.config?.auto_leverage_enabled ? 'Auto-Hebel' : `${result.config?.leverage}x`} · Gebühren inkl.)
              {resultId && (
                <span className="btc-export">
                  <a href={`${API_URL}/api/backtest/export/${resultId}?kind=trades`}
                    className="btc-export-btn" data-testid="bt-export-trades">
                    <DownloadSimple size={13} weight="bold" /> Trades CSV
                  </a>
                  <a href={`${API_URL}/api/backtest/export/${resultId}?kind=candles`}
                    className="btc-export-btn" data-testid="bt-export-candles">
                    <DownloadSimple size={13} weight="bold" /> Kerzen CSV
                  </a>
                </span>
              )}
            </div>
            <div className="bt-table-wrap">
              <table className="bt-table" data-testid="bt-strategy-table">
                <thead>
                  <tr><th>#</th><th>Strategie</th><th>TF</th><th>Trades</th><th>Win-Rate</th><th>PnL</th><th>PnL %</th><th>Ø PnL</th><th>Max DD</th><th>DD %</th><th>Gebühren</th><th>Liq.</th><th>Gesichert</th><th>Ø Dauer</th><th></th></tr>
                </thead>
                <tbody>
                  {perStrategy.map((s, i) => (
                    <tr key={s.strategy_id} className={i === 0 && s.pnl > 0 ? 'bt-best' : ''}>
                      <td>{i === 0 && s.pnl > 0 ? <Trophy size={13} weight="fill" style={{ color: '#FFD700' }} /> : i + 1}</td>
                      <td className="bt-name">{s.strategy_name}</td>
                      <td className="btc-tf-cell">{s.timeframe || '1m'}</td>
                      <td>{s.trades}</td>
                      <td className={s.win_rate >= 50 ? 'pos' : 'neg'}>{fmt(s.win_rate, 1)}%</td>
                      <td className={`mono ${s.pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(s.pnl)}</td>
                      <td className={`mono ${s.pnl >= 0 ? 'pos' : 'neg'}`} data-testid={`bt-pnl-pct-${s.strategy_id}`}>{s.pnl_pct !== undefined ? `${fmt(s.pnl_pct, 1)}%` : '–'}</td>
                      <td className={`mono ${s.avg_pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(s.avg_pnl, 3)}</td>
                      <td className="mono neg">{fmt(s.max_drawdown)}</td>
                      <td className="mono neg">{s.max_drawdown_pct !== undefined ? `${fmt(s.max_drawdown_pct, 1)}%` : '–'}</td>
                      <td className="mono">{fmt(s.fees)}</td>
                      <td className={s.liquidations > 0 ? 'neg' : ''}>{s.liquidations || 0}</td>
                      <td>{s.secured || 0}</td>
                      <td>{fmt(s.avg_duration_min, 1)} min</td>
                      <td>
                        <button className="bt-tool-btn" data-testid={`bt-apply-${s.strategy_id}`}
                          title="Diese Backtest-Einstellungen in Live/Paper-Trading übernehmen"
                          onClick={() => {
                            setApplyFor(applyFor === s.strategy_id ? null : s.strategy_id);
                            setApplyCoins(resultCoins);
                            setApplyMode('paper');
                          }}>
                          → Trading
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {applyFor && (
              <div className="btc-panel" data-testid="bt-apply-panel">
                <div className="btc-head">
                  <span className="btc-title">
                    EINSTELLUNGEN ÜBERNEHMEN · {perStrategy.find(p => p.strategy_id === applyFor)?.strategy_name || applyFor}
                  </span>
                  <button className="btc-reset" onClick={() => setApplyFor(null)}>Schließen</button>
                </div>
                <div className="bt-label" style={{ marginTop: 8 }}>MODUS</div>
                <div className="bt-chips">
                  <button className={`bt-chip ${applyMode === 'paper' ? 'on' : ''}`}
                    onClick={() => setApplyMode('paper')} data-testid="bt-apply-mode-paper">PAPER (Simulation)</button>
                  <button className={`bt-chip ${applyMode === 'live' ? 'on' : ''}`}
                    onClick={() => setApplyMode('live')} data-testid="bt-apply-mode-live">LIVE (Echtgeld!)</button>
                </div>
                <div className="bt-label" style={{ marginTop: 8 }}>ASSETS</div>
                <AssetPicker selected={applyCoins} chipClass="bt-chip" testIdPrefix="bt-apply-coin"
                  onToggle={(c) => toggle(applyCoins, setApplyCoins, c)} />
                <div className="bt-hint" style={{ marginTop: 8 }}>
                  Übernommen werden: Kapital, Gebühren sowie die Strategie-Einstellungen
                  (Hebel/Auto-Leverage, TP/SL-Modus, Break-Even, Gewinnsicherung) dieser Strategie.
                </div>
                <button className="bt-run" style={{ marginTop: 10 }} onClick={applyToTrading}
                  disabled={applying} data-testid="bt-apply-confirm">
                  {applying ? 'Übernehme...' : `Für ${applyCoins.length} Coin(s) als ${applyMode.toUpperCase()} aktivieren`}
                </button>
              </div>
            )}

            {resultId && <EquityChart jobId={resultId} />}

            <div className="bt-section-title">MATRIX: WELCHE STRATEGIE PASST ZU WELCHEM COIN?</div>
            <div className="bt-table-wrap">
              <table className="bt-table bt-matrix" data-testid="bt-matrix-table">
                <thead>
                  <tr>
                    <th>Coin</th>
                    {resultStrats.map(sid => (
                      <th key={sid}>{perPair.find(p => p.strategy_id === sid)?.strategy_name || sid}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {resultCoins.map(sym => (
                    <tr key={sym}>
                      <td className="bt-name">{sym.replace('USDT', '')}</td>
                      {resultStrats.map(sid => {
                        const p = pairMap[`${sid}_${sym}`];
                        const isBest = bestPerSymbol[sym]?.strategy_id === sid && (p?.pnl || 0) > 0;
                        return (
                          <td key={sid} className={isBest ? 'bt-best' : ''}>
                            {p ? (
                              <div className="bt-cell">
                                <span className={`mono ${p.pnl >= 0 ? 'pos' : 'neg'}`}>
                                  {fmt(p.pnl)}{p.pnl_pct !== undefined ? ` (${fmt(p.pnl_pct, 1)}%)` : ''}
                                </span>
                                <span className="bt-cell-sub">{p.trades} T · {fmt(p.win_rate, 0)}% WR · {p.timeframe || '1m'}{p.avg_leverage ? ` · Ø${fmt(p.avg_leverage, 1)}x` : ''}</span>
                              </div>
                            ) : '–'}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="bt-hint">
              🏆 = bester PnL. Simulation nutzt dieselbe Logik wie Paper/Live: Struktur-SL, TP1-Teilverkauf,
              Break-Even, ATR-Trailing{result.config?.profit_secure_enabled ? ', Gewinnsicherung' : ''}{result.config?.auto_leverage_enabled ? ', Auto-Leverage' : ''} und Gebühren pro Fill.
              CSV-Export enthält jeden einzelnen Trade inkl. Indikatorwerten &amp; alle ausgewerteten Kerzen zum Nachprüfen.
              Hinweis: Hohe Timeframes (24h+) brauchen einen langen Zeitraum, sonst gibt es zu wenig Kerzen.
            </div>
          </>
        )}

        {!result && !running && (
          <div className="bt-empty" data-testid="bt-empty">
            Wähle Strategien &amp; Coins und starte den Backtest –
            die Vergangenheit zeigt dir in Minuten, was Paper-Trading Wochen kosten würde.
          </div>
        )}
      </div>
    </SafeOverlay>
  );
}
