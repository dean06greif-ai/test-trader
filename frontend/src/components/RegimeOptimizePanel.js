import React, { useState, useRef, useEffect } from 'react';
import { Play, CheckCircle, X } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import TIMEFRAMES from '../constants/timeframes';
import { INDICATOR_GROUPS, INDICATOR_POOL } from '../lib/indicatorPool';

const API_URL = process.env.REACT_APP_BACKEND_URL;
const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

export { INDICATOR_POOL };

const OPT_GROUPS = [
  { k: 'tpsl', l: 'TP/SL' },
  { k: 'breakeven', l: 'Break-Even' },
  { k: 'profit_secure', l: 'Gewinnsicherung' },
  { k: 'leverage', l: 'Hebel' },
  { k: 'auto_leverage', l: 'Auto-Leverage' },
  { k: 'sessions', l: 'Zeitfenster' },
];

const MODES = [
  { id: 'discovery', l: 'Discovery', d: 'Neue Regeln nur aus den gewählten Indikatoren für diese Marktphase bauen' },
  { id: 'combo', l: 'Discovery + Optimierung', d: 'Regeln entdecken UND Trade-Parameter für diese Phase optimieren' },
  { id: 'params', l: 'Nur Parameter', d: 'Trade-Parameter einer bestehenden Strategie für diese Phase optimieren' },
];

const Metrics = ({ m }) => m ? (
  <span className="opt-small">
    PnL <b className={m.pnl >= 0 ? 'pos' : 'neg'}>{fmt(m.pnl)}</b> ·
    WR <b>{fmt(m.win_rate, 1)}%</b> · Trades <b>{m.trades}</b> · DD <b>{fmt(m.max_drawdown)}</b>
  </span>
) : <span className="opt-small">–</span>;

/**
 * Strategie-Suche für EIN ausgewähltes Regime einer gespeicherten Analyse –
 * mit allen Einstellungen von Discovery & Optimierer, aber statt "Tage"
 * zählt nur der Kerzen-Anteil dieser Marktphase.
 */
export default function RegimeOptimizePanel({ analysisId, scope, symbol, regime,
  strategies, analysisTf, onAssigned, jobBlocked, execution }) {
  const [mode, setMode] = useState('combo');
  const [strategyId, setStrategyId] = useState('');
  const [baseStrategy, setBaseStrategy] = useState('');
  const [indicators, setIndicators] = useState(INDICATOR_POOL.map(i => i.id));
  const [iterations, setIterations] = useState(40);
  const [objective, setObjective] = useState('combo');
  const [minTrades, setMinTrades] = useState(10);
  const [maxRules, setMaxRules] = useState(4);
  const [optFlags, setOptFlags] = useState({ tpsl: true, leverage: true });
  const [tf, setTf] = useState(analysisTf);
  const [regimeWf, setRegimeWf] = useState(true);
  const [regimeTrainPct, setRegimeTrainPct] = useState(75);
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [assigning, setAssigning] = useState(null);
  const [optStratParams, setOptStratParams] = useState(false);
  const [deepTest, setDeepTest] = useState(false);
  const [directionBias, setDirectionBias] = useState('auto');
  const pollRef = useRef(null);

  useEffect(() => () => clearInterval(pollRef.current), []);

  const customStrategies = strategies.filter(s => s.is_custom);

  const start = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (mode === 'params' && !strategyId) { toast.error('Strategie wählen'); return; }
    if (mode !== 'params' && indicators.length === 0) { toast.error('Mind. 1 Indikator anhaken'); return; }
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/${analysisId}/optimize`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          scope, symbol, regime_id: regime.id, mode,
          strategy_id: mode === 'params' ? strategyId : undefined,
          base_strategy_id: mode !== 'params' && baseStrategy ? baseStrategy : undefined,
          indicators: mode === 'params' ? undefined : indicators,
          iterations, objective, min_trades: minTrades, max_rules: maxRules,
          optimize_strategy_params: mode === 'params' ? optStratParams : undefined,
          deep_test: mode !== 'params' ? deepTest : undefined,
          optimize: optFlags, timeframe: tf,
          direction_bias: directionBias,
          regime_walk_forward: regimeWf, regime_train_pct: regimeTrainPct,
          execution,
        }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      setResult(null);
      setJob({ id: d.job_id, status: 'running', progress: 0, phase: 'Startet' });
      pollRef.current = setInterval(async () => {
        try {
          const j = await fetch(`${API_URL}/api/regime-lab/status/${d.job_id}`).then(x => x.json());
          setJob(j);
          if (j.status !== 'running') {
            clearInterval(pollRef.current);
            if (j.status === 'done') setResult(j.result);
            else if (j.status === 'error') toast.error(j.error || 'Fehlgeschlagen');
          }
        } catch { /* transient */ }
      }, 1500);
    } catch { toast.error('Verbindungsfehler'); }
  };

  const cancel = async () => {
    if (job?.id) await fetch(`${API_URL}/api/regime-lab/cancel/${job.id}`, { method: 'POST', headers: authHeaders() });
  };

  const assign = async (cand, idx) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setAssigning(idx);
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/${analysisId}/assign`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          scope, symbol, regime_id: regime.id,
          candidate: {
            mode: result.mode,
            strategy_id: result.strategy_id,
            strategy_name: result.strategy_name,
            definition: result.definition,
            rules: result.discovery?.rules || [],
            trade_params: cand.trade_params,
            strategy_params: cand.strategy_params,
            metrics: cand.metrics,
            validation: cand.validation,
            source_job_id: job?.id,
          },
        }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Übernahme fehlgeschlagen'); return; }
      toast.success(`Strategie für "${regime.label}" bestätigt`);
      onAssigned?.();
    } catch { toast.error('Verbindungsfehler'); }
    finally { setAssigning(null); }
  };

  const running = job?.status === 'running';
  return (
    <div className="rl-opt-box" data-testid={`regime-opt-panel-${regime.id}`}>
      <div className="opt-chips" style={{ marginBottom: 8 }}>
        {MODES.map(m => (
          <button key={m.id} className={`opt-chip ${mode === m.id ? 'on' : ''}`}
            title={m.d} onClick={() => setMode(m.id)}
            data-testid={`regime-opt-mode-${m.id}-${regime.id}`}>{m.l}</button>
        ))}
      </div>
      <div className="opt-setup">
        {mode === 'params' && (
          <label className="opt-field">Strategie
            <select value={strategyId} onChange={e => setStrategyId(e.target.value)}
              data-testid={`regime-opt-strategy-${regime.id}`}>
              <option value="">– wählen –</option>
              {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        )}
        {mode === 'params' && (
          <label className="opt-check" style={{ paddingBottom: 0 }}
            title="Sucht zusätzlich die Strategie-Parameter (Perioden, Schwellen, Filter) für genau dieses Regime – z.B. um NNFX je Marktphase zu justieren">
            <input type="checkbox" checked={optStratParams}
              onChange={e => setOptStratParams(e.target.checked)}
              data-testid={`regime-opt-stratparams-${regime.id}`} />
            Strategie-Parameter mitoptimieren
          </label>
        )}
        {mode !== 'params' && (
          <label className="opt-field" title="Optional: bestehende Custom-Strategie als Ausgangspunkt weiterentwickeln">
            Basis (optional)
            <select value={baseStrategy} onChange={e => setBaseStrategy(e.target.value)}
              data-testid={`regime-opt-base-${regime.id}`}>
              <option value="">Von Null starten</option>
              {customStrategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        )}
        <label className="opt-field" title="Timeframe der Simulation – die Regime-Zeitbereiche der Analyse bleiben identisch">
          Timeframe
          <select value={tf} onChange={e => setTf(e.target.value)} data-testid={`regime-opt-tf-${regime.id}`}>
            {TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}{t.v === analysisTf ? ' (Analyse)' : ''}</option>)}
          </select>
        </label>
        <label className="opt-field"
          title="Richtungs-Bias (Etappe 3): auto = Aufwärts-Regime handelt nur Longs, Abwärts-Regime nur Shorts, Seitwärts beides. Der Bias wandert mit in die dynamische Strategie (Walk-Forward/Live).">
          Richtung
          <select value={directionBias} onChange={e => setDirectionBias(e.target.value)}
            data-testid={`regime-opt-bias-${regime.id}`}>
            <option value="auto">Auto (Regime-Richtung)</option>
            <option value="off">Beide Seiten (kein Bias)</option>
            <option value="long">Nur Longs</option>
            <option value="short">Nur Shorts</option>
          </select>
        </label>
        <label className="opt-field">Ziel
          <select value={objective} onChange={e => setObjective(e.target.value)} data-testid={`regime-opt-objective-${regime.id}`}>
            <option value="combo">Kombi (PnL × Winrate)</option>
            <option value="win_rate">Höchste Win-Rate</option>
            <option value="pnl">Höchster PnL</option>
          </select>
        </label>
        <label className="opt-field">Iterationen
          <input type="number" min={0} max={500} value={iterations}
            onChange={e => setIterations(parseInt(e.target.value) || 0)}
            data-testid={`regime-opt-iterations-${regime.id}`} style={{ width: 70 }} />
        </label>
        <label className="opt-field">Min. Trades
          <input type="number" min={1} max={500} value={minTrades}
            onChange={e => setMinTrades(parseInt(e.target.value) || 1)}
            data-testid={`regime-opt-mintrades-${regime.id}`} style={{ width: 60 }} />
        </label>
        {mode !== 'params' && (
          <label className="opt-field">Max. Regeln
            <input type="number" min={1} max={8} value={maxRules}
              onChange={e => setMaxRules(parseInt(e.target.value) || 1)}
              data-testid={`regime-opt-maxrules-${regime.id}`} style={{ width: 55 }} />
          </label>
        )}
        {mode !== 'params' && (
          <label className="opt-check"
            title="Statt Greedy: Einzeltest aller Indikatoren, ALLE Paare, Beam-Suche, Austausch-Phase und Beitrags-Auswertung – nur auf den Kerzen dieser Marktphase. Dauert deutlich länger.">
            <input type="checkbox" checked={deepTest}
              onChange={e => setDeepTest(e.target.checked)}
              data-testid={`regime-opt-deep-${regime.id}`} />
            Deep-Test (alle Kombinationen)
          </label>
        )}
        <label className="opt-check" title="Trainingsteil der Phase nochmals teilen: nur Varianten übernehmen, die auf dem unbekannten Teil DERSELBEN Phase profitabel bleiben">
          <input type="checkbox" checked={regimeWf} onChange={e => setRegimeWf(e.target.checked)}
            data-testid={`regime-opt-wf-${regime.id}`} />
          Walk-Forward in der Phase
        </label>
        {regimeWf && (
          <label className="opt-field">Training %
            <input type="number" min={40} max={95} value={regimeTrainPct}
              onChange={e => setRegimeTrainPct(parseInt(e.target.value) || 75)}
              data-testid={`regime-opt-trainpct-${regime.id}`} style={{ width: 55 }} />
          </label>
        )}
      </div>
      {mode !== 'params' && (
        <div className="opt-row">
          <div className="opt-label">INDIKATOREN FÜR DIESE MARKTPHASE (über die Erklärung hovern)</div>
          {INDICATOR_GROUPS.map(g => {
            const ids = g.items.map(i => i.id);
            const allOn = ids.every(id => indicators.includes(id));
            return (
              <div key={g.key} className="opt-ind-group" data-testid={`regime-opt-ind-group-${g.key}-${regime.id}`}>
                <div className="opt-ind-group-head">
                  <span className="opt-ind-group-title" title={g.hint}>{g.label}</span>
                  <button className="opt-ind-group-toggle"
                    data-testid={`regime-opt-ind-group-toggle-${g.key}-${regime.id}`}
                    onClick={() => setIndicators(allOn
                      ? indicators.filter(x => !ids.includes(x))
                      : [...new Set([...indicators, ...ids])])}>
                    {allOn ? 'abwählen' : 'alle'}
                  </button>
                </div>
                <div className="opt-chips">
                  {g.items.map(i => (
                    <button key={i.id} className={`opt-chip ${indicators.includes(i.id) ? 'on' : ''}`}
                      onClick={() => setIndicators(indicators.includes(i.id)
                        ? indicators.filter(x => x !== i.id) : [...indicators, i.id])}
                      title={i.desc}
                      data-testid={`regime-opt-ind-${i.id}-${regime.id}`}>{i.label}</button>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="opt-row">
        <div className="opt-label">TRADE-PARAMETER MITOPTIMIEREN</div>
        <div className="opt-chips">
          {OPT_GROUPS.map(g => (
            <button key={g.k} className={`opt-chip ${optFlags[g.k] ? 'on' : ''}`}
              onClick={() => setOptFlags(f => ({ ...f, [g.k]: !f[g.k] }))}
              data-testid={`regime-opt-group-${g.k}-${regime.id}`}>{g.l}</button>
          ))}
        </div>
      </div>
      <button className="opt-run" onClick={start} disabled={running || jobBlocked}
        data-testid={`regime-opt-run-${regime.id}`}>
        <Play size={14} weight="fill" /> {running ? 'Läuft...' : `Suche für "${regime.label}" starten`}
      </button>
      {running && (
        <div className="opt-progress">
          <div className="opt-progress-bar"><div style={{ width: `${job.progress || 0}%`, height: '100%', background: '#b388ff' }} /></div>
          <div className="opt-progress-row">
            <div className="opt-progress-text">{job.phase} · {job.progress || 0}%</div>
            <button className="opt-cancel-run" onClick={cancel} data-testid={`regime-opt-cancel-${regime.id}`}>Abbrechen</button>
          </div>
        </div>
      )}
      {result && (
        <div className="opt-result" data-testid={`regime-opt-result-${regime.id}`}>
          <div className="opt-small" style={{ marginBottom: 6 }}>
            Bewertet auf <b>{result.segments_info?.segments}</b> Abschnitten ·{' '}
            <b>{result.segments_info?.days}</b> Tage in dieser Phase ({result.symbols?.join(', ')})
            {result.regime_walk_forward && ' · Walk-Forward innerhalb der Phase aktiv'}
            {result.direction_bias?.allowed_sides && (
              <span data-testid={`regime-opt-bias-out-${regime.id}`}>
                {' '}· Richtungs-Bias: <b>{result.direction_bias.allowed_sides.join('+') === 'LONG' ? 'nur Longs' : 'nur Shorts'}</b>
              </span>
            )}
            {result.direction_bias && !result.direction_bias.allowed_sides
              && result.direction_bias.mode === 'auto' && (
              <span data-testid={`regime-opt-bias-out-${regime.id}`}>
                {' '}· Richtungs-Bias: <b>beide Seiten</b> (Seitwärts-Regime)
              </span>
            )}
          </div>
          {result.discovery && (
            <div className="opt-small" style={{ marginBottom: 6 }}>
              {result.discovery.rules?.length
                ? <>Gefundene Regeln: {result.discovery.rules.map((r, i) => <span key={i} className="opt-param-pill">{r}</span>)}</>
                : <span className="neg">{result.discovery.note || 'Keine Regel-Kombination gefunden'}</span>}
            </div>
          )}
          {result.discovery?.deep_report && (
            <div className="opt-small" style={{ marginBottom: 6 }}
              data-testid={`regime-opt-deep-report-${regime.id}`}>
              <div style={{ color: '#8A8FA3', marginBottom: 3 }}>
                DEEP-TEST: {result.discovery.deep_report.candidates} Kandidaten ·
                {' '}{result.discovery.deep_report.pairs_tested} Paare geprüft
              </div>
              {(result.discovery.deep_report.contribution || []).map((c, i) => (
                <span key={i} className="opt-param-pill"
                  title={`Ohne diese Regel: Score ${c.score_without}`}>
                  {c.rule} <b>{c.delta >= 0 ? '+' : ''}{c.delta}</b>
                </span>
              ))}
              {(result.discovery.deep_report.best_synergies || []).slice(0, 3).map((s, i) => (
                <span key={`s${i}`} className="opt-param-pill"
                  title="Zugewinn gegenüber dem besten Einzelteil">
                  {s.a} + {s.b} <b>{s.synergy >= 0 ? '+' : ''}{s.synergy}</b>
                </span>
              ))}
            </div>
          )}
          {(result.top5 || []).length === 0 && (
            <div className="opt-small neg">Keine bewertbaren Kandidaten – Indikatoren/Einstellungen ändern und erneut versuchen.</div>
          )}
          {(result.top5 || []).map((c, i) => (
            <div key={i} className="opt-card" style={{ marginBottom: 6 }} data-testid={`regime-opt-top-${regime.id}-${i}`}>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                <b style={{ fontSize: 12 }}>#{i + 1}</b>
                <Metrics m={c.metrics} />
                {c.validation && (
                  <span className={`opt-small ${c.validation_passed ? 'pos' : 'neg'}`}
                    title="Ergebnis auf dem unbekannten Teil derselben Marktphase">
                    WF: PnL {fmt(c.validation.pnl)} · {c.validation.trades} T. {c.validation_passed ? '✓' : '✗'}
                  </span>
                )}
                <span style={{ flex: 1 }} />
                <button className="opt-apply" onClick={() => assign(c, i)} disabled={assigning !== null}
                  data-testid={`regime-opt-assign-${regime.id}-${i}`}>
                  <CheckCircle size={13} /> Für dieses Regime übernehmen
                </button>
              </div>
              {Object.keys(c.strategy_params || {}).length > 0 && (
                <div className="opt-small" data-testid={`regime-opt-stratparams-out-${regime.id}-${i}`}>
                  Strategie-Parameter: {Object.entries(c.strategy_params).map(([k, v]) =>
                    `${k}=${v}`).join(' · ')}
                </div>
              )}
              {Object.keys(c.trade_params || {}).length > 0 && (
                <div className="opt-params-list" style={{ marginTop: 6, marginBottom: 0 }}>
                  {Object.entries(c.trade_params).map(([k, v]) =>
                    <span key={k} className="opt-param-pill trade">{k}: <b>{String(v)}</b></span>)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {job && job.status === 'error' && (
        <div className="opt-small neg" style={{ marginTop: 6 }}><X size={11} /> {job.error}</div>
      )}
    </div>
  );
}
