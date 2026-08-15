import React, { useState, useEffect, useRef, useCallback } from 'react';
import { X, Play, MagicWand, Trophy, CheckCircle, FloppyDisk, ChartLine, Cloud, Desktop, Gear, ClockCounterClockwise } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import AssetPicker from './AssetPicker';
import useInstruments from '../hooks/useInstruments';
import { fmtShort } from '../lib/time';
import { authHeaders, isAdmin } from '../auth';
import SafeOverlay from './SafeOverlay';
import LocalWorkerPanel from './LocalWorkerPanel';
import BenchmarkBar from './BenchmarkBar';
import TIMEFRAMES, { RULE_TIMEFRAMES } from '../constants/timeframes';
import EquityChart from './EquityChart';
import DynamicResult from './DynamicResult';
import DynamicPanel from './DynamicPanel';
import LearningPanel from './LearningPanel';
import { INDICATOR_GROUPS, INDICATOR_POOL } from '../lib/indicatorPool';
import './Optimizer.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

const MODES = [
  { id: 'params', title: 'Parameter-Optimierung', desc: 'Testet viele Parameter-Kombinationen einer bestehenden Strategie und findet die besten Einstellungen (inkl. TP/SL).' },
  { id: 'discovery', title: 'Strategie-Discovery', desc: 'Baut eine neue Strategie: fügt Regel für Regel den Indikator hinzu, der die Winrate am stärksten verbessert.' },
  { id: 'combo', title: 'Discovery + Optimierung', desc: 'Erst neue Strategie entdecken, dann die Schwellenwerte per Feintuning weiter optimieren.' },
  { id: 'dynamic', title: 'Dynamische Strategie', desc: 'Erkennt Marktregime automatisch (ohne Lookahead) und sucht pro Marktphase eine eigene Strategie – wahlweise nur eigene Trade-Parameter oder komplett eigene Regeln. Jede Phase wird per Walk-Forward geprüft; Vergleich gegen die statische Benchmark ist immer aktiv.' },
  { id: 'explore', title: 'Endlos-Suche', desc: 'Sucht im Hintergrund so lange neue Indikator-Kombinationen (mit frischem Zufalls-Seed je Lauf), bis genug Kombis Training UND Walk-Forward mit ähnlich gutem Ergebnis bestehen. Positive Kombis werden feinjustiert, Champions in der Top-5 gesammelt. Jederzeit stoppbar – das Beste bleibt erhalten.' },
];

const OBJECTIVES = [
  { v: 'combo', l: 'Kombi (PnL × Winrate)' },
  { v: 'win_rate', l: 'Höchste Win-Rate' },
  { v: 'pnl', l: 'Höchster PnL' },
];

const ALGORITHMS = [
  { v: 'random', l: 'Random Search' },
  { v: 'bayes', l: 'Bayes (TPE) – schneller zum Optimum' },
];

const OPT_GROUPS = [
  { k: 'tpsl', l: 'TP/SL optimieren', d: 'TP1/Full-CRV, SL-Modus (Struktur/ATR/Fest), SL-Lookback, ATR-Puffer, TP1-%' },
  { k: 'breakeven', l: 'Break-Even optimieren', d: 'BE-Modus (TP1/CRV/Gewinn-%) + Trigger' },
  { k: 'profit_secure', l: 'Gewinnsicherung optimieren', d: 'An/Aus, Auslöser-%, gesicherter Anteil' },
  { k: 'leverage', l: 'Hebel optimieren', d: 'Fester Hebel 3x–50x' },
  { k: 'auto_leverage', l: 'Auto-Leverage optimieren', d: 'An/Aus, Modus (% oder Ticks hinter Stop), Abstand, Max-Hebel' },
  { k: 'sessions', l: 'Zeitfenster optimieren', d: '24/7 vs. typische Handelsfenster' },
];

const DAY_OPTIONS = [1, 2, 3, 5, 7, 14, 30, 60, 90, 180, 360, 540, 720, 900, 1080, 1440,
  1800, 2160, 2520, 2880, 3240, 3600, 3960, 4320, 4680, 5040, 5400];

const CT_CHUNK_OPTIONS = [7, 15, 30, 50, 90];

const STATE_KEY = 'opt_ui_state_v1';
const loadState = () => {
  try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; } catch { return {}; }
};

const fmtEta = (s) => {
  if (s === null || s === undefined) return '';
  if (s >= 3600) return `~${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}min`;
  if (s >= 60) return `~${Math.floor(s / 60)}min ${s % 60}s`;
  return `~${s}s`;
};

export default function Optimizer({ onClose }) {
  const saved = useRef(loadState()).current;
  const [mode, setMode] = useState(saved.mode || 'params');
  const [strategies, setStrategies] = useState([]);
  const [selStrategy, setSelStrategy] = useState(saved.selStrategy || '');
  const [selCoins, setSelCoins] = useState(saved.selCoins || []);
  const { symbols: allSymbols } = useInstruments();
  const [days, setDays] = useState(saved.days ?? 3);
  const [timeframe, setTimeframe] = useState(saved.timeframe || '1m');
  const [optSessions, setOptSessions] = useState(saved.optSessions || '');
  const [objective, setObjective] = useState(saved.objective || 'combo');
  const [iterations, setIterations] = useState(saved.iterations ?? 40);
  const [minTrades, setMinTrades] = useState(saved.minTrades ?? 10);
  const [maxRules, setMaxRules] = useState(saved.maxRules ?? 4);
  const [deepTest, setDeepTest] = useState(saved.deepTest ?? false);
  const [deepDepth, setDeepDepth] = useState(saved.deepDepth || 'deep');
  const [exploreChamps, setExploreChamps] = useState(saved.exploreChamps ?? 5);
  const [exploreMaxMin, setExploreMaxMin] = useState(saved.exploreMaxMin ?? 0);
  const [indicators, setIndicators] = useState(saved.indicators || INDICATOR_POOL.map(i => i.id));
  const [optFlags, setOptFlags] = useState(saved.optFlags || { tpsl: true });
  const [ruleTf, setRuleTf] = useState(saved.ruleTf || { enabled: false, min: '1m', max: '4h' });
  const [algorithm, setAlgorithm] = useState(saved.algorithm || 'random');
  const [baseStrategy, setBaseStrategy] = useState(saved.baseStrategy || '');
  const [updateBase, setUpdateBase] = useState(false);
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [saveName, setSaveName] = useState('');
  const [applied, setApplied] = useState(false);
  const [showApplyChoice, setShowApplyChoice] = useState(false);
  const [applying, setApplying] = useState(false);
  const [overrides, setOverrides] = useState([]);
  const [ram, setRam] = useState(null);
  // Equity-Chart im Optimizer: Standard AUS (Performance), on-demand geladen.
  const [showEquity, setShowEquity] = useState(false);
  const [equityScope, setEquityScope] = useState('optimized'); // 'optimized' | 'all'
  const [equityPoints, setEquityPoints] = useState(null);
  const [equityLoading, setEquityLoading] = useState(false);
  const [equityJobId, setEquityJobId] = useState(null);
  const [execution, setExecution] = useState(saved.execution || 'cloud');
  // ---- Robustheit: Walk-Forward, Drawdown-Filter, Konstanz-Test ----
  const [wfEnabled, setWfEnabled] = useState(!!saved.wfEnabled);
  const [wfMode, setWfMode] = useState(['rolling', 'anchored'].includes(saved.wfMode) ? saved.wfMode : 'single');
  const [wfWindows, setWfWindows] = useState(saved.wfWindows ?? 4);
  const [wfTrainPct, setWfTrainPct] = useState(saved.wfTrainPct ?? 75);
  const [ddEnabled, setDdEnabled] = useState(!!saved.ddEnabled);
  const [ddMaxPct, setDdMaxPct] = useState(saved.ddMaxPct ?? 40);
  const [ctEnabled, setCtEnabled] = useState(!!saved.ctEnabled);
  const [ctChunkDays, setCtChunkDays] = useState(saved.ctChunkDays ?? 30);
  const [ctMaxDev, setCtMaxDev] = useState(saved.ctMaxDev ?? 20);
  const [stEnabled, setStEnabled] = useState(!!saved.stEnabled);
  const [stMult, setStMult] = useState(saved.stMult ?? 1.5);
  const [sbEnabled, setSbEnabled] = useState(!!saved.sbEnabled);
  const [sbVar, setSbVar] = useState(saved.sbVar ?? 10);
  const [mcEnabled, setMcEnabled] = useState(!!saved.mcEnabled);
  const [mcRuns, setMcRuns] = useState(saved.mcRuns ?? 200);
  const [rgEnabled, setRgEnabled] = useState(!!saved.rgEnabled);
  // ---- Dynamische Strategie (Regime-basiert) ----
  const [dynMaxRegimes, setDynMaxRegimes] = useState(saved.dynMaxRegimes ?? 5);
  const [dynLookback, setDynLookback] = useState(saved.dynLookback ?? 3);
  const [dynConfMin, setDynConfMin] = useState(saved.dynConfMin ?? 70);
  const [dynMinHold, setDynMinHold] = useState(saved.dynMinHold ?? 2);
  const [dynTrainPct, setDynTrainPct] = useState(saved.dynTrainPct ?? 75);
  const [dynRuleVariants, setDynRuleVariants] = useState(!!saved.dynRuleVariants);
  const [dynPerRegime, setDynPerRegime] = useState(!!saved.dynPerRegime);
  const [dynMaxRules, setDynMaxRules] = useState(saved.dynMaxRules ?? 4);
  const [dynStartFromBase, setDynStartFromBase] = useState(!!saved.dynStartFromBase);
  const [selTop, setSelTop] = useState(0);
  // ---- Verlauf (Robustheit über alle Läufe) ----
  const [showHistory, setShowHistory] = useState(false);
  const [historyRows, setHistoryRows] = useState(null);
  const [lwOnline, setLwOnline] = useState(false);
  const [showLW, setShowLW] = useState(false);
  const pollRef = useRef(null);

  // ---- QoL: Auswahl lokal merken (bleibt beim Schließen/Neuöffnen erhalten) ----
  useEffect(() => {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({
        mode, selStrategy, selCoins, days, timeframe, objective, iterations,
        minTrades, maxRules, indicators, optFlags, ruleTf, algorithm, baseStrategy, optSessions,
        deepTest, deepDepth, exploreChamps, exploreMaxMin,
        execution, wfEnabled, wfTrainPct, wfMode, wfWindows,
        ddEnabled, ddMaxPct, ctEnabled, ctChunkDays, ctMaxDev,
        stEnabled, stMult, sbEnabled, sbVar, mcEnabled, mcRuns, rgEnabled,
        dynMaxRegimes, dynLookback, dynConfMin, dynMinHold, dynTrainPct, dynRuleVariants,
        dynPerRegime, dynMaxRules, dynStartFromBase,
      }));
    } catch { /* ignore */ }
  }, [mode, selStrategy, selCoins, days, timeframe, objective, iterations,
    minTrades, maxRules, indicators, optFlags, ruleTf, algorithm, baseStrategy, optSessions,
    deepTest, deepDepth, exploreChamps, exploreMaxMin,
    execution, wfEnabled, wfTrainPct, wfMode, wfWindows,
    ddEnabled, ddMaxPct, ctEnabled, ctChunkDays, ctMaxDev,
    stEnabled, stMult, sbEnabled, sbVar, mcEnabled, mcRuns, rgEnabled,
    dynMaxRegimes, dynLookback, dynConfMin, dynMinHold, dynTrainPct, dynRuleVariants,
    dynPerRegime, dynMaxRules, dynStartFromBase]);

  // ---- Lokaler Worker: Online-Status für die Ausführungs-Auswahl ----
  useEffect(() => {
    const check = () => fetch(`${API_URL}/api/localworker/status`).then(r => r.json())
      .then(d => setLwOnline(!!d.online)).catch(() => setLwOnline(false));
    check();
    const iv = setInterval(check, 10000);
    return () => clearInterval(iv);
  }, []);

  const loadRam = () => {
    fetch(`${API_URL}/api/system/ram`).then(r => r.json()).then(setRam).catch(() => {});
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

  const loadEquity = async (scope) => {
    if (!equityJobId) { toast.error('Keine Job-ID – bitte Optimierung neu starten'); return; }
    setEquityLoading(true);
    setEquityScope(scope);
    setEquityPoints(null); // sofort "lädt..."-Zustand zeigen, kein stale render zwischen scope-Wechsel
    try {
      const r = await fetch(`${API_URL}/api/optimizer/equity/${equityJobId}?scope=${scope}`);
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Equity-Simulation fehlgeschlagen'); return; }
      setEquityPoints(d.points || []);
      if ((d.points || []).length === 0) {
        toast.info('Keine geschlossenen Trades im simulierten Zeitraum');
      }
    } catch { toast.error('Verbindungsfehler bei Equity-Simulation'); }
    finally { setEquityLoading(false); }
  };

  const toggleEquity = async () => {
    const next = !showEquity;
    setShowEquity(next);
    if (next && !equityPoints) await loadEquity('optimized');
  };

  const loadOverrides = useCallback((sid) => {
    if (!sid) { setOverrides([]); return; }
    fetch(`${API_URL}/api/optimizer/overrides/${sid}`)
      .then(r => r.json())
      .then(d => setOverrides(d.symbols || []))
      .catch(() => setOverrides([]));
  }, []);

  useEffect(() => {
    if (mode === 'params' || mode === 'dynamic') loadOverrides(selStrategy);
  }, [selStrategy, mode, loadOverrides]);

  // Vorauswahl absichern, sobald das Asset-Universum geladen ist
  useEffect(() => {
    if (!allSymbols.length) return;
    setSelCoins(prev => {
      const valid = (prev || []).filter(c => allSymbols.includes(c));
      return valid.length ? valid : allSymbols.slice(0, 1);
    });
  }, [allSymbols]);

  const recTf = strategies.find(s => s.id === selStrategy)?.timeframe;
  const selIsCustom = !!strategies.find(s => s.id === selStrategy)?.is_custom;

  useEffect(() => {
    if ((mode === 'params' || mode === 'dynamic') && recTf) setTimeframe(recTf);
  }, [selStrategy, mode, recTf]);

  useEffect(() => {
    fetch(`${API_URL}/api/strategies`).then(r => r.json()).then(d => {
      const list = d.strategies || [];
      setStrategies(list);
      setSelStrategy(prev => (prev && list.some(s => s.id === prev)) ? prev : (list[0]?.id || ''));
    });
    fetch(`${API_URL}/api/optimizer/results?limit=1`).then(r => r.json()).then(d => {
      const last = (d.results || [])[0];
      if (last?.result) {
        setResult(last.result);
        if (last.id) setEquityJobId(last.id);
      }
    }).catch(() => {});
    // Läuft gerade eine Optimierung? -> Fortschritt & Abbrechen wieder anzeigen
    fetch(`${API_URL}/api/optimizer/active`).then(r => r.json()).then(d => {
      if (d.active) { setJob(d.active); poll(d.active.id); }
    }).catch(() => {});
    loadRam();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleCoin = (c) =>
    setSelCoins(selCoins.includes(c) ? selCoins.filter(x => x !== c) : [...selCoins, c]);

  const toggleInd = (id) =>
    setIndicators(indicators.includes(id) ? indicators.filter(x => x !== id) : [...indicators, id]);

  const toggleOpt = (k) =>
    setOptFlags(prev => ({ ...prev, [k]: !prev[k] }));

  const poll = useCallback((jobId) => {
    pollRef.current = setInterval(async () => {
      try {
        const j = await fetch(`${API_URL}/api/optimizer/status/${jobId}`).then(r => r.json());
        setJob(j);
        if (j.status === 'done') {
          clearInterval(pollRef.current);
          setResult(j.result);
          setEquityJobId(jobId);
          setEquityPoints(null);
          setShowEquity(false);
          setApplied(false);
          setSelTop(0);
          toast.success('Optimierung abgeschlossen');
        } else if (j.status === 'error') {
          clearInterval(pollRef.current);
          toast.error(`Optimierung fehlgeschlagen: ${j.error}`);
        } else if (j.status === 'cancelled') {
          clearInterval(pollRef.current);
          toast.info('Optimierung abgebrochen');
        }
      } catch { /* keep polling */ }
    }, 1500);
  }, []);

  const cancel = async () => {
    if (!job?.id) return;
    try {
      await fetch(`${API_URL}/api/optimizer/cancel/${job.id}`, {
        method: 'POST', headers: authHeaders(),
      });
      toast.info('Abbruch angefordert...');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const stopExplore = async () => {
    if (!job?.id) return;
    try {
      const r = await fetch(`${API_URL}/api/optimizer/explore/stop/${job.id}`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Stop fehlgeschlagen'); return; }
      toast.info('Suche wird beendet – beste Ergebnisse werden behalten...');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const forceReset = async () => {
    try {
      await fetch(`${API_URL}/api/optimizer/reset`, { method: 'POST', headers: authHeaders() });
      if (pollRef.current) clearInterval(pollRef.current);
      setJob(null);
      toast.success('Optimizer zurückgesetzt – neue Läufe sind wieder möglich');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const run = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (!selCoins.length) { toast.error('Mind. 1 Coin wählen'); return; }
    if ((mode === 'params' || mode === 'dynamic') && !selStrategy) { toast.error('Strategie wählen'); return; }
    if ((mode === 'discovery' || mode === 'combo' || mode === 'explore') && indicators.length === 0) { toast.error('Mind. 1 Indikator anhaken'); return; }
    if (execution === 'local' && !lwOnline) {
      toast.error('Kein lokaler Worker verbunden – Worker starten oder Cloud wählen');
      setShowLW(true);
      return;
    }
    try {
      const res = await fetch(`${API_URL}/api/optimizer/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          mode, strategy_id: selStrategy, symbols: selCoins, days, timeframe,
          objective, iterations, min_trades: minTrades, max_rules: maxRules,
          indicators: (mode === 'params' || (mode === 'dynamic' && !dynRuleVariants && !dynPerRegime)) ? undefined : indicators,
          optimize: optFlags,
          include_trade_params: !!optFlags.tpsl,
          rule_timeframes: ruleTf.enabled && ((mode === 'params') || ['discovery', 'combo', 'explore', 'dynamic'].includes(mode))
            ? { enabled: true, min: ruleTf.min, max: ruleTf.max }
            : undefined,
          algorithm,
          sessions: optSessions.trim() || undefined,
          base_strategy_id: (mode === 'discovery' || mode === 'combo' || mode === 'explore') && baseStrategy ? baseStrategy : undefined,
          deep_test: (mode === 'discovery' || mode === 'combo') ? deepTest : undefined,
          deep_depth: (mode === 'discovery' || mode === 'combo') && deepTest ? deepDepth : undefined,
          explore: mode === 'explore'
            ? { target_champions: exploreChamps, max_minutes: exploreMaxMin }
            : undefined,
          execution,
          dynamic: mode === 'dynamic'
            ? { max_regimes: dynMaxRegimes, lookback_days: dynLookback,
              confidence_min: dynConfMin, min_hold_days: dynMinHold,
              rule_variants: dynRuleVariants, per_regime_strategies: dynPerRegime,
              max_rules_per_regime: dynMaxRules, start_from_base: dynStartFromBase }
            : undefined,
          walk_forward: mode === 'dynamic'
            ? { enabled: true, train_pct: dynTrainPct, mode: 'single' }
            : mode === 'explore'
              ? { enabled: true, train_pct: wfTrainPct, mode: 'single' }
              : (wfEnabled
              ? { enabled: true, train_pct: wfTrainPct, mode: wfMode,
                windows: wfMode !== 'single' ? wfWindows : undefined }
              : undefined),
          dd_filter: ddEnabled ? { enabled: true, max_dd_pct: ddMaxPct } : undefined,
          constancy: ctEnabled
            ? { enabled: true, chunk_days: ctChunkDays, max_deviation_pct: ctMaxDev }
            : undefined,
          stress_test: stEnabled ? { enabled: true, cost_multiplier: stMult } : undefined,
          stability: sbEnabled ? { enabled: true, variation_pct: sbVar } : undefined,
          monte_carlo: mcEnabled ? { enabled: true, runs: mcRuns } : undefined,
          regime_analysis: rgEnabled ? { enabled: true } : undefined,
        }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      setResult(null);
      setApplied(false);
      setJob({ id: d.job_id, status: 'running', progress: 0, phase: 'Startet...' });
      poll(d.job_id);
    } catch { toast.error('Verbindungsfehler'); }
  };

  const applyParams = async (scope) => {
    const best = selEntry || result?.best;
    if (!best) return;
    setApplying(true);
    try {
      const res = await fetch(`${API_URL}/api/optimizer/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ type: 'params', strategy_id: result.strategy_id,
          params: best.params, trade_params: best.trade_params,
          timeframe: result.timeframe,
          scope, symbols: scope === 'coins' ? result.symbols : undefined }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Übernahme fehlgeschlagen'); return; }
      setApplied(true);
      setShowApplyChoice(false);
      loadOverrides(result.strategy_id);
      toast.success(scope === 'coins'
        ? `Einstellungen für ${(result.symbols || []).map(s => s.replace('USDT', '')).join(', ')} übernommen (Coin-spezifisch)`
        : 'Einstellungen global für alle Coins übernommen');
    } catch { toast.error('Verbindungsfehler'); }
    finally { setApplying(false); }
  };

  const applyToBacktester = async () => {
    const best = selEntry || result?.best;
    if (!best) return;
    try {
      const res = await fetch(`${API_URL}/api/optimizer/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ type: 'backtest', strategy_id: result.strategy_id,
          params: best.params, trade_params: best.trade_params, timeframe: result.timeframe }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Übernahme fehlgeschlagen'); return; }
      toast.success('In Backtester übernommen – Strategie dort auswählen & testen');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const saveStrategy = async () => {
    const def = selEntry?.definition || result?.definition;
    if (!def) return;
    const tp = selEntry ? selEntry.trade_params : result.trade_params;
    try {
      const res = await fetch(`${API_URL}/api/optimizer/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ type: 'strategy', definition: def,
          name: saveName || undefined, timeframe: result.timeframe,
          sessions: result.sessions || undefined,
          trade_params: (tp && Object.keys(tp).length ? tp : undefined),
          update_strategy_id: updateBase && result.base_strategy_id ? result.base_strategy_id : undefined }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Speichern fehlgeschlagen'); return; }
      setApplied(true);
      toast.success(d.updated
        ? 'Basis-Strategie aktualisiert – Änderungen sind sofort aktiv'
        : 'Strategie gespeichert & aktiviert – sichtbar in den Strategie-Tabs');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const running = job?.status === 'running';
  // Top-5-Auswahl: der ausgewählte Kandidat wird beim Übernehmen/Speichern verwendet
  const top5 = result?.top5 || [];
  const selEntry = top5.length ? top5[Math.min(selTop, top5.length - 1)] : null;

  const toggleHistory = async () => {
    const next = !showHistory;
    setShowHistory(next);
    if (next) {
      try {
        const r = await fetch(`${API_URL}/api/optimizer/history?limit=30`);
        const d = await r.json();
        setHistoryRows(d.history || []);
      } catch { setHistoryRows([]); }
    }
  };

  const loadRun = async (id) => {
    try {
      const r = await fetch(`${API_URL}/api/optimizer/result/${id}`);
      if (!r.ok) { toast.error('Ergebnis nicht gefunden'); return; }
      const d = await r.json();
      setResult(d.result);
      setEquityJobId(id);
      setEquityPoints(null);
      setShowEquity(false);
      setApplied(false);
      setSelTop(0);
      toast.success('Lauf aus dem Verlauf geladen');
    } catch { toast.error('Laden fehlgeschlagen'); }
  };
  const metricsRow = (m) => m ? (
    <>
      <span>{m.trades} Trades</span>
      <span className={m.win_rate >= 50 ? 'pos' : 'neg'}>{fmt(m.win_rate, 1)}% WR</span>
      <span className={`mono ${m.pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(m.pnl)} PnL</span>
      {m.pnl_pct !== undefined && (
        <span className={`mono ${m.pnl_pct >= 0 ? 'pos' : 'neg'}`}>{fmt(m.pnl_pct, 1)}% PnL</span>
      )}
      <span className="mono neg">DD {fmt(m.max_drawdown)}</span>
      {m.max_drawdown_pct !== undefined && (
        <span className="mono neg">DD {fmt(m.max_drawdown_pct, 1)}%</span>
      )}
    </>
  ) : null;

  const tradeParamPills = (tp) => Object.entries(tp || {}).map(([k, v]) => (
    <span key={k} className="opt-param-pill trade">{k}: <b>{String(v)}</b></span>
  ));

  return (
    <SafeOverlay className="opt-overlay" onClose={onClose}>
      <div className="opt-panel" onClick={e => e.stopPropagation()} data-testid="optimizer-modal">
        <div className="opt-header">
          <h2><MagicWand size={20} weight="bold" style={{ color: '#B388FF' }} /> STRATEGIE-OPTIMIZER</h2>
          <button className="opt-close" onClick={onClose} data-testid="optimizer-close"><X size={22} weight="bold" /></button>
        </div>

        <div className="opt-row" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}
          data-testid="opt-ram-row">
          <span style={{ fontSize: 11, color: '#8A8FA3' }} data-testid="opt-ram-info">
            {ram ? `RAM Backend: ${ram.process_rss_mb} MB · System: ${ram.system_used_percent}% belegt · Kerzen-Cache: ${(ram.candle_cache?.total_candles || 0).toLocaleString('de-DE')} Kerzen (~${ram.candle_cache?.estimated_mb} MB)` : 'RAM-Info lädt...'}
          </span>
          <button className="opt-chip" style={{ fontSize: 11 }} onClick={loadRam} data-testid="opt-ram-refresh">↻ RAM</button>
          <button className="opt-chip" style={{ fontSize: 11 }} onClick={clearCache} data-testid="opt-clear-cache">Cache leeren</button>
        </div>

        <div className="opt-modes">
          {MODES.map(m => (
            <button key={m.id} className={`opt-mode ${mode === m.id ? 'on' : ''}`}
              onClick={() => setMode(m.id)} data-testid={`opt-mode-${m.id}`}>
              <div className="opt-mode-title">{m.title}</div>
              <div className="opt-mode-desc">{m.desc}</div>
            </button>
          ))}
        </div>

        <div className="opt-setup">
          {(mode === 'params' || mode === 'dynamic') && (
            <label className="opt-field">Strategie
              <select value={selStrategy} onChange={e => setSelStrategy(e.target.value)} data-testid="opt-strategy">
                {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
          )}
          <label className="opt-field">Timeframe
            {(mode === 'params' || mode === 'dynamic') && recTf && (
              <span data-testid="opt-rec-tf" style={{ color: '#B388FF', fontSize: '0.8em', marginLeft: 6 }}>
                (Empfohlen: {TIMEFRAMES.find(t => t.v === recTf)?.l || recTf})
              </span>
            )}
            <select value={timeframe} onChange={e => setTimeframe(e.target.value)} data-testid="opt-timeframe">
              {TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}
            </select>
          </label>
          <label className="opt-field">Zeitraum
            <select value={days} onChange={e => setDays(parseInt(e.target.value))} data-testid="opt-days">
              {DAY_OPTIONS.map(d => <option key={d} value={d}>{`${d} Tag${d > 1 ? 'e' : ''}`}</option>)}
            </select>
          </label>
          <label className="opt-field">Zeitfenster (optional)
            <input type="text" placeholder="z.B. 15:00-18:00 · leer = 24h" value={optSessions}
              onChange={e => setOptSessions(e.target.value)} data-testid="opt-sessions"
              title="Festes Handels-Zeitfenster (Berlin-Zeit) für die Optimierung vorgeben, z.B. 15:00-18:00 oder 09:00-12:00,15:00-18:00" />
          </label>
          {mode === 'params' && (
            <label className="opt-field">Algorithmus
              <select value={algorithm} onChange={e => setAlgorithm(e.target.value)} data-testid="opt-algorithm">
                {ALGORITHMS.map(a => <option key={a.v} value={a.v}>{a.l}</option>)}
              </select>
            </label>
          )}
          {mode !== 'params' && mode !== 'dynamic' && (
            <label className="opt-field">Basis-Strategie (weiterentwickeln)
              <select value={baseStrategy} onChange={e => setBaseStrategy(e.target.value)} data-testid="opt-base-strategy">
                <option value="">– Neue Strategie von Null –</option>
                {strategies.filter(s => s.is_custom).map(s =>
                  <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
          )}
          <label className="opt-field">Ziel
            <select value={objective} onChange={e => setObjective(e.target.value)} data-testid="opt-objective">
              {OBJECTIVES.map(o => <option key={o.v} value={o.v}>{o.l}</option>)}
            </select>
          </label>
          <label className="opt-field">Min. Trades
            <input type="number" min={1} value={minTrades}
              onChange={e => setMinTrades(parseInt(e.target.value) || 1)} data-testid="opt-min-trades" />
          </label>
          <label className="opt-field">Iterationen
            <input type="number" min={5} max={300} value={iterations}
              onChange={e => setIterations(parseInt(e.target.value) || 40)} data-testid="opt-iterations" />
          </label>
          {mode !== 'params' && mode !== 'dynamic' && (
            <label className="opt-field">Max. Regeln
              <input type="number" min={1} max={6} value={maxRules}
                onChange={e => setMaxRules(parseInt(e.target.value) || 4)} data-testid="opt-max-rules" />
            </label>
          )}
        </div>

        {(mode === 'discovery' || mode === 'combo') && (
          <div className="opt-row" data-testid="deep-test-row">
            <label className="opt-check"
              title="Statt der schnellen Greedy-Suche werden ALLE Indikator-Paare geprüft, die besten Pfade parallel weiterverfolgt (Beam-Suche), die Favoriten je mit den eingestellten Iterationen feinjustiert und am Ende jede Regel gegen jede Alternative getauscht. Findet auch Kombis, deren Einzelteile schwach sind. Dauert deutlich länger.">
              <input type="checkbox" checked={deepTest}
                onChange={e => setDeepTest(e.target.checked)} data-testid="opt-deep-test" />
              {' '}Deep-Test (alle Kombinationen statt Greedy · viel gründlicher, dauert länger)
            </label>
            {deepTest && (
              <label className="opt-field" style={{ marginLeft: 12 }}
                title="deep: Beam-Breite 6, bis 900 Paare · extrem: Beam-Breite 10, bis 2500 Paare und mehr Feintuning">
                Tiefe
                <select value={deepDepth} onChange={e => setDeepDepth(e.target.value)}
                  data-testid="opt-deep-depth">
                  <option value="deep">deep (gründlich)</option>
                  <option value="extreme">extrem (maximale Suche)</option>
                </select>
              </label>
            )}
            {deepTest && (
              <span className="opt-small" data-testid="deep-test-hint">
                Phasen: Einzeltest → alle Paare → Beam-Suche → Feintuning ({iterations} Iterationen je Favorit) → Austausch → Auswertung
              </span>
            )}
          </div>
        )}
        {mode === 'explore' && (
          <div className="opt-row" data-testid="explore-settings">
            <div className="opt-label">ENDLOS-SUCHE – EINSTELLUNGEN</div>
            <div className="opt-setup" style={{ marginTop: 6 }}>
              <label className="opt-field" title="Die Suche stoppt automatisch, sobald so viele Kombinationen Training UND Walk-Forward bestanden haben. Die besten 5 werden immer behalten (auch über mehrere Läufe hinweg).">
                Champions-Ziel
                <input type="number" min={1} max={10} value={exploreChamps}
                  onChange={e => setExploreChamps(parseInt(e.target.value) || 5)}
                  data-testid="explore-target-champions" />
              </label>
              <label className="opt-field" title="Sicherheits-Zeitlimit in Minuten. 0 = unbegrenzt (läuft bis zum Champions-Ziel oder bis du auf 'Suche beenden' klickst).">
                Zeitlimit (Min. · 0 = unbegrenzt)
                <input type="number" min={0} max={1440} value={exploreMaxMin}
                  onChange={e => setExploreMaxMin(parseInt(e.target.value) || 0)}
                  data-testid="explore-max-minutes" />
              </label>
              <label className="opt-field" title="Anteil der Daten fürs Training – der Rest ist der unbekannte Walk-Forward-Test (Pflicht bei der Endlos-Suche)">
                Training (%)
                <input type="number" min={50} max={95} value={wfTrainPct}
                  onChange={e => setWfTrainPct(parseInt(e.target.value) || 75)}
                  data-testid="explore-train-pct" />
              </label>
            </div>
            <div className="opt-override-legend" style={{ marginTop: 4 }} data-testid="explore-hint">
              Ablauf: zufällige, noch nie getestete Kombis (jeder Lauf mit frischem Seed → nie zweimal dasselbe Ergebnis) →
              positive Kombis werden je {iterations} Iterationen feinjustiert → Walk-Forward auf unbekannten Testdaten →
              Champion nur bei Test-Gewinn UND ähnlicher Qualität wie im Training. Läuft im Hintergrund weiter,
              auch wenn du das Fenster schließt · „Suche beenden“ behält die beste Top-5.
            </div>
          </div>
        )}
        {mode === 'dynamic' && (
          <div className="opt-row" data-testid="dyn-settings">
            <div className="opt-label">
              DYNAMISCHE STRATEGIE – REGIME-EINSTELLUNGEN
              (Anzahl der Regime wird automatisch bestimmt · zu kleine Regime werden zusammengelegt ·
              Erkennung ohne Blick in die Zukunft · Vergleich gegen statische Benchmark ist immer aktiv)
            </div>
            <div className="opt-setup" style={{ marginTop: 6 }}>
              <label className="opt-field">Max. Regime (3–10)
                <input type="number" min={2} max={10} value={dynMaxRegimes}
                  onChange={e => setDynMaxRegimes(parseInt(e.target.value) || 5)} data-testid="dyn-max-regimes" />
              </label>
              <label className="opt-field" title="Rückblick-Fenster für die Markt-Features (Trend, Volatilität, Effizienz, Volumen)">
                Merkmal-Fenster (Tage)
                <input type="number" min={0.5} max={30} step={0.5} value={dynLookback}
                  onChange={e => setDynLookback(parseFloat(e.target.value) || 3)} data-testid="dyn-lookback" />
              </label>
              <label className="opt-field" title="Erst ab dieser Sicherheit wird auf ein anderes Regime umgeschaltet (Anti-Flattern)">
                Umschalt-Sicherheit (%)
                <input type="number" min={50} max={95} value={dynConfMin}
                  onChange={e => setDynConfMin(parseInt(e.target.value) || 70)} data-testid="dyn-conf-min" />
              </label>
              <label className="opt-field" title="Mindestdauer, die ein Regime aktiv bleibt, bevor gewechselt werden darf">
                Mindesthaltedauer (Tage)
                <input type="number" min={0.25} max={30} step={0.25} value={dynMinHold}
                  onChange={e => setDynMinHold(parseFloat(e.target.value) || 2)} data-testid="dyn-min-hold" />
              </label>
              <label className="opt-field" title="Anteil der Daten fürs Training – der Rest bleibt als unbekannter Test für den Vergleich dynamisch vs. statisch">
                Training (%)
                <input type="number" min={50} max={90} value={dynTrainPct}
                  onChange={e => setDynTrainPct(parseInt(e.target.value) || 75)} data-testid="dyn-train-pct" />
              </label>
            </div>
            <div className="opt-override-legend" style={{ marginTop: 4 }}>
              Empfehlung: langen Zeitraum wählen (≥ 90 Tage), damit jedes Regime genügend Trades hat.
              Optimiert werden die oben angehakten Einstellungs-Gruppen (Standard: TP/SL + Hebel) – pro Regime eine eigene Konfiguration.
            </div>
            <label className="opt-check" style={{ marginTop: 6 }} title="Nur für Custom-Strategien: testet pro Regime, ob EINE zusätzliche Regel aus den gewählten Indikatoren die Performance deutlich verbessert (>10%). Kandidaten werden nach dem Lern-Gedächtnis sortiert.">
              <input type="checkbox" checked={dynRuleVariants} disabled={dynPerRegime}
                onChange={e => setDynRuleVariants(e.target.checked)} data-testid="dyn-rule-variants" />
              Regel-Varianten pro Regime testen (nur Custom-Strategien · Indikatoren unten anhaken)
            </label>
            <label className="opt-check" style={{ marginTop: 4 }} title="Statt nur die Trade-Parameter zu justieren, wird pro Marktphase eine KOMPLETT eigene Strategie gesucht (eigene Regeln + eigene TP/SL/Hebel). Ergebnis: pro Regime eine eigenständige Sub-Strategie. Dauert deutlich länger.">
              <input type="checkbox" checked={dynPerRegime}
                onChange={e => { setDynPerRegime(e.target.checked); if (e.target.checked) setDynRuleVariants(false); }}
                data-testid="dyn-per-regime" />
              <b>Eigene Strategie pro Marktphase suchen</b> (volle Regel-Suche je Regime · Indikatoren unten anhaken)
            </label>
            {dynPerRegime && (
              <div className="opt-setup" style={{ marginTop: 6 }}>
                <label className="opt-field" title="Wie viele Regeln darf eine Sub-Strategie maximal bekommen? Mehr Regeln = spezifischer, aber höheres Overfitting-Risiko.">
                  Max. Regeln je Sub-Strategie
                  <input type="number" min={1} max={8} value={dynMaxRules}
                    onChange={e => setDynMaxRules(parseInt(e.target.value) || 4)} data-testid="dyn-max-rules-regime" />
                </label>
                <label className="opt-check" style={{ alignSelf: 'end' }} title="Startet die Regel-Suche bei den Regeln der gewählten Basis-Strategie statt bei null">
                  <input type="checkbox" checked={dynStartFromBase}
                    onChange={e => setDynStartFromBase(e.target.checked)} data-testid="dyn-start-from-base" />
                  Von Basis-Strategie ausgehen
                </label>
              </div>
            )}
          </div>
        )}

        <div className="opt-row">
          <div className="opt-label">
            WAS SOLL MITOPTIMIERT WERDEN? {mode !== 'params' && '(Regeln werden bei Discovery immer optimiert)'}
          </div>
          <div className="opt-chips">
            {OPT_GROUPS.map(g => (
              <button key={g.k} className={`opt-chip ${optFlags[g.k] ? 'on' : ''}`}
                onClick={() => toggleOpt(g.k)} title={g.d} data-testid={`opt-flag-${g.k}`}>
                {optFlags[g.k] ? '☑' : '☐'} {g.l}
              </button>
            ))}
          </div>
          {((mode === 'params' && selIsCustom) || ['discovery', 'combo', 'explore', 'dynamic'].includes(mode)) && (
            <div className="opt-chips" style={{ marginTop: 6, alignItems: 'center' }}>
              <button className={`opt-chip ${ruleTf.enabled ? 'on' : ''}`}
                onClick={() => setRuleTf(v => ({ ...v, enabled: !v.enabled }))}
                data-testid="opt-rule-tf-toggle"
                title="Multi-Timeframe: Regeln dürfen einen eigenen (höheren) Timeframe bekommen – z.B. Trend-Filter auf 1h, Entry auf 1m. Wird NICHT erzwungen: der Strategie-Timeframe bleibt immer eine Option und gewinnt, wenn er besser ist.">
                {ruleTf.enabled ? '☑' : '☐'} Regel-Timeframes optimieren (Multi-Timeframe)
              </button>
              {ruleTf.enabled && (
                <>
                  <label style={{ fontSize: 11, color: '#8A8FA3' }}>von{' '}
                    <select value={ruleTf.min} onChange={e => setRuleTf(v => ({ ...v, min: e.target.value }))} data-testid="opt-rule-tf-min">
                      {RULE_TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}
                    </select>
                  </label>
                  <label style={{ fontSize: 11, color: '#8A8FA3' }}>bis{' '}
                    <select value={ruleTf.max} onChange={e => setRuleTf(v => ({ ...v, max: e.target.value }))} data-testid="opt-rule-tf-max">
                      {RULE_TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}
                    </select>
                  </label>
                </>
              )}
            </div>
          )}
          <div className="opt-override-legend" style={{ marginTop: 4 }}>
            Achtung: Jede zusätzliche Gruppe vergrößert den Suchraum – ggf. mehr Iterationen wählen.
          </div>
        </div>

        <div className="opt-row" style={mode === 'dynamic' ? { display: 'none' } : undefined}>
          <div className="opt-label">ROBUSTHEIT &amp; WALK-FORWARD (optional)</div>
          <div className="opt-chips">
            <button className={`opt-chip ${wfEnabled ? 'on' : ''}`} onClick={() => setWfEnabled(v => !v)}
              data-testid="opt-wf-toggle"
              title="Strategie wird nur auf den Trainingsdaten gefunden/optimiert und danach auf unbekannten Testdaten geprüft. Der WF-Score bevorzugt Strategien, die auf beiden Datensätzen ähnlich gut laufen (Overfitting-Schutz).">
              {wfEnabled ? '☑' : '☐'} Walk-Forward (Training/Test)
            </button>
            <button className={`opt-chip ${ddEnabled ? 'on' : ''}`} onClick={() => setDdEnabled(v => !v)}
              data-testid="opt-dd-toggle"
              title="Strategien aussortieren, deren max. Drawdown im Verhältnis zum PnL zu hoch ist (z.B. max. 40% vom PnL).">
              {ddEnabled ? '☑' : '☐'} Drawdown-Filter
            </button>
            <button className={`opt-chip ${ctEnabled ? 'on' : ''}`} onClick={() => setCtEnabled(v => !v)}
              data-testid="opt-ct-toggle"
              title="Zeitraum in Abschnitte teilen und prüfen, ob der Gewinn gleichmäßig verteilt ist – oder nur aus wenigen Phasen stammt.">
              {ctEnabled ? '☑' : '☐'} Konstanz-Test
            </button>
            <button className={`opt-chip ${stEnabled ? 'on' : ''}`} onClick={() => setStEnabled(v => !v)}
              data-testid="opt-st-toggle"
              title="Kandidat wird zusätzlich mit vervielfachten Gebühren/Slippage getestet und muss profitabel bleiben – deckt Strategien auf, die reale Kosten auffressen würden.">
              {stEnabled ? '☑' : '☐'} Kosten-Stresstest
            </button>
            <button className={`opt-chip ${sbEnabled ? 'on' : ''}`} onClick={() => setSbEnabled(v => !v)}
              data-testid="opt-sb-toggle"
              title="Alle Schwellenwerte werden um ±X% variiert – bleibt das Ergebnis stabil (Plateau), ist die Strategie robust; kippt es, war es ein Zufalls-Spike.">
              {sbEnabled ? '☑' : '☐'} Parameter-Stabilität
            </button>
            <button className={`opt-chip ${mcEnabled ? 'on' : ''}`} onClick={() => setMcEnabled(v => !v)}
              data-testid="opt-mc-toggle"
              title="Trade-Reihenfolge wird viele Male zufällig gemischt – statt einem Drawdown-Wert bekommst du eine Verteilung (p50/p95/worst). Deckt Glücks-Sequenzen auf.">
              {mcEnabled ? '☑' : '☐'} Monte-Carlo
            </button>
            <button className={`opt-chip ${rgEnabled ? 'on' : ''}`} onClick={() => setRgEnabled(v => !v)}
              data-testid="opt-rg-toggle"
              title="PnL getrennt nach Marktphase (Bull/Bär/Seitwärts) ausweisen – nur Info, kein Filter.">
              {rgEnabled ? '☑' : '☐'} Regime-Analyse
            </button>
          </div>
          {(wfEnabled || ddEnabled || ctEnabled || stEnabled || sbEnabled || mcEnabled) && (
            <div className="opt-setup" style={{ marginTop: 8 }} data-testid="opt-robust-settings">
              {wfEnabled && (
                <>
                  <label className="opt-field">Walk-Forward-Variante
                    <div className="opt-wf-mode" data-testid="opt-wf-mode">
                      <button type="button" className={`opt-chip ${wfMode === 'single' ? 'on' : ''}`}
                        onClick={() => setWfMode('single')} data-testid="opt-wf-mode-single"
                        title="Ein Split: Training vorne, Test hinten (schnell)">
                        Einfacher Split
                      </button>
                      <button type="button" className={`opt-chip ${wfMode === 'rolling' ? 'on' : ''}`}
                        onClick={() => setWfMode('rolling')} data-testid="opt-wf-mode-rolling"
                        title="Mehrere gleitende Trainings-/Test-Fenster über den Zeitraum – Goldstandard gegen Overfitting, dauert etwas länger">
                        Rolling (mehrere Fenster)
                      </button>
                      <button type="button" className={`opt-chip ${wfMode === 'anchored' ? 'on' : ''}`}
                        onClick={() => setWfMode('anchored')} data-testid="opt-wf-mode-anchored"
                        title="Wie Rolling, aber das Training beginnt immer am Anfang und WÄCHST mit jedem Fenster – nutzt alle historischen Daten">
                        Anchored (wachsendes Training)
                      </button>
                    </div>
                  </label>
                  {wfMode !== 'single' && (
                    <label className="opt-field">Anzahl Fenster
                      <input type="number" min={2} max={12} value={wfWindows}
                        onChange={e => setWfWindows(parseInt(e.target.value) || 4)}
                        data-testid="opt-wf-windows" />
                      <span className="opt-inline-hint">
                        {wfMode === 'anchored'
                          ? 'Training beginnt immer am Anfang und wächst je Fenster · Test auf den direkt folgenden, unbekannten Daten'
                          : 'Jedes Fenster: eigenes Training + Test auf den direkt folgenden, unbekannten Daten'}
                      </span>
                    </label>
                  )}
                  <label className="opt-field">Trainings-Anteil (%)
                    <input type="number" min={50} max={95} value={wfTrainPct}
                      onChange={e => setWfTrainPct(parseInt(e.target.value) || 75)}
                      data-testid="opt-wf-trainpct" />
                    <span className="opt-inline-hint" data-testid="opt-wf-split-info">
                      {wfMode === 'rolling'
                        ? `${Math.max(2, Math.min(12, wfWindows))} Fenster · Training je ${Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)} Tage · Test je ~${Math.max(Math.round((days - Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)) / Math.max(2, Math.min(12, wfWindows)) * 10) / 10, 0.1)} Tage`
                        : wfMode === 'anchored'
                          ? `${Math.max(2, Math.min(12, wfWindows))} Fenster · Training wächst von ${Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)} auf ~${days - Math.max(Math.round((days - Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)) / Math.max(2, Math.min(12, wfWindows))), 1)} Tage · Test je ~${Math.max(Math.round((days - Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)) / Math.max(2, Math.min(12, wfWindows)) * 10) / 10, 0.1)} Tage`
                          : `Training: ${Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)} Tage · Test: ${days - Math.round(days * Math.min(Math.max(wfTrainPct, 50), 95) / 100)} Tage`}
                    </span>
                  </label>
                </>
              )}
              {ddEnabled && (
                <label className="opt-field">Max. Drawdown (% vom PnL)
                  <input type="number" min={1} max={1000} value={ddMaxPct}
                    onChange={e => setDdMaxPct(parseInt(e.target.value) || 40)}
                    data-testid="opt-dd-maxpct" />
                  <span className="opt-inline-hint">40 = DD darf höchstens 40% des PnL betragen</span>
                </label>
              )}
              {ctEnabled && (
                <>
                  <label className="opt-field">Abschnitts-Länge (Tage)
                    <select value={ctChunkDays} onChange={e => setCtChunkDays(parseInt(e.target.value))}
                      data-testid="opt-ct-chunkdays">
                      {CT_CHUNK_OPTIONS.map(d => <option key={d} value={d}>{d} Tage</option>)}
                    </select>
                  </label>
                  <label className="opt-field">Max. Abweichung (%)
                    <input type="number" min={1} max={1000} value={ctMaxDev}
                      onChange={e => setCtMaxDev(parseInt(e.target.value) || 20)}
                      data-testid="opt-ct-maxdev" />
                    <span className="opt-inline-hint">Streuung der Abschnitts-PnLs · 20% = sehr streng, 100% = locker</span>
                  </label>
                </>
              )}
            </div>
          )}
        </div>

        <div className="opt-row">
          <div className="opt-label">ASSETS</div>
          <AssetPicker selected={selCoins} chipClass="opt-chip" testIdPrefix="opt-coin"
            onToggle={toggleCoin}
            extraClass={(c) => (overrides.includes(c) ? 'has-override' : '')}
            renderExtra={(c) => (overrides.includes(c)
              ? <span className="opt-chip-dot" data-testid={`opt-override-dot-${c}`} /> : null)} />
          {mode === 'params' && overrides.length > 0 && (
            <div className="opt-override-legend" data-testid="opt-override-legend">
              <span className="opt-chip-dot inline" /> Coin-spezifische Einstellungen aktiv: {overrides.map(s => s.replace('USDT', '')).join(', ')}
            </div>
          )}
        </div>

        {(mode === 'discovery' || mode === 'combo' || mode === 'explore' || (mode === 'dynamic' && (dynRuleVariants || dynPerRegime))) && (
          <div className="opt-row">
            <div className="opt-label">
              {mode === 'dynamic'
                ? 'INDIKATOREN FÜR DIE REGEL-VARIANTEN (Häkchen = wird pro Regime getestet)'
                : 'INDIKATOREN FÜR DIE SUCHE (Häkchen = wird getestet · über die Erklärung hovern)'}
            </div>
            <div className="opt-ind-master">
              <button className="opt-chip" data-testid="opt-ind-all-on"
                onClick={() => setIndicators(INDICATOR_POOL.map(i => i.id))}>
                Alle anhaken
              </button>
              <button className="opt-chip" data-testid="opt-ind-all-off"
                onClick={() => setIndicators([])}>
                Alle abwählen
              </button>
              <span className="opt-small" style={{ alignSelf: 'center' }}>
                Gruppen zeigen den typischen Einsatzzweck – beim Deep-Test / der Endlos-Suche dürfen alle Gruppen gleichzeitig aktiv sein.
              </span>
            </div>
            {INDICATOR_GROUPS.map(g => {
              const ids = g.items.map(i => i.id);
              const allOn = ids.every(id => indicators.includes(id));
              return (
                <div key={g.key} className="opt-ind-group" data-testid={`opt-ind-group-${g.key}`}>
                  <div className="opt-ind-group-head">
                    <span className="opt-ind-group-title" title={g.hint}>{g.label}</span>
                    <button className="opt-ind-group-toggle" data-testid={`opt-ind-group-toggle-${g.key}`}
                      onClick={() => setIndicators(allOn
                        ? indicators.filter(x => !ids.includes(x))
                        : [...new Set([...indicators, ...ids])])}>
                      {allOn ? 'abwählen' : 'alle'}
                    </button>
                  </div>
                  <div className="opt-chips">
                    {g.items.map(i => (
                      <button key={i.id} className={`opt-chip ${indicators.includes(i.id) ? 'on' : ''}`}
                        onClick={() => toggleInd(i.id)} title={i.desc} data-testid={`opt-ind-${i.id}`}>
                        {i.label}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="opt-exec-row" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', margin: '10px 0 4px' }}>
          <div className="bt-exec" data-testid="opt-execution-toggle">
            <span className="bt-exec-label">Ausführung</span>
            <button className={`bt-exec-btn ${execution === 'cloud' ? 'on' : ''}`}
              onClick={() => setExecution('cloud')} data-testid="opt-exec-cloud"
              title="Berechnung auf dem Server (wie bisher)">
              <Cloud size={13} weight="bold" /> Cloud
            </button>
            <button className={`bt-exec-btn ${execution === 'local' ? 'on' : ''}`}
              onClick={() => setExecution('local')} data-testid="opt-exec-local"
              title="Berechnung auf deinem PC über den lokalen Worker – identische Ergebnisse, nutzt lokal gespeicherte Kerzendaten">
              <Desktop size={13} weight="bold" /> Lokal
              <span className={`bt-exec-dot ${lwOnline ? 'on' : ''}`} data-testid="opt-exec-dot" />
            </button>
            <button className="bt-exec-manage" onClick={() => setShowLW(true)}
              title="Lokale Ausführung verwalten: Worker, Einstellungen & Marktdaten"
              data-testid="opt-exec-manage">
              <Gear size={13} weight="bold" />
            </button>
          </div>
        </div>
        {showLW && <LocalWorkerPanel onClose={() => setShowLW(false)} />}

        <button className="opt-run" onClick={run} disabled={running} data-testid="opt-run">
          <Play size={15} weight="fill" /> {running ? 'Optimiert...' : 'Optimierung starten'}
        </button>
        <button className={`opt-chip opt-history-btn ${showHistory ? 'on' : ''}`} onClick={toggleHistory}
          data-testid="opt-history-toggle"
          title="Alle bisherigen Läufe mit Robustheits-Kennzahlen (WF-Score, Konsistenz, Konstanz) vergleichen und alte Ergebnisse wieder laden">
          <ClockCounterClockwise size={13} /> Verlauf
        </button>
        <DynamicPanel />
        <LearningPanel />
        {showHistory && (
          <div className="opt-history" data-testid="opt-history">
            <div className="opt-section-title">
              <ClockCounterClockwise size={14} /> VERLAUF – Robustheit über alle Läufe (klicken zum Laden)
            </div>
            {historyRows === null && <div className="opt-small">Lade Verlauf...</div>}
            {historyRows !== null && historyRows.length === 0 && (
              <div className="opt-small">Noch keine gespeicherten Läufe.</div>
            )}
            {(historyRows || []).length > 0 && (
              <table className="opt-history-table">
                <thead>
                  <tr>
                    <th>Datum</th><th>Modus</th><th>Strategie</th><th>TF/Tage</th>
                    <th>PnL</th><th>WR</th><th>WF-Score</th><th>Konsist.</th>
                    <th>Test-PnL</th><th>DD/PnL</th><th>Konstanz</th><th>Checks</th><th>Filter</th>
                  </tr>
                </thead>
                <tbody>
                  {historyRows.map((h, hi) => {
                    const maxWf = Math.max(...historyRows.map(x => Math.abs(x.wf?.wf_score || 0)), 0.01);
                    const wfv = h.wf?.wf_score;
                    return (
                      <tr key={h.id || hi} onClick={() => loadRun(h.id)} data-testid={`opt-history-row-${hi}`}
                        title={`${(h.symbols || []).join(', ')} · Ziel: ${h.objective || '–'}${h.wf_mode ? ` · WF: ${h.wf_mode}` : ''}`}>
                        <td>{fmtShort(h.created_at)}</td>
                        <td>{h.mode || '–'}{h.wf_mode ? ` +WF(${h.wf_mode === 'single' ? 'Split' : h.wf_mode})` : ''}</td>
                        <td className="opt-hist-name">{h.strategy || (h.rules_n ? `${h.rules_n} Regeln` : '–')}</td>
                        <td>{h.timeframe || '–'}/{h.days || '–'}</td>
                        <td className={(h.pnl || 0) > 0 ? 'pos' : 'neg'}>{fmt(h.pnl, 1)}</td>
                        <td>{fmt(h.win_rate, 0)}%</td>
                        <td>
                          {wfv !== undefined && wfv !== null ? (
                            <span className="opt-hist-wf">
                              <span className={`opt-hist-bar ${wfv >= 0 ? 'pos' : 'neg'}`}
                                style={{ width: `${Math.min(Math.abs(wfv) / maxWf * 40, 40)}px` }} />
                              {fmt(wfv, 2)}
                            </span>
                          ) : '–'}
                        </td>
                        <td>{h.wf ? `${fmt(h.wf.consistency_pct, 0)}%${h.wf.positive_windows_pct !== undefined ? ` · ${fmt(h.wf.positive_windows_pct, 0)}%F+` : ''}` : '–'}</td>
                        <td className={(h.test_pnl || 0) > 0 ? 'pos' : (h.test_pnl !== undefined && h.test_pnl !== null ? 'neg' : '')}>{h.test_pnl !== undefined && h.test_pnl !== null ? fmt(h.test_pnl, 1) : '–'}</td>
                        <td>{h.dd_ratio_pct !== undefined && h.dd_ratio_pct !== null ? `${fmt(h.dd_ratio_pct, 0)}%` : '–'}</td>
                        <td>{h.constancy_dev !== undefined && h.constancy_dev !== null ? `${fmt(h.constancy_dev, 0)}%` : '–'}</td>
                        <td>{h.passed === undefined ? '–' : (h.passed ? '✓' : '✗')}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        )}

        {running && (
          <div className="opt-progress" data-testid="opt-progress">
            <div className="opt-progress-bar"><div style={{ width: `${job.progress || 0}%` }} /></div>
            <div className="opt-progress-row">
              <div className="opt-progress-text" data-testid="opt-progress-text">
                {(job.execution === 'local' || job.params?.execution === 'local') &&
                  <span className="bt-exec-tag" data-testid="opt-local-tag">💻 Lokal</span>}
                {job.phase} · {job.progress || 0}%
                {job.eta_seconds != null && <span className="opt-eta"> · Restzeit {fmtEta(job.eta_seconds)}</span>}
              </div>
              {job?.params?.mode === 'explore' && (
                <button className="opt-cancel-run" onClick={stopExplore} data-testid="opt-explore-stop"
                  title="Endlos-Suche sanft beenden: die bis jetzt gefundenen Champions und die Top-5 bleiben erhalten">
                  <CheckCircle size={13} weight="bold" /> Suche beenden &amp; Beste behalten
                </button>
              )}
              <button className="opt-cancel-run" onClick={cancel} data-testid="opt-cancel">
                <X size={13} weight="bold" /> Abbrechen
              </button>
              <button className="opt-cancel-run" onClick={forceReset} data-testid="opt-force-reset"
                title="Notfall: hängende Optimierung sofort freigeben">
                <X size={13} weight="bold" /> Zurücksetzen (Notfall)
              </button>
            </div>
            {job.best?.metrics && (
              <div className="opt-best-live">
                Bester Stand: {metricsRow(job.best.metrics)}
                {job.best?.explore && (
                  <span data-testid="opt-explore-live">
                    {' '}· Champions: <b>{job.best.explore.champions}</b>
                    {' '}· WF-Score <b>{fmt(job.best.explore.wf_score)}</b>
                    {' '}· Test-PnL <b>{fmt(job.best.explore.test_pnl)}</b>
                  </span>
                )}
              </div>
            )}
          </div>
        )}

        {result && !running && (
          <div className="opt-result" data-testid="opt-result">
            {result.explore_report && (
              <div className="opt-row opt-explore-report" data-testid="explore-report">
                <div className="opt-section-title">ENDLOS-SUCHE – AUSWERTUNG</div>
                <div className="opt-small" data-testid="explore-report-stats">
                  <b>{result.explore_report.tested}</b> Kombis getestet ·{' '}
                  <b>{result.explore_report.refined}</b> feinjustiert ·{' '}
                  <b>{result.explore_report.wf_checked}</b> Walk-Forward-geprüft ·{' '}
                  <b>{result.explore_report.champions_found}</b> Champions ·{' '}
                  Laufzeit {Math.round((result.explore_report.elapsed_seconds || 0) / 60 * 10) / 10} min ·{' '}
                  {result.explore_report.combos_per_min} Kombis/min ·{' '}
                  Suchraum: {result.explore_report.total_space?.toLocaleString?.('de-DE') || result.explore_report.total_space} mögliche Kombis
                  ({result.explore_report.space_seen_pct}% erkundet)
                </div>
                <div className="opt-small" style={{ marginTop: 4 }}>
                  Stop-Grund: <b>{{
                    target_reached: 'Champions-Ziel erreicht ✅',
                    stopped_by_user: 'Manuell beendet (Bestes behalten)',
                    time_limit: 'Zeitlimit erreicht',
                    space_exhausted: 'Suchraum vollständig erkundet',
                  }[result.explore_report.stop_reason] || result.explore_report.stop_reason || '–'}</b>
                  {' '}· Champion-Kriterium: Training positiv + Test positiv + Konsistenz ≥ {result.explore_report.min_consistency_pct}%
                </div>
                {(result.explore_report.indicator_stats || []).length > 0 && (
                  <div className="opt-small" style={{ marginTop: 4 }}>
                    Trefferquote je Indikator:{' '}
                    {result.explore_report.indicator_stats.slice(0, 6).map(s =>
                      `${s.label} ${s.positive_pct}% (${s.tried}x)`).join(' · ')}
                  </div>
                )}
                {(result.explore_report.near_misses || []).length > 0 && (
                  <div className="opt-small" style={{ marginTop: 4, opacity: 0.8 }}>
                    Knapp gescheitert: {result.explore_report.near_misses.slice(0, 3).map(nm =>
                      `${(nm.labels || []).join(' + ')} (${nm.reason})`).join(' · ')}
                  </div>
                )}
                {result.explore_report.champions_found === 0 && (
                  <div className="opt-small" style={{ marginTop: 4, color: '#FF9F0A' }}>
                    Noch kein Champion gefunden – länger laufen lassen, mehr Indikatoren anhaken
                    oder Zeitraum/Coins ändern.
                  </div>
                )}
              </div>
            )}
            <div className="opt-equity-toggle" style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', margin: '4px 0 10px' }}>
              <button className={`opt-chip ${showEquity ? 'on' : ''}`}
                onClick={toggleEquity} data-testid="opt-equity-toggle"
                style={{ fontSize: 12, fontWeight: 600 }}>
                <ChartLine size={13} weight="bold" style={{ verticalAlign: -2, marginRight: 4 }} />
                {showEquity ? 'Equity-Kurve ausblenden' : 'Equity-Kurve anzeigen'}
              </button>
              {showEquity && equityJobId && (
                <>
                  <button className={`opt-chip ${equityScope === 'optimized' ? 'on' : ''}`}
                    onClick={() => loadEquity('optimized')} disabled={equityLoading}
                    data-testid="opt-equity-scope-optimized"
                    style={{ fontSize: 11 }}
                    title="Nur die im Lauf verwendeten Coins">
                    Nur optimierte Coins
                  </button>
                  <button className={`opt-chip ${equityScope === 'all' ? 'on' : ''}`}
                    onClick={() => loadEquity('all')} disabled={equityLoading}
                    data-testid="opt-equity-scope-all"
                    style={{ fontSize: 11 }}
                    title="Auch andere Coins simulieren – zeigt wie robust die Strategie ist">
                    Auch andere Coins prüfen
                  </button>
                  {equityLoading && (
                    <span style={{ fontSize: 11, color: '#8A8FA3' }} data-testid="opt-equity-loading">
                      Simuliere…
                    </span>
                  )}
                </>
              )}
              <span style={{ fontSize: 10, color: '#8A8FA3', marginLeft: 4 }}>
                Standard AUS wegen Performance – bei mehreren Coins/Strategien kurz Geduld.
              </span>
            </div>
            {showEquity && (
              <div data-testid="opt-equity-chart-wrap" style={{ marginBottom: 12 }}>
                <EquityChart points={equityPoints}
                  csvHref={equityJobId ? `${API_URL}/api/optimizer/export/${equityJobId}?kind=equity` : null}
                  title={`EQUITY-KURVE · ${equityScope === 'all' ? 'ALLE COINS (Robustheit)' : 'Optimierte Coins'}`} />
                {equityJobId && (
                  <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                    <a href={`${API_URL}/api/optimizer/export/${equityJobId}?kind=trades`}
                      className="opt-chip" data-testid="opt-export-trades">Alle Trades als CSV</a>
                    <a href={`${API_URL}/api/optimizer/export/${equityJobId}?kind=equity`}
                      className="opt-chip" data-testid="opt-export-equity">Equity-Kurve als CSV</a>
                  </div>
                )}
              </div>
            )}
            {result.benchmark && <BenchmarkBar b={result.benchmark} testid="opt-benchmark" />}
            {top5.length > 0 && (
              <div className="opt-top5" data-testid="opt-top5">
                <div className="opt-section-title">
                  <Trophy size={15} weight="fill" style={{ color: '#FFD700' }} />
                  TOP {top5.length} ERGEBNISSE – zum Auswählen anklicken
                  {result.walk_forward && (
                    <span className="opt-wf-tag" data-testid="opt-wf-tag">
                      {result.walk_forward.mode === 'rolling'
                        ? `Rolling Walk-Forward: ${result.walk_forward.windows} Fenster · je ${result.walk_forward.train_days}d Training / ~${result.walk_forward.test_days}d Test`
                        : result.walk_forward.mode === 'anchored'
                          ? `Anchored Walk-Forward: ${result.walk_forward.windows} Fenster · Training wächst ab ${result.walk_forward.train_days}d / ~${result.walk_forward.test_days}d Test je Fenster`
                          : `Walk-Forward: ${result.walk_forward.train_days}d Training / ${result.walk_forward.test_days}d Test`}
                    </span>
                  )}
                </div>
                {top5.map((t, i) => (
                  <button key={i} type="button"
                    className={`opt-top5-card ${selTop === i ? 'sel' : ''} ${t.passed === false ? 'failed' : ''}`}
                    onClick={() => setSelTop(i)} data-testid={`opt-top5-${i}`}>
                    <div className="opt-top5-head">
                      <span className="opt-top5-rank">#{t.rank || i + 1}</span>
                      {t.wf
                        ? <span className="opt-top5-score">
                          WF-Score {fmt(t.wf.wf_score, 2)} · Übereinstimmung {fmt(t.wf.consistency_pct, 0)}%
                          {t.wf.positive_windows_pct !== undefined && ` · ${fmt(t.wf.positive_windows_pct, 0)}% Fenster positiv`}
                        </span>
                        : <span className="opt-top5-score">Score {fmt(t.score, 1)}</span>}
                      {t.dd_ratio_pct !== undefined && (
                        <span className={`opt-badge ${t.dd_pass === false ? 'bad' : 'ok'}`}
                          title="Max. Drawdown in % vom PnL (Training)">
                          DD/PnL {t.dd_ratio_pct != null ? `${fmt(t.dd_ratio_pct, 0)}%` : '–'}
                        </span>
                      )}
                      {t.constancy && (
                        <span className={`opt-badge ${t.constancy.passed ? 'ok' : 'bad'}`}
                          title={`${t.constancy.chunks} Abschnitte · ${fmt(t.constancy.profitable_chunks_pct, 0)}% davon profitabel · Ø ${fmt(t.constancy.mean_pnl)} PnL/Abschnitt`}>
                          Konstanz {t.constancy.deviation_pct != null ? `${fmt(t.constancy.deviation_pct, 0)}%` : '–'}
                        </span>
                      )}
                      {t.stress && (
                        <span className={`opt-badge ${t.stress.passed ? 'ok' : 'bad'}`}
                          title={`PnL bei ${t.stress.cost_multiplier}× Kosten: ${fmt(t.stress.pnl)} (${t.stress.trades ?? 0} Trades, ${fmt(t.stress.win_rate, 0)}% WR)`}>
                          Stress ×{t.stress.cost_multiplier}: {fmt(t.stress.pnl, 1)}
                        </span>
                      )}
                      {t.stability && (
                        <span className={`opt-badge ${t.stability.passed ? 'ok' : 'bad'}`}
                          title={`Schwellen ±${t.stability.variation_pct}%: ${fmt(t.stability.positive_pct, 0)}% der ${t.stability.variants} Varianten profitabel · Ø PnL-Erhalt ${fmt(t.stability.retention_pct, 0)}%`}>
                          Stabil {fmt(t.stability.positive_pct, 0)}%
                        </span>
                      )}
                      {t.monte_carlo && (
                        <span className={`opt-badge ${t.monte_carlo.passed ? 'ok' : 'bad'}`}
                          title={`${t.monte_carlo.runs} gemischte Läufe · DD median ${fmt(t.monte_carlo.dd_p50)} · p95 ${fmt(t.monte_carlo.dd_p95)} (${fmt(t.monte_carlo.dd_p95_pct, 0)}% vom PnL) · worst ${fmt(t.monte_carlo.dd_worst)}`}>
                          MC-DD p95 {t.monte_carlo.dd_p95 != null ? fmt(t.monte_carlo.dd_p95, 1) : '–'}
                        </span>
                      )}
                      {t.passed === false && <span className="opt-badge bad">Filter nicht bestanden</span>}
                      {selTop === i && <span className="opt-badge sel">Ausgewählt ✓</span>}
                    </div>
                    {t.rank_reason && (
                      <div className="opt-rank-reason" data-testid={`opt-rank-reason-${i}`}>{t.rank_reason}</div>
                    )}
                    {(t.fail_reasons || []).length > 0 && (
                      <div className="opt-fail-reasons" data-testid={`opt-fail-reasons-${i}`}>
                        {t.fail_reasons.map((r, ri) => <div key={ri}>✗ {r}</div>)}
                      </div>
                    )}
                    {(t.checks || []).some(c => c.enabled) && (
                      <div className="opt-checks-row" data-testid={`opt-checks-${i}`}>
                        {t.checks.filter(c => c.enabled).map(c => (
                          <span key={c.id}
                            className={`opt-check-chip ${c.passed === false ? 'bad' : c.passed === true ? 'ok' : 'info'}`}
                            title={c.detail}>
                            {c.passed === false ? '✗' : c.passed === true ? '✓' : 'ℹ'} {c.label}
                          </span>
                        ))}
                        <span className="opt-small" style={{ alignSelf: 'center' }}>
                          ✓ bestanden · ✗ nicht bestanden · ℹ Info/Ranking (Details per Mouseover)
                        </span>
                      </div>
                    )}
                    <div className="opt-metrics">
                      {t.test_metrics && <span className="opt-small">Training:</span>}
                      {metricsRow(t.metrics)}
                    </div>
                    {t.test_metrics && (
                      <div className="opt-metrics">
                        <span className="opt-small">Test (unbekannte Daten):</span>
                        {metricsRow(t.test_metrics)}
                      </div>
                    )}
                    {(t.wf_windows || []).length > 0 && (
                      <div className="opt-wf-windows" data-testid={`opt-wf-windows-${i}`}>
                        {t.wf_windows.map((w, wi) => (
                          <span key={wi}
                            className={`opt-wf-win ${(w.test_metrics?.pnl || 0) > 0 ? 'pos' : 'neg'}`}
                            title={`Fenster ${w.window}: Training ${w.range?.train_from || '?'} bis ${w.range?.train_to || '?'} (PnL ${fmt(w.train_metrics?.pnl)}) · Test ${w.range?.test_from || '?'} bis ${w.range?.test_to || '?'} (PnL ${fmt(w.test_metrics?.pnl)}) · WF-Score ${fmt(w.wf_score, 2)}`}>
                            F{w.window}: {fmt(w.test_metrics?.pnl, 1)}
                          </span>
                        ))}
                        <span className="opt-small" style={{ alignSelf: 'center' }}>Test-PnL je Fenster (Details per Mouseover)</span>
                      </div>
                    )}
                    {t.regimes && (
                      <div className="opt-wf-windows" data-testid={`opt-regimes-${i}`}>
                        {[['bull', 'Bull'], ['bear', 'Bär'], ['sideways', 'Seitwärts']].map(([k, label]) => (
                          t.regimes[k] && (
                            <span key={k} className={`opt-wf-win ${(t.regimes[k].pnl || 0) > 0 ? 'pos' : 'neg'}`}
                              title={`${label}-Phase: ${t.regimes[k].trades} Trades`}>
                              {label}: {fmt(t.regimes[k].pnl, 1)}
                            </span>
                          )
                        ))}
                        <span className="opt-small" style={{ alignSelf: 'center' }}>PnL je Marktphase (Training)</span>
                      </div>
                    )}
                    {t.per_symbol && (
                      <div className="opt-wf-windows" data-testid={`opt-per-symbol-${i}`}>
                        {Object.entries(t.per_symbol).map(([sym, v]) => (
                          <span key={sym} className={`opt-wf-win ${(v.pnl || 0) > 0 ? 'pos' : 'neg'}`}
                            title={`${sym}: PnL ${fmt(v.pnl)} · ${v.trades ?? 0} Trades · ${fmt(v.win_rate, 0)}% WR`}>
                            {sym.replace('USDT', '')}: {fmt(v.pnl, 1)}
                          </span>
                        ))}
                        <span className="opt-small" style={{ alignSelf: 'center' }}>
                          PnL je Coin · {fmt(t.positive_symbols_pct, 0)}% der Coins positiv
                        </span>
                      </div>
                    )}
                    <div className="opt-params-list">
                      {Object.entries(t.params || {}).map(([k, v]) => (
                        <span key={k} className="opt-param-pill">{k}: <b>{String(v)}</b></span>
                      ))}
                      {tradeParamPills(t.trade_params)}
                      {(t.rules?.long || []).map((r, ri) => <span key={`l${ri}`} className="opt-param-pill">L: {r}</span>)}
                      {(t.rules?.short || []).map((r, ri) => <span key={`s${ri}`} className="opt-param-pill">S: {r}</span>)}
                    </div>
                  </button>
                ))}
                <div className="opt-override-legend">
                  Die ausgewählte Strategie (#{(selEntry?.rank) || selTop + 1}) wird beim Übernehmen/Speichern verwendet.
                </div>
              </div>
            )}
            {(result.strategy_warnings || []).length > 0 && (
              <div className="bt-rule-warnings" data-testid="opt-strategy-warnings">
                <b>Hinweis:</b> Diese Regeln konnten nicht ausgewertet werden (deshalb evtl. 0 Trades):
                <ul>
                  {result.strategy_warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}
            {result.mode === 'dynamic' ? (
              <DynamicResult result={result} />
            ) : result.mode === 'params' ? (
              <>
                <div className="opt-section-title">
                  <Trophy size={15} weight="fill" style={{ color: '#FFD700' }} />
                  ERGEBNIS · {result.strategy_name} · {result.timeframe} · {result.days} Tage
                </div>
                <div className="opt-compare">
                  <div className="opt-card">
                    <div className="opt-card-title">AKTUELLE EINSTELLUNGEN</div>
                    <div className="opt-metrics">{metricsRow(result.baseline?.metrics)}</div>
                  </div>
                  <div className="opt-card best">
                    <div className="opt-card-title">OPTIMIERT {result.best?.is_baseline && '(keine Verbesserung gefunden)'}</div>
                    <div className="opt-metrics">{metricsRow(result.best?.metrics)}</div>
                  </div>
                </div>
                {!result.best?.is_baseline && (
                  <>
                    <div className="opt-params-list" data-testid="opt-best-params">
                      {Object.entries(result.best?.params || {}).map(([k, v]) => (
                        <span key={k} className="opt-param-pill">{k}: <b>{String(v)}</b></span>
                      ))}
                      {tradeParamPills(result.best?.trade_params)}
                    </div>
                    <button className="opt-apply" onClick={() => setShowApplyChoice(v => !v)} disabled={applied} data-testid="opt-apply-params">
                      <CheckCircle size={15} weight="bold" />
                      {applied ? 'Übernommen ✓' : 'Beste Parameter übernehmen (Live/Paper Einstellungen)'}
                    </button>
                    {showApplyChoice && !applied && (
                      <div className="opt-apply-choice" data-testid="opt-apply-choice">
                        <div className="opt-apply-choice-title">Wofür sollen die Einstellungen gelten?</div>
                        <button className="opt-apply-choice-btn coins" onClick={() => applyParams('coins')} disabled={applying} data-testid="opt-apply-scope-coins">
                          <b>Nur für optimierte Coins</b>
                          <span>{(result.symbols || []).map(s => s.replace('USDT', '')).join(', ')} – Coin-spezifische Overrides</span>
                        </button>
                        <button className="opt-apply-choice-btn global" onClick={() => applyParams('global')} disabled={applying} data-testid="opt-apply-scope-global">
                          <b>Für alle Coins</b>
                          <span>Einstellungen gelten global für die Strategie</span>
                        </button>
                        <button className="opt-apply-choice-cancel" onClick={() => setShowApplyChoice(false)} data-testid="opt-apply-scope-cancel">Abbrechen</button>
                      </div>
                    )}
                    <button className="opt-apply opt-apply-bt" onClick={applyToBacktester} data-testid="opt-apply-backtest">
                      <FloppyDisk size={15} weight="bold" />
                      In Backtester übernehmen (dort direkt testen)
                    </button>
                  </>
                )}
                {result.search_stats && (result.search_stats.improvements || []).length > 0 && (
                  <div className="opt-steps" data-testid="opt-search-stats">
                    <div className="opt-label">
                      SUCH-VERLAUF · {result.search_stats.algorithm} · {result.search_stats.improved_n} Verbesserungen
                      in {result.search_stats.iterations} Iterationen
                      {result.search_stats.algorithm === 'Bayes' && ' (lernt aus bisherigen Ergebnissen und sucht gezielt in vielversprechenden Bereichen weiter)'}
                    </div>
                    {result.search_stats.improvements.map((s, i) => (
                      <div key={i} className="opt-step">
                        <span className="opt-step-round">Iteration {s.iteration}</span>
                        <span>Neuer Bestwert: Score {fmt(s.score, 1)} · {s.trades} Trades · {fmt(s.win_rate, 1)}% WR · {fmt(s.pnl)} PnL</span>
                      </div>
                    ))}
                  </div>
                )}
                {(result.top || []).length > 0 && (
                  <div className="opt-table-wrap">
                    <table className="opt-table">
                      <thead><tr><th>#</th><th>Score</th><th>Trades</th><th>WR</th><th>PnL</th><th>PnL %</th><th>Parameter</th></tr></thead>
                      <tbody>
                        {result.top.slice(0, 10).map((t, i) => (
                          <tr key={i}>
                            <td>{i + 1}</td>
                            <td className="mono">{fmt(t.score, 1)}</td>
                            <td>{t.metrics.trades}</td>
                            <td className={t.metrics.win_rate >= 50 ? 'pos' : 'neg'}>{fmt(t.metrics.win_rate, 1)}%</td>
                            <td className={`mono ${t.metrics.pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(t.metrics.pnl)}</td>
                            <td className={`mono ${(t.metrics.pnl_pct || 0) >= 0 ? 'pos' : 'neg'}`}>{t.metrics.pnl_pct !== undefined ? `${fmt(t.metrics.pnl_pct, 1)}%` : '–'}</td>
                            <td className="opt-small">{Object.entries({ ...t.params, ...t.trade_params }).map(([k, v]) => `${k}=${v}`).join(' · ')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="opt-section-title">
                  <Trophy size={15} weight="fill" style={{ color: '#FFD700' }} />
                  ENTDECKTE STRATEGIE · {result.timeframe} · {result.days} Tage
                </div>
                {result.metrics ? (
                  <div className="opt-card best">
                    <div className="opt-metrics">{metricsRow(result.metrics)}</div>
                  </div>
                ) : (
                  <div className="opt-empty">Keine Regel-Kombination hat die Mindest-Trades erreicht – Zeitraum erhöhen oder Min. Trades senken.</div>
                )}
                <div className="opt-rules">
                  <div className="opt-rules-col">
                    <div className="opt-rules-title pos">LONG-REGELN</div>
                    {(result.rules?.long || []).map((r, i) => <div key={i} className="opt-rule">{r}</div>)}
                  </div>
                  <div className="opt-rules-col">
                    <div className="opt-rules-title neg">SHORT-REGELN</div>
                    {(result.rules?.short || []).map((r, i) => <div key={i} className="opt-rule">{r}</div>)}
                  </div>
                </div>
                {result.trade_params && Object.keys(result.trade_params).length > 0 && (
                  <div className="opt-params-list" data-testid="opt-discovery-trade-params">
                    <span style={{ fontSize: 11, color: '#8A8FA3', alignSelf: 'center' }}>BESTE TRADE-EINSTELLUNGEN:</span>
                    {tradeParamPills(result.trade_params)}
                  </div>
                )}
                {(result.steps || []).length > 0 && (
                  <div className="opt-steps">
                    <div className="opt-label">SUCH-VERLAUF</div>
                    {result.steps.map((s, i) => (
                      <div key={i} className="opt-step">
                        <span className="opt-step-round">Runde {s.round}</span>
                        {s.added
                          ? <span>+ {s.added} → {s.metrics?.trades} Trades · {fmt(s.metrics?.win_rate, 1)}% WR · {fmt(s.metrics?.pnl)} PnL</span>
                          : <span className="opt-small">{s.info}</span>}
                      </div>
                    ))}
                    {(result.refine_log || []).map((s, i) => (
                      <div key={`r${i}`} className="opt-step">
                        <span className="opt-step-round tune">Tuning</span>
                        <span>{s.change} → {fmt(s.metrics?.win_rate, 1)}% WR · {fmt(s.metrics?.pnl)} PnL</span>
                      </div>
                    ))}
                  </div>
                )}
                {result.metrics && (
                  <div className="opt-save-row">
                    <input type="text" placeholder="Name der neuen Strategie"
                      value={saveName} onChange={e => setSaveName(e.target.value)}
                      data-testid="opt-save-name" />
                    {result.base_strategy_id && (
                      <label className="opt-check" style={{ whiteSpace: 'nowrap' }}>
                        <input type="checkbox" checked={updateBase}
                          onChange={e => setUpdateBase(e.target.checked)} data-testid="opt-update-base" />
                        Basis-Strategie aktualisieren
                      </label>
                    )}
                    <button className="opt-apply" onClick={saveStrategy} disabled={applied} data-testid="opt-save-strategy">
                      <FloppyDisk size={15} weight="bold" />
                      {applied ? 'Gespeichert ✓' : (updateBase && result.base_strategy_id ? 'Basis-Strategie aktualisieren' : 'Als Strategie speichern & aktivieren')}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {!result && !running && (
          <div className="opt-empty" data-testid="opt-empty">
            Wähle einen Modus und starte die Optimierung – der Algorithmus testet automatisch
            hunderte Kombinationen auf echten historischen Daten.
          </div>
        )}
      </div>
    </SafeOverlay>
  );
}
