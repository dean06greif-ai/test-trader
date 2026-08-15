import React, { useState, useEffect, useRef, useCallback } from 'react';
import { X, Play, Trash, ChartScatter, ArrowClockwise, Cloud, Desktop, Gear } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import SafeOverlay from './SafeOverlay';
import LocalWorkerPanel from './LocalWorkerPanel';
import TIMEFRAMES from '../constants/timeframes';
import EquityChart from './EquityChart';
import RegimeChart from './RegimeChart';
import RegimeOptimizePanel from './RegimeOptimizePanel';
import RegimeEngineSettings from './RegimeEngineSettings';
import RegimeValidation from './RegimeValidation';
import DynamicPanel from './DynamicPanel';
import { regimeColor } from '../lib/regimeColors';
import { fmtDate, fmtDateTime } from '../lib/time';
import './RegimeLab.css';

const NNFX_LABELS = { trend: 'NNFX: Trend', range: 'NNFX: Seitwärts', breakout: 'NNFX: Breakout' };

const API_URL = process.env.REACT_APP_BACKEND_URL;
const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

const DAY_OPTIONS = [30, 60, 90, 180, 360, 540, 720, 1080, 1440, 1800, 2160, 2880, 3600];
const STATE_KEY = 'regime_lab_ui_v1';
const loadState = () => { try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; } catch { return {}; } };

const scopeKey = (scope, symbol) => (scope === 'per_coin' ? `per_coin:${symbol}` : 'combined');

// ---------------- EMA-Perioden-Vergleich (Detektor 'ema') ----------------
function EmaPeriodCompare({ selCoins, timeframe, days, trainPct, engineConfig, jobBlocked }) {
  const [periods, setPeriods] = useState('5, 9, 14');
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const timer = useRef(null);
  useEffect(() => () => clearInterval(timer.current), []);

  const start = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const ps = periods.split(',').map(x => parseFloat(x.trim())).filter(x => x >= 2 && x <= 100);
    if (!ps.length) { toast.error('Perioden 2-100 Tage angeben, z.B. 5, 9, 14'); return; }
    if (!selCoins.length) { toast.error('Mindestens 1 Coin wählen'); return; }
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/ema-compare`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ symbols: selCoins, timeframe, days, train_pct: trainPct,
          periods: ps, engine_config: engineConfig || {} }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      setResult(null);
      setJob({ id: d.job_id, phase: 'Startet', progress: 0 });
      timer.current = setInterval(async () => {
        try {
          const j = await fetch(`${API_URL}/api/regime-lab/status/${d.job_id}`).then(x => x.json());
          setJob({ id: d.job_id, phase: j.phase, progress: j.progress, status: j.status });
          if (j.status !== 'running') {
            clearInterval(timer.current);
            setJob(null);
            if (j.status === 'done' && j.result?.rows) setResult(j.result);
            else if (j.status === 'error') toast.error(j.error || 'Vergleich fehlgeschlagen');
          }
        } catch { /* nächster Poll */ }
      }, 1500);
    } catch { toast.error('Verbindungsfehler'); }
  };

  const cancel = async () => {
    if (job?.id) await fetch(`${API_URL}/api/regime-lab/cancel/${job.id}`,
      { method: 'POST', headers: authHeaders() }).catch(() => {});
  };

  return (
    <div className="opt-row" data-testid="ema-compare-section">
      <div className="opt-label">EMA-PERIODEN-VERGLEICH (DETEKTOR 'EMA')</div>
      <div className="opt-small" style={{ marginBottom: 6 }}>
        Gleiche Coins/Zeitraum wie oben, mehrere EMA-Perioden im direkten Vergleich:
        Live=Final-Treffer (gesamt / Holdout / Trend), Phasendauern und Prüfung –
        so findest du die beste Periode fürs Daytrading, ohne zu raten.
      </div>
      <div className="opt-setup" style={{ alignItems: 'center' }}>
        <label className="opt-field" title="Kommagetrennte EMA-Perioden in Tagen (2-100), max. 8">
          Perioden (Tage)
          <input value={periods} onChange={e => setPeriods(e.target.value)}
            placeholder="5, 9, 14" style={{ width: 110 }} data-testid="ema-compare-periods" />
        </label>
        <button className="opt-chip" onClick={start} disabled={!!job || jobBlocked}
          data-testid="ema-compare-run">
          <Play size={11} weight="fill" /> Vergleich starten
        </button>
        {job && (
          <span className="opt-small" data-testid="ema-compare-status">
            {job.phase} · {job.progress ?? 0}%
            <button className="opt-chip" onClick={cancel} style={{ marginLeft: 6 }}
              data-testid="ema-compare-cancel">Abbrechen</button>
          </span>
        )}
      </div>
      {result && (
        <div style={{ overflowX: 'auto', marginTop: 6 }}>
          <table className="rl-compare-table" data-testid="ema-compare-table"
            style={{ borderCollapse: 'collapse', fontSize: 12, minWidth: 620 }}>
            <thead>
              <tr style={{ textAlign: 'left', opacity: 0.7 }}>
                <th style={{ padding: '3px 10px 3px 0' }}>EMA</th>
                <th style={{ padding: '3px 10px' }} title="Anteil der Kerzen, bei denen die Live-Sicht dieselbe Richtung sieht wie die finale Sicht">Live=Final</th>
                <th style={{ padding: '3px 10px' }} title="Nur im Holdout (Walk-Forward-Zeitraum) – die ehrlichste Kennzahl">Holdout</th>
                <th style={{ padding: '3px 10px' }} title="Nur auf Trend-Kerzen (Auf/Ab)">Trend-Treffer</th>
                <th style={{ padding: '3px 10px' }} title="Durchschnittliche Phasendauer der finalen Sicht">Ø Phase final</th>
                <th style={{ padding: '3px 10px' }} title="Durchschnittliche Phasendauer der Live-Sicht (kürzer = mehr Flackern)">Ø Phase live</th>
                <th style={{ padding: '3px 10px' }}>Wechsel (final/live)</th>
                <th style={{ padding: '3px 10px' }} title="Anteil der Kerzen, die gegen ihr Regime-Label laufen">Verstöße</th>
                <th style={{ padding: '3px 10px' }}>Prüfung</th>
              </tr>
            </thead>
            <tbody>
              {result.rows.map(r => (
                <tr key={r.period} data-testid={`ema-compare-row-${r.period}`}
                  style={r.period === result.best_period
                    ? { background: 'rgba(80,200,120,0.12)' } : undefined}>
                  <td style={{ padding: '3px 10px 3px 0' }}>
                    <b>{r.period}d</b>{r.period === result.best_period ? ' ★' : ''}
                  </td>
                  {r.error ? <td colSpan={8} className="neg">{r.error}</td> : (
                    <>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.direction_pct, 1)}%</td>
                      <td style={{ padding: '3px 10px' }}><b>{fmt(r.holdout_direction_pct, 1)}%</b></td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.trend_hit_pct, 1)}%</td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.avg_final_segment_days, 1)}d</td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.avg_live_segment_days, 1)}d</td>
                      <td style={{ padding: '3px 10px' }}>{r.switches_final} / {r.switches_live}</td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.violation_pct, 1)}%</td>
                      <td style={{ padding: '3px 10px' }} className={r.passed ? 'pos' : 'neg'}>
                        {r.passed ? '✓' : '✗'}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="opt-small" style={{ marginTop: 4 }}>
            ★ = beste Holdout-Trefferquote. Tipp: die Sieger-Periode oben unter
            Engine-Feineinstellungen → „EMA-Regime: Periode" eintragen und die
            Analyse mit Detektor 'ema' starten.
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- Kombi-Detektor: Auto-Kalibrierung ----------------
function KombiAutoCalibrate({ selCoins, timeframe, days, trainPct, engineConfig,
  setEngineConfig, jobBlocked }) {
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [applied, setApplied] = useState(false);
  const timer = useRef(null);
  useEffect(() => () => clearInterval(timer.current), []);

  const start = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (!selCoins.length) { toast.error('Mindestens 1 Coin wählen'); return; }
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/kombi-calibrate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ symbols: selCoins, timeframe, days, train_pct: trainPct,
          engine_config: engineConfig || {} }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      setResult(null); setApplied(false);
      setJob({ id: d.job_id, phase: 'Startet', progress: 0 });
      timer.current = setInterval(async () => {
        try {
          const j = await fetch(`${API_URL}/api/regime-lab/status/${d.job_id}`).then(x => x.json());
          setJob({ id: d.job_id, phase: j.phase, progress: j.progress, status: j.status });
          if (j.status !== 'running') {
            clearInterval(timer.current);
            setJob(null);
            if (j.status === 'done' && j.result?.rows) setResult(j.result);
            else if (j.status === 'error') toast.error(j.error || 'Kalibrierung fehlgeschlagen');
          }
        } catch { /* nächster Poll */ }
      }, 1500);
    } catch { toast.error('Verbindungsfehler'); }
  };

  const cancel = async () => {
    if (job?.id) await fetch(`${API_URL}/api/regime-lab/cancel/${job.id}`,
      { method: 'POST', headers: authHeaders() }).catch(() => {});
  };

  const apply = () => {
    if (!result?.best_config) return;
    setEngineConfig({ ...(engineConfig || {}), ...result.best_config });
    setApplied(true);
    toast.success(`Kombi kalibriert: Schwelle ${result.best.thr} · Fenster ${result.best.slope_days}d übernommen`);
  };

  const isBest = (r) => result?.best && r.thr === result.best.thr
    && r.slope_days === result.best.slope_days;

  return (
    <div className="opt-row" data-testid="kombi-calibrate-section">
      <div className="opt-label">AUTO-KALIBRIERUNG (DETEKTOR 'KOMBI')</div>
      <div className="opt-small" style={{ marginBottom: 6 }}>
        Sucht automatisch die ideale Trend-Schwelle und das Steigungs-Fenster für den
        Kombi-Detektor: Ø Final-Phasendauer im 5–15-Tage-Zielband bei maximaler
        Holdout-Trefferquote (Live=Final, kein Lookahead). EMA-Periode und alle
        anderen Einstellungen bleiben wie oben konfiguriert.
      </div>
      <div className="opt-setup" style={{ alignItems: 'center' }}>
        <button className="opt-chip" onClick={start} disabled={!!job || jobBlocked}
          data-testid="kombi-calibrate-run">
          <Play size={11} weight="fill" /> Auto-Kalibrierung starten
        </button>
        {job && (
          <span className="opt-small" data-testid="kombi-calibrate-status">
            {job.phase} · {job.progress ?? 0}%
            <button className="opt-chip" onClick={cancel} style={{ marginLeft: 6 }}
              data-testid="kombi-calibrate-cancel">Abbrechen</button>
          </span>
        )}
      </div>
      {result && (
        <div style={{ overflowX: 'auto', marginTop: 6 }}>
          <div className="opt-setup" style={{ alignItems: 'center', marginBottom: 4 }}>
            {result.best ? (
              <>
                <span className="opt-small" data-testid="kombi-calibrate-best">
                  Bestes Ergebnis: Schwelle <b>{result.best.thr}</b> ·
                  Fenster <b>{result.best.slope_days}d</b> ·
                  Ø Phase <b>{fmt(result.best.avg_final_segment_days, 1)}d</b>
                  {result.best.in_target ? ' ✓ im Zielband' : ' ⚠ außerhalb 5–15d'} ·
                  Holdout <b>{fmt(result.best.holdout_direction_pct, 1)}%</b>
                </span>
                <button className="opt-chip" onClick={apply} disabled={applied}
                  data-testid="kombi-calibrate-apply">
                  {applied ? '✓ Übernommen' : 'Beste Werte übernehmen'}
                </button>
              </>
            ) : <span className="opt-small">Kein bewertbares Ergebnis</span>}
          </div>
          <table className="rl-compare-table" data-testid="kombi-calibrate-table"
            style={{ borderCollapse: 'collapse', fontSize: 12, minWidth: 640 }}>
            <thead>
              <tr style={{ textAlign: 'left', opacity: 0.7 }}>
                <th style={{ padding: '3px 10px 3px 0' }}>Schwelle</th>
                <th style={{ padding: '3px 10px' }}>Fenster</th>
                <th style={{ padding: '3px 10px' }} title="Durchschnittliche Phasendauer der finalen Sicht – Ziel: 5-15 Tage">Ø Phase final</th>
                <th style={{ padding: '3px 10px' }} title="Liegt die Phasendauer im 5-15-Tage-Zielband?">Zielband</th>
                <th style={{ padding: '3px 10px' }} title="Nur im Holdout (Walk-Forward-Zeitraum) – die ehrlichste Kennzahl">Holdout</th>
                <th style={{ padding: '3px 10px' }} title="Anteil der Kerzen, bei denen die Live-Sicht dieselbe Richtung sieht wie die finale Sicht">Live=Final</th>
                <th style={{ padding: '3px 10px' }} title="Nur auf Trend-Kerzen (Auf/Ab)">Trend-Treffer</th>
                <th style={{ padding: '3px 10px' }}>Wechsel (final/live)</th>
                <th style={{ padding: '3px 10px' }} title="Holdout-Trefferquote minus 4 Punkte je Tag außerhalb des Zielbands">Score</th>
              </tr>
            </thead>
            <tbody>
              {result.rows.slice(0, 12).map((r, idx) => (
                <tr key={`${r.thr}-${r.slope_days}`}
                  data-testid={`kombi-calibrate-row-${idx}`}
                  style={isBest(r) ? { background: 'rgba(80,200,120,0.12)' } : undefined}>
                  <td style={{ padding: '3px 10px 3px 0' }}>
                    <b>{r.thr}</b>{isBest(r) ? ' ★' : ''}
                  </td>
                  {r.error ? <td colSpan={8} className="neg">{r.error}</td> : (
                    <>
                      <td style={{ padding: '3px 10px' }}>{r.slope_days}d</td>
                      <td style={{ padding: '3px 10px' }}><b>{fmt(r.avg_final_segment_days, 1)}d</b></td>
                      <td style={{ padding: '3px 10px' }} className={r.in_target ? 'pos' : 'neg'}>
                        {r.in_target ? '✓' : '✗'}
                      </td>
                      <td style={{ padding: '3px 10px' }}><b>{fmt(r.holdout_direction_pct, 1)}%</b></td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.direction_pct, 1)}%</td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.trend_hit_pct, 1)}%</td>
                      <td style={{ padding: '3px 10px' }}>{r.switches_final} / {r.switches_live}</td>
                      <td style={{ padding: '3px 10px' }}>{fmt(r.score, 2)}</td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="opt-small" style={{ marginTop: 4 }}>
            ★ = bester Score ({result.combos} Kombinationen geprüft, Top 12 angezeigt).
            Mit „Beste Werte übernehmen" landen Schwelle + Fenster direkt in den
            Engine-Einstellungen (Detektor wird auf 'kombi' gestellt) – danach die
            Analyse oben neu starten.
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- Regime-Übergangs-Matrix (Etappe 2) ----------------
function TransitionMatrix({ analysisId, scope, symbol, regimes, model }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState('final');
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!open) return;
    setData(null); setErr(null);
    const q = new URLSearchParams({ scope, view });
    if (symbol) q.set('symbol', symbol);
    fetch(`${API_URL}/api/regime-lab/${analysisId}/transitions?${q}`)
      .then(r => r.json().then(d => { if (!r.ok) throw new Error(d.detail || 'Fehler'); return d; }))
      .then(setData)
      .catch(e => setErr(String(e.message || e)));
  }, [open, view, scope, symbol, analysisId]);

  const DIR_COLORS = { 0: '#ff4757', 1: '#ffa502', 2: '#00e5a0' };
  const cellOf = (rows, f, t) => rows.find(m => m.from === f && m.to === t);

  const matrixTable = (rows, ids, labelOf, colorOf, testid) => (
    <table className="rl-compare-table" data-testid={testid}
      style={{ borderCollapse: 'collapse', fontSize: 12, marginTop: 4 }}>
      <thead>
        <tr style={{ textAlign: 'left', opacity: 0.7 }}>
          <th style={{ padding: '3px 10px 3px 0' }}>Von ↓ / Nach →</th>
          {ids.map(t => (
            <th key={t} style={{ padding: '3px 10px' }}>
              <span className="rl-dot" style={{ background: colorOf(t), marginRight: 4 }} />
              {labelOf(t)}
            </th>
          ))}
          <th style={{ padding: '3px 10px' }} title="Anzahl Übergänge aus diesem Regime · Ø Dauer der Phase vor dem Wechsel">Kontext</th>
        </tr>
      </thead>
      <tbody>
        {ids.map(f => {
          const fromRows = rows.filter(m => m.from === f);
          if (!fromRows.length) return null;
          const tot = fromRows.reduce((a, m) => a + m.count, 0);
          return (
            <tr key={f} style={data.last
              && ((rows === data.direction_matrix && data.last.direction === f)
                || (rows === data.matrix && data.last.regime === f))
              ? { background: 'rgba(179,136,255,0.10)' } : undefined}>
              <td style={{ padding: '3px 10px 3px 0', whiteSpace: 'nowrap' }}>
                <span className="rl-dot" style={{ background: colorOf(f), marginRight: 4 }} />
                <b>{labelOf(f)}</b>
              </td>
              {ids.map(t => {
                const c = cellOf(fromRows, f, t);
                return (
                  <td key={t} style={{ padding: '3px 10px', whiteSpace: 'nowrap' }}
                    title={c ? `${c.count}× · Ø Dauer davor ${fmt(c.avg_from_days, 1)}d · Ø Dauer danach ${fmt(c.avg_to_days, 1)}d` : 'kein Übergang beobachtet'}>
                    {c ? <><b>{fmt(c.prob_pct, 0)}%</b> <span style={{ opacity: 0.6 }}>({c.count})</span></> : '–'}
                  </td>
                );
              })}
              <td style={{ padding: '3px 10px', whiteSpace: 'nowrap', opacity: 0.75 }}>
                {tot}× · Ø {(() => {
                  const pf = (rows === data.matrix ? data.per_from : data.direction_per_from) || [];
                  const e = pf.find(p => p.from === f);
                  return e ? `${fmt(e.avg_days, 1)}d` : '–';
                })()}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );

  return (
    <div data-testid="regime-transition-matrix">
      <div className="opt-setup" style={{ alignItems: 'center', marginTop: 6 }}>
        <button className="opt-chip" onClick={() => setOpen(!open)}
          data-testid="transition-matrix-toggle"
          title="Historische Übergangs-Wahrscheinlichkeiten zwischen den Regimen dieser Analyse: was folgt z.B. auf Seitwärts – und wie lange dauerten die Phasen davor/danach?">
          {open ? '▾' : '▸'} Übergangs-Matrix
        </button>
        {open && (
          <>
            <button className={`opt-chip ${view === 'final' ? 'on' : ''}`}
              onClick={() => setView('final')} data-testid="transition-matrix-view-final"
              title="Rückwirkend korrigierte Final-Phasen (die 'wahren' Abschnitte)">Final</button>
            <button className={`opt-chip ${view === 'live' ? 'on' : ''}`}
              onClick={() => setView('live')} data-testid="transition-matrix-view-live"
              title="Kausale Live-Abschnitte (ohne Lookahead) – so hätte man es live erlebt">Live</button>
          </>
        )}
      </div>
      {open && err && <span className="opt-small" style={{ color: '#e66' }}>{err}</span>}
      {open && !data && !err && <span className="opt-small">Lade Übergänge…</span>}
      {open && data && (
        <div style={{ overflowX: 'auto' }}>
          {data.last && (
            <div className="opt-small" style={{ marginTop: 4 }} data-testid="transition-matrix-last">
              Aktuelle Phase: <b>{data.last.label}</b> seit <b>{fmt(data.last.days, 1)}d</b>
              {' '}– die markierte Zeile zeigt, was darauf historisch folgte.
            </div>
          )}
          <div className="opt-small" style={{ marginTop: 6, fontWeight: 700, letterSpacing: 0.4 }}>
            RICHTUNGS-EBENE (AUF / SEITWÄRTS / AB)
          </div>
          {matrixTable(data.direction_matrix || [],
            [...new Set((data.direction_matrix || []).flatMap(m => [m.from, m.to]))].sort(),
            (d) => (data.direction_labels || {})[d] || d,
            (d) => DIR_COLORS[d] || '#888',
            'transition-matrix-direction')}
          <div className="opt-small" style={{ marginTop: 8, fontWeight: 700, letterSpacing: 0.4 }}>
            JE REGIME ({data.total_transitions} ÜBERGÄNGE)
          </div>
          {matrixTable(data.matrix || [],
            (data.regimes || []).map(r => r.id),
            (id) => (data.regimes || []).find(r => r.id === id)?.label || `#${id}`,
            (id) => regimeColor(id, regimes, model),
            'transition-matrix-regimes')}
          <div className="opt-small" style={{ marginTop: 4, opacity: 0.7 }}>
            Lesart: Zeile = aktuelles Regime, Spalte = nächstes Regime, Zelle = Anteil
            der historischen Übergänge (Anzahl). Kontext = Übergänge gesamt · Ø Phasendauer
            vor dem Wechsel. {view === 'live' ? 'Live-Abschnitte (kausal, ohne Lookahead).' : 'Final-Phasen (rückwirkend korrigiert).'}
          </div>
        </div>
      )}
    </div>
  );
}

const M = ({ m }) => m ? (
  <span className="opt-small">
    PnL <b className={m.pnl >= 0 ? 'pos' : 'neg'}>{fmt(m.pnl)}</b> · WR <b>{fmt(m.win_rate, 1)}%</b> ·
    Trades <b>{m.trades}</b> · DD <b>{fmt(m.max_drawdown)}</b>
  </span>
) : null;

// ---------------- Regime-Karte (Label, Kennzahlen, behalten, Strategie-Suche) ----------------
function RegimeCard({ analysis, scope, symbol, regime, usage, strategies, jobBlocked, execution, model, onChanged }) {
  const [showOpt, setShowOpt] = useState(false);
  const key = `${scopeKey(scope, symbol)}:${regime.id}`;
  const kept = (analysis.kept || {})[key] !== false;
  const assignment = (analysis.assignments || {})[key];
  const st = regime.stats || {};

  const toggleKeep = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const r = await fetch(`${API_URL}/api/regime-lab/${analysis.id}/keep`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ scope, symbol, regime_id: regime.id, keep: !kept }),
    });
    if (r.ok) onChanged(); else toast.error('Speichern fehlgeschlagen');
  };

  const removeAssignment = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const r = await fetch(`${API_URL}/api/regime-lab/${analysis.id}/assign`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ scope, symbol, regime_id: regime.id, remove: true }),
    });
    if (r.ok) { toast.success('Zuordnung entfernt'); onChanged(); } else toast.error('Fehlgeschlagen');
  };

  return (
    <div className={`rl-regime-card ${kept ? '' : 'discarded'} ${assignment ? 'assigned' : ''}`}
      style={{ borderLeft: `3px solid ${regimeColor(regime.id, [regime], model)}` }}
      data-testid={`regime-card-${scope}-${regime.id}`}>
      <div className="rl-regime-head">
        <span className="rl-dot" style={{ background: regimeColor(regime.id, [regime], model) }} />
        <label className="opt-check" style={{ paddingBottom: 0 }} title="Verworfene Regime werden bei Strategie-Suche und Zusammenbau übersprungen">
          <input type="checkbox" checked={kept} onChange={toggleKeep}
            data-testid={`regime-keep-${scope}-${regime.id}`} /> behalten
        </label>
        <b style={{ fontSize: 12.5 }}>#{regime.id + 1} {regime.label}</b>
        {regime.nnfx && (
          <span className={`rl-nnfx-tag ${regime.nnfx}`} title="Zuordnung im NNFX-Framework (3 Regime)"
            data-testid={`regime-nnfx-${scope}-${regime.id}`}>
            {NNFX_LABELS[regime.nnfx] || regime.nnfx}
          </span>
        )}
        <span className="opt-small">Anteil <b>{fmt(regime.share_pct, 0)}%</b></span>
        {usage && <span className="opt-small">· <b>{usage.days}</b> Tage in <b>{usage.segments}</b> Abschnitten</span>}
        <span style={{ flex: 1 }} />
        {kept && (
          <button className="opt-chip" onClick={() => setShowOpt(!showOpt)}
            data-testid={`regime-optimize-toggle-${scope}-${regime.id}`}>
            {showOpt ? 'Suche schließen' : (assignment ? 'Neue Strategie suchen' : 'Strategie suchen')}
          </button>
        )}
      </div>
      <div className="rl-regime-stats">
        <span title="Durchschnittliche Bewegung pro Tag in dieser Phase">Trend/Tag <b className={st.trend_pct_per_day >= 0 ? 'pos' : 'neg'}>{fmt(st.trend_pct_per_day, 2)}%</b></span>
        <span title="Verhältnis |Trend| zu Volatilität – unter ~0.45 gilt die Phase als seitwärts">Trendstärke <b>{fmt(st.trend_strength, 2)}</b></span>
        <span title="0 = reines Hin und Her, 1 = gerade Linie">Effizienz <b>{fmt(st.efficiency, 2)}</b></span>
        <span>Volatilität <b>{fmt(st.vol_pct, 2)}%</b></span>
        {st.score !== undefined && (
          <span title="Trend-Score = gewichteter t-Wert der Regression über alle Horizonte (|t|>2 = belegter Trend)">
            Trend-Score <b className={st.score >= 0 ? 'pos' : 'neg'}>{fmt(st.score, 2)}</b>
          </span>
        )}
        {st.adx !== undefined && <span title="Durchschnittlicher ADX in dieser Phase">ADX <b>{fmt(st.adx, 1)}</b></span>}
        {st.agreement !== undefined && (
          <span title="Anteil der Zeit-Horizonte, die dieselbe Richtung zeigen">
            Konsens <b>{fmt(st.agreement * 100, 0)}%</b>
          </span>
        )}
        {st.vol_z !== undefined && <span title="Volatilität gegen die eigene Historie (z-Wert)">Vola z <b>{fmt(st.vol_z, 2)}</b></span>}
        <span>Rel. Volumen <b>{fmt((regime.features || {}).rel_volume, 2)}</b></span>
      </div>
      {assignment && (
        <div className="rl-assign" data-testid={`regime-assignment-${scope}-${regime.id}`}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <b>✓ Bestätigte Strategie</b>
            <span className="opt-small">{assignment.mode === 'params'
              ? (assignment.strategy_name || assignment.strategy_id)
              : `Eigene Regeln (${(assignment.rules || []).length})`}</span>
            <M m={assignment.metrics} />
            {assignment.validation && <span className="opt-small pos">WF-PnL {fmt(assignment.validation.pnl)}</span>}
            <span style={{ flex: 1 }} />
            <button className="opt-chip" onClick={removeAssignment} data-testid={`regime-assignment-remove-${scope}-${regime.id}`}>
              <Trash size={11} /> entfernen
            </button>
          </div>
          {(assignment.rules || []).length > 0 && (
            <div className="opt-params-list" style={{ marginTop: 4, marginBottom: 0 }}>
              {assignment.rules.map((r, i) => <span key={i} className="opt-param-pill">{r}</span>)}
            </div>
          )}
          {Object.keys(assignment.trade_params || {}).length > 0 && (
            <div className="opt-params-list" style={{ marginTop: 4, marginBottom: 0 }}>
              {Object.entries(assignment.trade_params).map(([k, v]) =>
                <span key={k} className="opt-param-pill trade">{k}: <b>{String(v)}</b></span>)}
            </div>
          )}
        </div>
      )}
      {showOpt && kept && (
        <RegimeOptimizePanel analysisId={analysis.id} scope={scope} symbol={symbol}
          regime={regime} strategies={strategies} analysisTf={analysis.timeframe}
          jobBlocked={jobBlocked} execution={execution}
          onAssigned={() => { setShowOpt(false); onChanged(); }} />
      )}
    </div>
  );
}

// ---------------- Zusammenbau + finaler Walk-Forward ----------------
function BuildAndTest({ analysis, scope, symbol, strategies, jobBlocked, execution, onChanged }) {
  const [name, setName] = useState('');
  const [baseStrategy, setBaseStrategy] = useState('');
  const [busy, setBusy] = useState(false);
  const [wfJob, setWfJob] = useState(null);
  const [wfResult, setWfResult] = useState(null);
  const pollRef = useRef(null);
  useEffect(() => () => clearInterval(pollRef.current), []);

  const key = scopeKey(scope, symbol);
  const model = scope === 'per_coin' ? analysis.per_coin?.[symbol]?.model : analysis.combined?.model;
  const regimes = model?.regimes || [];
  const keptRegimes = regimes.filter(r => (analysis.kept || {})[`${key}:${r.id}`] !== false);
  const assignments = Object.keys(analysis.assignments || {}).filter(k => k.startsWith(key + ':'));
  const savedWf = (analysis.walkforward || {})[key];
  const trainPct = analysis.settings?.train_pct ?? 100;
  const hasHoldout = trainPct < 100;
  const needsBase = keptRegimes.some(r => {
    const a = (analysis.assignments || {})[`${key}:${r.id}`];
    return a && !a.definition;
  }) || assignments.some(k => !(analysis.assignments[k] || {}).definition);

  const build = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setBusy(true);
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/${analysis.id}/build`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ scope, symbol, name: name || undefined, strategy_id: baseStrategy || undefined }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Erstellen fehlgeschlagen'); return; }
      toast.success(`Dynamische Strategie erstellt (${d.regimes.length} Regime) – unter "Dynamische Strategien" im Optimizer verfügbar`);
    } catch { toast.error('Verbindungsfehler'); }
    finally { setBusy(false); }
  };

  const buildNnfx = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setBusy(true);
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/${analysis.id}/build-nnfx`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ scope, symbol, name: name || undefined }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'NNFX-Aufbau fehlgeschlagen'); return; }
      toast.success(`NNFX-Strategie erstellt: ${Object.keys(d.regime_strategies).length} Regime → `
        + `${Object.keys(d.nnfx_strategies).length} NNFX-Strategien. Jedes Regime hat jetzt eine Zuordnung `
        + '– Feinjustierung je Regime weiterhin über "Strategie suchen".');
      onChanged();
    } catch { toast.error('Verbindungsfehler'); }
    finally { setBusy(false); }
  };

  const runWf = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/${analysis.id}/walkforward`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ scope, symbol, strategy_id: baseStrategy || undefined, execution }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      setWfResult(null);
      setWfJob({ id: d.job_id, status: 'running', progress: 0, phase: 'Startet' });
      pollRef.current = setInterval(async () => {
        try {
          const j = await fetch(`${API_URL}/api/regime-lab/status/${d.job_id}`).then(x => x.json());
          setWfJob(j);
          if (j.status !== 'running') {
            clearInterval(pollRef.current);
            if (j.status === 'done') { setWfResult(j.result); onChanged(); }
            else if (j.status === 'error') toast.error(j.error || 'Walk-Forward fehlgeschlagen');
          }
        } catch { /* transient */ }
      }, 1500);
    } catch { toast.error('Verbindungsfehler'); }
  };

  const wf = wfResult || savedWf;
  return (
    <div className="rl-wf-box" data-testid={`regime-build-${scope}`}>
      <div className="opt-section-title">DYNAMISCHE STRATEGIE ZUSAMMENSTELLEN</div>
      <div className="opt-small" style={{ marginBottom: 8 }}>
        {assignments.length} von {keptRegimes.length} behaltenen Regimen haben eine bestätigte Strategie.
        {!hasHoldout && ' Hinweis: Diese Analyse hat keinen Holdout (Training 100%) – für den finalen Walk-Forward-Test eine Analyse mit z.B. 75% Training erstellen.'}
      </div>
      <div className="opt-setup">
        <label className="opt-field">Name
          <input value={name} onChange={e => setName(e.target.value)} placeholder={`Regime-Lab: ${analysis.name}`}
            data-testid={`regime-build-name-${scope}`} style={{ width: 220 }} />
        </label>
        <label className="opt-field" title={needsBase
          ? 'Erforderlich: mindestens ein Regime nutzt eine bestehende Strategie ohne eigene Regeln'
          : 'Optional: Basis-Strategie für Regime ohne Zuordnung'}>
          Basis-Strategie {needsBase ? '(erforderlich)' : '(optional)'}
          <select value={baseStrategy} onChange={e => setBaseStrategy(e.target.value)}
            data-testid={`regime-build-base-${scope}`}>
            <option value="">– automatisch –</option>
            {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <button className="opt-apply" onClick={build} disabled={busy || !assignments.length}
          data-testid={`regime-build-btn-${scope}`}>
          Dynamische Strategie erstellen
        </button>
        <button className="opt-apply" onClick={runWf}
          disabled={!assignments.length || !hasHoldout || wfJob?.status === 'running' || jobBlocked}
          title="Testet die Kombination auf dem unangetasteten Holdout – Klassifikation rückblickend, kein Lookahead"
          data-testid={`regime-wf-btn-${scope}`}>
          <Play size={13} /> Finaler Walk-Forward (Holdout)
        </button>
        <button className="opt-apply" onClick={buildNnfx} disabled={busy || !keptRegimes.length}
          title="NNFX-Framework: die Regime werden auf Trend / Seitwärts / Volatilität gemappt und automatisch mit den drei NNFX-Strategien belegt (danach je Regime optimierbar)"
          data-testid={`regime-nnfx-btn-${scope}`}>
          NNFX-Framework anwenden
        </button>
      </div>
      {wfJob?.status === 'running' && (
        <div className="opt-progress">
          <div className="opt-progress-bar"><div style={{ width: `${wfJob.progress || 0}%`, height: '100%', background: '#00e5a0' }} /></div>
          <div className="opt-progress-text">{wfJob.phase} · {wfJob.progress || 0}%</div>
        </div>
      )}
      {wf && (
        <div data-testid={`regime-wf-result-${scope}`}>
          <div className={`rl-verdict ${wf.verdict?.dynamic_better ? 'good' : 'bad'}`}>
            {wf.verdict?.recommendation}
          </div>
          <div className="opt-compare">
            <div className="opt-card best">
              <div className="opt-card-title">DYNAMISCH (HOLDOUT · {wf.switches} Phasenwechsel)</div>
              <M m={wf.dynamic_test} />
            </div>
            <div className="opt-card">
              <div className="opt-card-title">BESTE EINZELSTRATEGIE STATISCH (BENCHMARK)</div>
              <M m={wf.best_single?.metrics} />
              {wf.best_single && <div className="opt-small" style={{ marginTop: 4 }}>{wf.best_single.label}</div>}
            </div>
          </div>
          {(wf.per_regime || []).length > 0 && (
            <div className="opt-small" style={{ margin: '6px 0' }}>
              Je Regime im Holdout: {wf.per_regime.map(p => (
                <span key={p.regime} className="opt-param-pill" style={{ marginRight: 4 }}>
                  {p.label || `#${p.regime + 1}`}: <b className={p.metrics.pnl >= 0 ? 'pos' : 'neg'}>{fmt(p.metrics.pnl)}</b> ({p.metrics.trades} T.)
                </span>
              ))}
            </div>
          )}
          {wfResult?.points?.length > 0 && (
            <EquityChart points={wfResult.points} title="Equity im Holdout (dynamisch)" />
          )}
        </div>
      )}
    </div>
  );
}

// ---------------- Detail einer Analyse ----------------
function AnalysisDetail({ analysis, strategies, jobBlocked, execution, onChanged }) {
  const scopes = [];
  if (analysis.combined) scopes.push({ id: 'combined', label: 'Alle Coins (kombiniert)' });
  (analysis.symbols || []).forEach(s => {
    if (analysis.per_coin?.[s] && !analysis.per_coin[s].error) {
      scopes.push({ id: `coin:${s}`, label: s.replace('USDT', '') });
    }
  });
  const [tab, setTab] = useState(scopes[0]?.id || 'combined');
  const isCombined = tab === 'combined';
  const symbol = isCombined ? null : tab.slice(5);
  const scope = isCombined ? 'combined' : 'per_coin';
  const model = isCombined ? analysis.combined?.model : analysis.per_coin?.[symbol]?.model;
  const usage = isCombined ? analysis.combined?.usage : analysis.per_coin?.[symbol]?.usage;
  const regimes = model?.regimes || [];
  const trainEnd = (sym) => analysis.bounds?.[sym]?.train_end_ts;
  const perSymbol = isCombined
    ? (analysis.combined?.per_symbol || {})
    : (symbol ? { [symbol]: analysis.per_coin?.[symbol] || {} } : {});
  const validationSummary = isCombined
    ? analysis.combined?.validation
    : (analysis.per_coin?.[symbol]?.validation);
  const engine = model?.engine || 'kmeans';
  const [showIdeal, setShowIdeal] = useState(false);
  const [showLive, setShowLive] = useState(false);

  const currentBadges = Object.entries(perSymbol)
    .map(([sym, v]) => [sym, v?.current])
    .filter(([, c]) => c && c.regime !== null && c.regime !== undefined);

  return (
    <div data-testid="regime-analysis-detail">
      <div className="opt-small" style={{ margin: '4px 0 8px' }}>
        {analysis.timeframe} · {analysis.days} Tage · Training {analysis.settings?.train_pct}%
        {analysis.settings?.train_pct < 100 && ' (Rest = Holdout für den finalen Walk-Forward)'} ·
        {engine === 'v2'
          ? ` Engine v2 · ${(model?.regime_mode || model?.config?.regime_mode || 9)}-Regime-Modus`
            + ((model?.config?.detector || 'reactive') === 'ema'
              ? ` · EMA-Steigungs-Regime (EMA ${fmt(model?.config?.ema_regime_days ?? 9, 0)} Tage`
                + `, Schwelle ${model?.config?.ema_regime_thr ?? 0.18}×Vola)`
                + ` · Mini-Phasen-Filter ${model?.config?.min_phase_days ? `${fmt(model.config.min_phase_days, 1)}d` : 'auto'}`
              : (model?.config?.detector || 'reactive') === 'kombi'
              ? ` · Kombi-Detektor (EMA ${fmt(model?.config?.kombi_ema_days ?? 14, 0)} Tage`
                + `, Schwelle ${model?.config?.kombi_thr ?? 0.18}×Vola`
                + `, Fenster ${fmt(model?.config?.kombi_slope_days ?? 5, 0)}d`
                + `, Trend-Dominanz ${fmt(model?.config?.kombi_dominance_days ?? 3, 0)}d`
                + `${model?.config?.kombi_pivot_accel === false ? ', ohne' : ', mit'} Umkehrpunkt-Beschleuniger)`
                + ` · Mini-Phasen-Filter ${model?.config?.min_phase_days ? `${fmt(model.config.min_phase_days, 1)}d` : 'auto'}`
              : (model?.config?.detector || 'reactive') !== 'regression'
              ? ` · Umkehrpunkt-Erkennung (reaktiv) · Umkehr-Schwelle ${model?.config?.rev_atr_mult ?? 3}×ATR`
                + ` · Persistenz ${model?.config?.persist_candles ?? 3} Kerzen`
                + ` · Mini-Phasen-Filter ${model?.config?.min_phase_days ? `${fmt(model.config.min_phase_days, 1)}d` : 'auto'}`
                + (model?.adapt?.profile ? ` · Glättung "${model.adapt.profile}"` : '')
              : ` · Horizonte ${(model?.config?.horizons_days || []).map(d => `${d}d`).join('/')}`
                + (model?.adapt?.profile ? ` · Glättung "${model.adapt.profile}"` : '')
                + ` · Mindesthaltedauer ${fmt(model?.config?.min_hold_days, 1)}d`
                + ` · Trend-Schwelle t=${model?.config?.trend_t} · ADX≥${model?.config?.adx_min}`)
          : ` Cluster-Modell · Lookback ${analysis.settings?.lookback_days}d · max. ${analysis.settings?.max_regimes} Regime · Cluster-Qualität ${fmt(model?.silhouette, 2)}`}
      </div>
      {model?.adapt?.report?.candidates?.length > 0 && (
        <div className="rl-sim" data-testid="regime-adapt-report">
          <span className="opt-small" style={{ alignSelf: 'center' }}
            title="Es wurden mehrere Glättungs-Profile berechnet und nach Plausibilität, Rückblick-Übereinstimmung und Abschnittslänge bewertet. Das beste wurde verwendet.">
            Glättungs-Profile geprüft:
          </span>
          {model.adapt.report.candidates.map(c => (
            <span key={c.profile} className="opt-param-pill"
              style={c.profile === model.adapt.profile
                ? { borderColor: 'rgba(0,229,160,0.5)' } : undefined}
              title={`Verstöße ${c.violation_bars_pct}% · Rückblick-Treffer ${c.direction_pct}% · Ø Abschnitt ${c.avg_segment_days}d (Ziel ${c.target_segment_days}d)${c.passes === false ? ' · Plausibilitätsprüfung NICHT bestanden' : ''}`}>
              {c.profile} <b>{fmt(c.quality, 1)}</b>{c.passes === false ? ' ✕' : ''}
            </span>
          ))}
        </div>
      )}
      {currentBadges.length > 0 && (
        <div className="rl-current-row" data-testid="regime-current-row">
          {currentBadges.map(([sym, c]) => (
            <div key={sym} className="rl-current" data-testid={`regime-current-${sym}`}
              title={c.reason || ''}>
              <span className="rl-dot" style={{ background: regimeColor(c.regime, regimes, model) }} />
              <b>{sym.replace('USDT', '')}</b>
              <span className="rl-current-label">{c.label}</span>
              {c.nnfx && <span className={`rl-nnfx-tag ${c.nnfx}`}>{NNFX_LABELS[c.nnfx] || c.nnfx}</span>}
              <span className="opt-small">Sicherheit <b>{fmt(c.confidence, 0)}%</b></span>
              {c.strength && <span className="opt-small">Stärke <b>{c.strength}</b></span>}
              {c.last_switch && (
                <span className="opt-small">seit {fmtDate(c.last_switch)}</span>
              )}
              {c.early_warning?.active && (
                <span className="rl-warn" title={c.early_warning.reason}
                  data-testid={`regime-warn-${sym}`}>
                  Wechsel → {c.early_warning.next_label} · {fmt(c.early_warning.probability_pct, 0)}%
                  {c.early_warning.eta_days !== null && c.early_warning.eta_days !== undefined
                    ? ` · ~${c.early_warning.eta_days} Tage` : ''}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
      <RegimeValidation summary={validationSummary} perSymbol={perSymbol} />
      {(() => {
        const reps = Object.entries(perSymbol)
          .map(([s, v]) => [s, v?.corrections]).filter(([, c]) => c);
        if (!reps.length) return null;
        return (
          <div className="rl-sim" data-testid="regime-corrections">
            <span className="opt-small" style={{ alignSelf: 'center' }}
              title="Reaktive Erkennung: Umkehrpunkte werden erst nach Bestätigung (2-3 Kerzen Persistenz) erkannt – die Phase wird dann rückwirkend bis zum Hoch-/Tiefpunkt korrigiert. Hier steht, wie viele Umkehrpunkte gefunden wurden und wie schnell die Erkennung im Schnitt war.">
              Umkehrpunkt-Erkennung (selbstkorrigierend):
            </span>
            {reps.map(([s, c]) => (
              <span key={s} className="opt-param-pill" data-testid={`regime-corrections-${s}`}>
                {s.replace('USDT', '')} <b>{c.pivots}</b> Umkehrpunkte
                {c.avg_delay_days !== null && c.avg_delay_days !== undefined
                  ? <> · erkannt nach Ø <b>{fmt(c.avg_delay_days, 1)}d</b></> : null}
              </span>
            ))}
          </div>
        );
      })()}
      {(() => {
        const ags = Object.entries(perSymbol)
          .map(([s, v]) => [s, v?.live_agreement]).filter(([, a]) => a?.direction_pct != null);
        if (!ags.length) return null;
        const col = (p) => (p >= 65 ? 'rgba(0,229,160,0.45)'
          : p >= 50 ? 'rgba(255,165,2,0.45)' : 'rgba(255,71,87,0.45)');
        return (
          <div className="rl-sim" data-testid="regime-live-agreement">
            <span className="opt-small" style={{ alignSelf: 'center' }}
              title="Wie oft trifft die LIVE-Erkennung (ohne Zukunftswissen, Kerze für Kerze) die Richtung der final korrigierten Phasen? 'Holdout' = nur der Walk-Forward-Testzeitraum nach der Trainings-Grenze – der ehrlichste Wert dafür, ob die Regime-Umschaltung für Paper-/Live-Trading taugt. 'Trend' = Trefferquote nur auf Trend-Kerzen (Auf/Ab).">
              Live-Trefferquote (Richtung):
            </span>
            {ags.map(([s, a]) => (
              <span key={s} className="opt-param-pill"
                style={{ borderColor: col(a.holdout_direction_pct ?? a.direction_pct) }}
                data-testid={`regime-live-agreement-${s}`}>
                {s.replace('USDT', '')} <b>{fmt(a.direction_pct, 0)}%</b>
                {a.holdout_direction_pct != null
                  ? <> · Holdout <b>{fmt(a.holdout_direction_pct, 0)}%</b></> : null}
                {a.trend_hit_pct != null
                  ? <> · Trend <b>{fmt(a.trend_hit_pct, 0)}%</b></> : null}
              </span>
            ))}
          </div>
        );
      })()}
      {scopes.length > 1 && (
        <div className="rl-scope-tabs">
          {scopes.map(s => (
            <button key={s.id} className={`opt-chip ${tab === s.id ? 'on' : ''}`}
              onClick={() => setTab(s.id)} data-testid={`regime-scope-tab-${s.id}`}>{s.label}</button>
          ))}
        </div>
      )}
      {isCombined && (analysis.combined?.coin_similarity || []).length > 0 && (
        <div className="rl-sim" data-testid="regime-coin-similarity">
          <span className="opt-small" style={{ alignSelf: 'center' }}
            title="Anteil der Zeit, in der zwei Coins im selben Regime sind – Coins mit hoher Übereinstimmung passen gut in eine gemeinsame dynamische Strategie">
            Regime-Übereinstimmung:
          </span>
          {analysis.combined.coin_similarity.map((s, i) => (
            <span key={i} className="opt-param-pill"
              style={s.agreement_pct >= 70 ? { borderColor: 'rgba(0,229,160,0.4)' } : undefined}>
              {s.a.replace('USDT', '')}↔{s.b.replace('USDT', '')} <b>{fmt(s.agreement_pct, 0)}%</b>
            </span>
          ))}
        </div>
      )}
      <TransitionMatrix analysisId={analysis.id} scope={scope} symbol={symbol}
        regimes={regimes} model={model} />
      {isCombined
        ? (analysis.symbols || []).map(sym => (
          <RegimeChart key={sym} title={sym.replace('USDT', '')}
            prices={analysis.chart?.[sym]}
            segments={analysis.combined?.per_symbol?.[sym]?.segments}
            idealSegments={showIdeal ? analysis.combined?.per_symbol?.[sym]?.ideal?.segments : null}
            liveSegments={analysis.combined?.per_symbol?.[sym]?.live_segments}
            liveBand={showLive} emas={analysis.chart_emas?.[sym]}
            regimes={regimes} model={model} trainEndTs={trainEnd(sym)} />
        ))
        : (
          <RegimeChart title={symbol.replace('USDT', '')}
            prices={analysis.chart?.[symbol]}
            segments={analysis.per_coin?.[symbol]?.segments}
            idealSegments={showIdeal ? analysis.per_coin?.[symbol]?.ideal?.segments : null}
            liveSegments={analysis.per_coin?.[symbol]?.live_segments}
            liveBand={showLive} emas={analysis.chart_emas?.[symbol]}
            regimes={regimes} model={model} trainEndTs={trainEnd(symbol)} height={240} />
        )}
      {Object.values(perSymbol).some(v => v?.live_segments?.length) && (
        <label className="opt-check" style={{ paddingBottom: 0 }}
          title="Band oben im Chart: so hat die Erkennung die Phasen über den GESAMTEN Zeitraum in Echtzeit gesehen (ohne rückwirkende Korrektur bis zum Umkehrpunkt). Abweichungen zum Hintergrund = Erkennungsverzögerung an den Umkehrpunkten. Der Bereich nach der orangen Holdout-Linie zeigt die Live-Sicht bereits als leuchtenden Hintergrund.">
          <input type="checkbox" checked={showLive} onChange={e => setShowLive(e.target.checked)}
            data-testid="regime-show-live" /> Live-Band über gesamten Zeitraum einblenden
        </label>
      )}
      {Object.values(perSymbol).some(v => v?.ideal?.segments?.length) && (
        <label className="opt-check" style={{ paddingBottom: 0 }}
          title="Vergleichsband: so lagen die Phasen im Rückblick (mit Zukunftssicht) – nur zur Kontrolle, wird nie für Backtests/Live genutzt">
          <input type="checkbox" checked={showIdeal} onChange={e => setShowIdeal(e.target.checked)}
            data-testid="regime-show-ideal" /> Rückblick-Vergleich einblenden (nur Kontrolle)
        </label>
      )}
      <div className="opt-section-title">REGIME PRÜFEN, BEHALTEN & STRATEGIEN SUCHEN</div>
      {regimes.map(r => (
        <RegimeCard key={r.id} analysis={analysis} scope={scope} symbol={symbol}
          regime={r} usage={usage?.[String(r.id)]} strategies={strategies}
          jobBlocked={jobBlocked} execution={execution} model={model}
          onChanged={onChanged} />
      ))}
      <BuildAndTest analysis={analysis} scope={scope} symbol={symbol}
        strategies={strategies} jobBlocked={jobBlocked} execution={execution}
        onChanged={onChanged} />
    </div>
  );
}

// ---------------- Haupt-Panel ----------------
export default function RegimeLab({ onClose }) {
  const saved = useRef(loadState()).current;
  const [coins, setCoins] = useState([]);
  const [strategies, setStrategies] = useState([]);
  const [selCoins, setSelCoins] = useState(saved.selCoins || []);
  const [timeframe, setTimeframe] = useState(saved.timeframe || '15m');
  const [days, setDays] = useState(saved.days ?? 360);
  const [scope, setScope] = useState(saved.scope || 'both');
  const [maxRegimes, setMaxRegimes] = useState(saved.maxRegimes ?? 5);
  const [lookback, setLookback] = useState(saved.lookback ?? 3);
  const [minShare, setMinShare] = useState(saved.minShare ?? 5);
  const [confMin, setConfMin] = useState(saved.confMin ?? 70);
  const [minHold, setMinHold] = useState(saved.minHold ?? 0);
  const [trainPct, setTrainPct] = useState(saved.trainPct ?? 75);
  const [engine, setEngine] = useState(saved.engine || 'v2');
  const [engineConfig, setEngineConfig] = useState(saved.engineConfig || {});
  const [execution, setExecution] = useState(saved.execution || 'cloud');
  const [lwOnline, setLwOnline] = useState(false);
  const [showLW, setShowLW] = useState(false);
  const [name, setName] = useState('');
  const [job, setJob] = useState(null);
  const [analyses, setAnalyses] = useState(null);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const pollRef = useRef(null);

  useEffect(() => {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({
        selCoins, timeframe, days, scope, maxRegimes, lookback, minShare, confMin, minHold, trainPct, execution,
        engine, engineConfig,
      }));
    } catch { /* quota */ }
  }, [selCoins, timeframe, days, scope, maxRegimes, lookback, minShare, confMin, minHold, trainPct, execution,
    engine, engineConfig]);

  const loadList = useCallback(() => {
    fetch(`${API_URL}/api/regime-lab/list`).then(r => r.json())
      .then(d => setAnalyses(d.analyses || [])).catch(() => setAnalyses([]));
  }, []);

  const loadDetail = useCallback((aid) => {
    fetch(`${API_URL}/api/regime-lab/${aid}`).then(r => r.json())
      .then(d => setDetail(d.analysis)).catch(() => toast.error('Analyse konnte nicht geladen werden'));
  }, []);

  useEffect(() => {
    fetch(`${API_URL}/api/coins`).then(r => r.json()).then(d => {
      const cs = d.coins || [];
      setCoins(cs);
      setSelCoins(prev => {
        const valid = (prev || []).filter(c => cs.includes(c));
        return valid.length ? valid : cs.slice(0, 4);
      });
    });
    fetch(`${API_URL}/api/strategies`).then(r => r.json()).then(d => setStrategies(d.strategies || []));
    fetch(`${API_URL}/api/regime-lab/active`).then(r => r.json()).then(d => {
      if (d.active) attachPoll(d.active.id, d.active.kind);
    }).catch(() => {});
    loadList();
    const checkLw = () => fetch(`${API_URL}/api/localworker/status`).then(r => r.json())
      .then(d => setLwOnline(!!d.online)).catch(() => setLwOnline(false));
    checkLw();
    const lwIv = setInterval(checkLw, 10000);
    return () => { clearInterval(pollRef.current); clearInterval(lwIv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { if (selected) loadDetail(selected); }, [selected, loadDetail]);

  const attachPoll = (jobId, kind) => {
    setJob({ id: jobId, kind, status: 'running', progress: 0, phase: 'Läuft...' });
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const j = await fetch(`${API_URL}/api/regime-lab/status/${jobId}`).then(x => x.json());
        setJob(j);
        if (j.status !== 'running') {
          clearInterval(pollRef.current);
          if (j.status === 'done' && j.kind === 'analysis') {
            toast.success('Regime-Analyse fertig');
            loadList();
            if (j.result?.analysis_id) setSelected(j.result.analysis_id);
          } else if (j.status === 'error') {
            toast.error(j.error || 'Job fehlgeschlagen');
          }
          setTimeout(() => setJob(null), 4000);
        }
      } catch { /* transient */ }
    }, 1500);
  };

  const startAnalysis = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (!selCoins.length) { toast.error('Mindestens 1 Coin wählen'); return; }
    if (execution === 'local' && !lwOnline) {
      toast.error('Kein lokaler Worker verbunden – Worker starten oder Cloud wählen');
      return;
    }
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/analyze`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          symbols: selCoins, timeframe, days, scope, name: name || undefined,
          max_regimes: maxRegimes, lookback_days: lookback, min_share_pct: minShare,
          confidence_min: confMin, min_hold_days: minHold, train_pct: trainPct,
          engine, engine_config: engine === 'v2' ? { min_phase_days: minHold, ...engineConfig } : undefined,
          execution,
        }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      attachPoll(d.job_id, 'analysis');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const cancelJob = async () => {
    if (job?.id) await fetch(`${API_URL}/api/regime-lab/cancel/${job.id}`, { method: 'POST', headers: authHeaders() });
  };

  const removeAnalysis = async (aid, e) => {
    e.stopPropagation();
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const r = await fetch(`${API_URL}/api/regime-lab/${aid}`, { method: 'DELETE', headers: authHeaders() });
    if (r.ok) {
      toast.success('Analyse gelöscht');
      if (selected === aid) { setSelected(null); setDetail(null); }
      loadList();
    } else toast.error('Löschen fehlgeschlagen');
  };

  const jobBlocked = job?.status === 'running';
  return (
    <SafeOverlay className="opt-overlay" onClose={onClose} testId="regime-lab-overlay">
      <div className="opt-panel" onClick={e => e.stopPropagation()} data-testid="regime-lab-modal">
        <div className="opt-header">
          <h2 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <ChartScatter size={20} weight="bold" /> Regime-Lab
          </h2>
          <button className="opt-close" onClick={onClose} data-testid="regime-lab-close"><X size={22} weight="bold" /></button>
        </div>
        <div className="opt-small" style={{ marginBottom: 12 }}>
          Workflow: 1) Regime für eine Konfiguration suchen & speichern → 2) Regime am Chart prüfen,
          unsinnige verwerfen → 3) je Regime mit Discovery/Optimierer eine Strategie suchen & bestätigen →
          4) dynamische Strategie zusammenstellen und auf dem unangetasteten Holdout per Walk-Forward testen (kein Lookahead).
        </div>

        <div className="opt-exec-row" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', margin: '0 0 10px' }}>
          <div className="bt-exec" data-testid="regime-execution-toggle">
            <span className="bt-exec-label">Ausführung</span>
            <button className={`bt-exec-btn ${execution === 'cloud' ? 'on' : ''}`}
              onClick={() => setExecution('cloud')} data-testid="regime-exec-cloud"
              title="Berechnung auf dem Server – für kurze Zeiträume ok">
              <Cloud size={13} weight="bold" /> Cloud
            </button>
            <button className={`bt-exec-btn ${execution === 'local' ? 'on' : ''}`}
              onClick={() => setExecution('local')} data-testid="regime-exec-local"
              title="Berechnung auf deinem PC (lokaler Worker, Multi-Core + lokale Kerzendaten) – empfohlen ab ~1000 Tagen, entlastet die Website. Worker-Version 1.5.0+ nötig.">
              <Desktop size={13} weight="bold" /> Lokal
              <span className={`bt-exec-dot ${lwOnline ? 'on' : ''}`} data-testid="regime-exec-dot" />
            </button>
            <button className="bt-exec-manage" onClick={() => setShowLW(true)}
              title="Lokale Ausführung verwalten: Worker, Einstellungen & Marktdaten"
              data-testid="regime-exec-manage">
              <Gear size={13} weight="bold" />
            </button>
          </div>
          {execution === 'local' && (
            <span className="opt-small">
              Gilt für alle Regime-Lab-Jobs (Analyse, Strategie-Suche, Walk-Forward)
              {!lwOnline && ' · kein Worker verbunden'}
            </span>
          )}
        </div>
        {showLW && <LocalWorkerPanel onClose={() => setShowLW(false)} />}

        <div className="opt-row">
          <div className="opt-label">1 · NEUE REGIME-ANALYSE</div>
          <div className="opt-chips" style={{ marginBottom: 8 }}>
            {coins.map(c => (
              <button key={c} className={`opt-chip ${selCoins.includes(c) ? 'on' : ''}`}
                onClick={() => setSelCoins(selCoins.includes(c) ? selCoins.filter(x => x !== c) : [...selCoins, c])}
                data-testid={`regime-coin-${c}`}>{c.replace('USDT', '')}</button>
            ))}
          </div>
          <RegimeEngineSettings engine={engine} setEngine={setEngine}
            config={engineConfig} setConfig={setEngineConfig}
            calibrateCtx={{ symbols: selCoins, timeframe, days, execution }} />
          <div className="opt-setup">
            <label className="opt-field">Name (optional)
              <input value={name} onChange={e => setName(e.target.value)} style={{ width: 170 }}
                placeholder="z.B. 15m · 360d · Top4" data-testid="regime-name" />
            </label>
            <label className="opt-field">Timeframe
              <select value={timeframe} onChange={e => setTimeframe(e.target.value)} data-testid="regime-tf">
                {TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}
              </select>
            </label>
            <label className="opt-field">Zeitraum
              <select value={days} onChange={e => setDays(parseInt(e.target.value))} data-testid="regime-days">
                {DAY_OPTIONS.map(d => <option key={d} value={d}>{`${d} Tage`}</option>)}
              </select>
            </label>
            <label className="opt-field" title="Kombiniert = ein Modell über alle Coins · Je Coin = eigenes Modell pro Coin · Beides = beide Varianten zum Vergleichen">
              Modell-Umfang
              <select value={scope} onChange={e => setScope(e.target.value)} data-testid="regime-scope">
                <option value="both">Beides (kombiniert + je Coin)</option>
                <option value="combined">Nur kombiniert (alle Coins)</option>
                <option value="per_coin">Nur je Coin einzeln</option>
              </select>
            </label>
            <label className="opt-field" title="Vorderer Anteil für Regime-Clustering & Strategie-Suche; der Rest bleibt unangetastet für den finalen Walk-Forward">
              Training %
              <input type="number" min={50} max={100} value={trainPct}
                onChange={e => setTrainPct(parseInt(e.target.value) || 75)}
                data-testid="regime-trainpct" style={{ width: 55 }} />
            </label>
            {engine === 'kmeans' && (
              <>
                <label className="opt-field" title="Fenster für Trend/Volatilität/Effizienz – größer = trägere, stabilere Regime">
                  Lookback (Tage)
                  <input type="number" min={0.5} max={60} step={0.5} value={lookback}
                    onChange={e => setLookback(parseFloat(e.target.value) || 3)}
                    data-testid="regime-lookback" style={{ width: 55 }} />
                </label>
                <label className="opt-field">Max. Regime
                  <input type="number" min={2} max={10} value={maxRegimes}
                    onChange={e => setMaxRegimes(parseInt(e.target.value) || 5)}
                    data-testid="regime-max" style={{ width: 50 }} />
                </label>
                <label className="opt-field" title="Regime mit kleinerem Anteil werden zusammengelegt">
                  Min. Anteil %
                  <input type="number" min={1} max={30} value={minShare}
                    onChange={e => setMinShare(parseInt(e.target.value) || 5)}
                    data-testid="regime-minshare" style={{ width: 50 }} />
                </label>
              </>
            )}
            <label className="opt-field" title="Umschalten nur bei dieser Sicherheit (Anti-Flattern)">
              Sicherheit %
              <input type="number" min={50} max={95} value={confMin}
                onChange={e => setConfMin(parseInt(e.target.value) || 70)}
                data-testid="regime-confmin" style={{ width: 50 }} />
            </label>
            <label className="opt-field"
              title="Mini-Phasen-Filter: kürzere Auf-/Ab-/Seitwärts-Phasen werden mit dem längeren Nachbarn zusammengelegt – weniger Mini-Regime, handelbarere Abschnitte. 0 = automatisch (~1% des Zeitraums)">
              Min. Phasendauer (d)
              <input type="number" min={0} max={60} step={0.5} value={minHold}
                onChange={e => setMinHold(parseFloat(e.target.value) || 0)}
                data-testid="regime-minhold" style={{ width: 55 }} />
            </label>
            <button className="opt-run" onClick={startAnalysis} disabled={jobBlocked} data-testid="regime-analyze-btn">
              <Play size={14} weight="fill" /> Regime suchen & speichern
            </button>
          </div>
          {job && (
            <div className="opt-progress">
              <div className="opt-progress-bar"><div style={{ width: `${job.progress || 0}%`, height: '100%', background: '#b388ff' }} /></div>
              <div className="opt-progress-row">
                <div className="opt-progress-text">
                  {job.kind === 'analysis' ? 'Analyse' : job.kind === 'regime_opt' ? 'Regime-Optimierung' : 'Walk-Forward'} ·
                  {' '}{job.phase} · {job.progress || 0}%
                </div>
                {job.status === 'running' && (
                  <button className="opt-cancel-run" onClick={cancelJob} data-testid="regime-job-cancel">Abbrechen</button>
                )}
              </div>
            </div>
          )}
        </div>

        <EmaPeriodCompare selCoins={selCoins} timeframe={timeframe} days={days}
          trainPct={trainPct} engineConfig={engineConfig} jobBlocked={jobBlocked} />

        <KombiAutoCalibrate selCoins={selCoins} timeframe={timeframe} days={days}
          trainPct={trainPct} engineConfig={engineConfig}
          setEngineConfig={setEngineConfig} jobBlocked={jobBlocked} />

        <div className="opt-row">
          <div className="opt-label" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            2 · GESPEICHERTE ANALYSEN
            <button className="opt-chip" style={{ fontSize: 10 }} onClick={loadList} data-testid="regime-list-refresh">
              <ArrowClockwise size={11} />
            </button>
          </div>
          {analyses === null && <div className="opt-small">Lade...</div>}
          {analyses !== null && analyses.length === 0 && (
            <div className="opt-small">Noch keine Analysen – oben Konfiguration wählen und "Regime suchen & speichern" starten.</div>
          )}
          {(analyses || []).map(a => (
            <div key={a.id} className={`rl-analysis-row ${selected === a.id ? 'on' : ''}`}
              onClick={() => setSelected(selected === a.id ? null : a.id)}
              data-testid={`regime-analysis-row-${a.id}`}>
              <b>{a.name}</b>
              <span className="opt-small">{(a.symbols || []).map(s => s.replace('USDT', '')).join(', ')}</span>
              <span className="opt-small">{a.timeframe} · {a.days}d · Training {a.settings?.train_pct}%</span>
              {a.n_regimes_combined > 0 && <span className="opt-small">{a.n_regimes_combined} Regime (kombiniert)</span>}
              {a.n_assignments > 0 && <span className="opt-small pos">{a.n_assignments} Strategie(n) bestätigt</span>}
              {a.has_walkforward && <span className="opt-small pos">WF getestet</span>}
              <span style={{ flex: 1 }} />
              <span className="opt-small">{fmtDateTime(a.created_at)}</span>
              <button className="opt-chip" onClick={(e) => removeAnalysis(a.id, e)} data-testid={`regime-analysis-delete-${a.id}`}>
                <Trash size={11} />
              </button>
            </div>
          ))}
        </div>

        {selected && detail && (
          <div className="opt-row">
            <div className="opt-label">3 · ANALYSE: {detail.name}</div>
            <AnalysisDetail analysis={detail} strategies={strategies}
              jobBlocked={jobBlocked} execution={execution} onChanged={() => loadDetail(selected)} />
          </div>
        )}

        <div className="opt-row">
          <div className="opt-label">4 · DYNAMISCHE STRATEGIEN (LIVE/PAPER)</div>
          <div className="opt-small" style={{ marginBottom: 6 }}>
            Aktuelles Regime je Coin, aktive Strategie, Auto-Umschaltung inkl. optionaler
            manueller Bestätigung und Wechsel-Protokoll – alles an einem Ort.
          </div>
          <DynamicPanel />
        </div>
      </div>
    </SafeOverlay>
  );
}
