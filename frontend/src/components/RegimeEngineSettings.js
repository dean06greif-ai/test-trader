import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CaretDown, CaretRight, ArrowCounterClockwise, Flask } from '@phosphor-icons/react';
import { authHeaders } from '../auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const BOOL_KEYS = ['require_adx', 'require_efficiency', 'require_range_break',
  'strong_bypass_range', 'use_variance_ratio', 'auto_adapt', 'mtf_confirm',
  'use_volume_confirm', 'use_ema_confirm', 'kombi_pivot_accel'];

const MODE_LABELS = {
  3: '3 Regime · Aufwärts / Seitwärts / Abwärts',
  5: '5 Regime · zusätzlich stark / leicht (empfohlen)',
  9: '9 Regime · Trend × Volatilität',
};

const MODE_HINT = {
  3: 'Gröbste Einteilung – die stabilsten, längsten Abschnitte.',
  5: 'Guter Kompromiss: klare Richtung plus Stärke, ohne Regime-Flut.',
  9: 'Feinste Einteilung – nur bei sehr langen Zeiträumen sinnvoll.',
};

// Diese Felder werden bei aktiver automatischer Anpassung aus dem Zeitraum
// berechnet – manuelles Setzen ist möglich (Override), wird aber markiert.
const ADAPTED_KEYS = ['horizons_days', 'min_hold_days', 'confirm_days', 'smooth_days',
  'vol_ref_days', 'vol_window_days', 'vol_smooth_days', 'gate_timeout_days',
  'validate_min_segment_days'];

/**
 * Einstellungen der Regime-Engine v2 (Regression + ADX + Volatilität +
 * Multi-Timeframe + Hysterese). Felder/Erklärungen kommen vom Backend
 * (/api/regime-lab/engine/defaults), damit UI und Engine nie auseinanderlaufen.
 */
export default function RegimeEngineSettings({ engine, setEngine, config, setConfig, calibrateCtx }) {
  const [defaults, setDefaults] = useState(null);
  const [open, setOpen] = useState(false);
  const [truthSource, setTruthSource] = useState('centered');
  const [calJob, setCalJob] = useState(null);      // {id, phase, progress}
  const [calReport, setCalReport] = useState(null);
  const [calErr, setCalErr] = useState(null);
  const calTimer = useRef(null);

  useEffect(() => () => clearInterval(calTimer.current), []);

  const startCalibrate = async () => {
    if (!calibrateCtx?.symbols?.length) { setCalErr('Mindestens 1 Coin auswählen'); return; }
    setCalErr(null); setCalReport(null);
    try {
      const r = await fetch(`${API_URL}/api/regime-lab/calibrate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          symbols: calibrateCtx.symbols, timeframe: calibrateCtx.timeframe,
          days: calibrateCtx.days, execution: calibrateCtx.execution,
          truth_source: truthSource, engine_config: config || {},
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'Start fehlgeschlagen');
      setCalJob({ id: d.job_id, phase: 'Startet', progress: 0 });
      calTimer.current = setInterval(async () => {
        try {
          const s = await fetch(`${API_URL}/api/regime-lab/status/${d.job_id}`).then(x => x.json());
          setCalJob({ id: d.job_id, phase: s.phase, progress: s.progress });
          if (s.status !== 'running') {
            clearInterval(calTimer.current);
            setCalJob(null);
            if (s.status === 'done' && s.result?.report) {
              setCalReport(s.result.report);
              if (s.result.report.best_config) setConfig({ ...s.result.report.best_config });
            } else if (s.status === 'error') setCalErr(s.error || 'Kalibrierung fehlgeschlagen');
          }
        } catch { /* nächster Poll */ }
      }, 1500);
    } catch (e) { setCalErr(String(e.message || e)); }
  };

  const cancelCalibrate = async () => {
    if (!calJob?.id) return;
    await fetch(`${API_URL}/api/regime-lab/cancel/${calJob.id}`,
      { method: 'POST', headers: authHeaders() }).catch(() => {});
  };

  useEffect(() => {
    fetch(`${API_URL}/api/regime-lab/engine/defaults`).then(r => r.json())
      .then(setDefaults).catch(() => setDefaults(null));
  }, []);

  const meta = useMemo(() => defaults?.meta || [], [defaults]);
  const val = (k) => (config?.[k] !== undefined ? config[k] : defaults?.config?.[k]);
  const set = (k, v) => setConfig({ ...(config || {}), [k]: v });
  const userSet = (k) => config?.[k] !== undefined;
  const mode = Number(val('regime_mode') || 5);
  const adapted = !!val('auto_adapt') && val('adapt_profile') !== 'off';
  const profiles = defaults?.adapt_profiles || [];
  const detector = String(val('detector') || 'reactive');
  const groups = useMemo(() => {
    const out = [];
    const idx = {};
    meta.forEach(m => {
      const g = m.group || 'Allgemein';
      if (idx[g] === undefined) {
        idx[g] = out.length;
        out.push({ name: g, detectors: m.detectors || null, items: [] });
      }
      out[idx[g]].items.push(m);
    });
    return out;
  }, [meta]);

  const field = (m) => {
    const v = val(m.key);
    if (m.key === 'regime_mode' || m.key === 'adapt_profile') return null;
    if (BOOL_KEYS.includes(m.key)) {
      return (
        <label className="opt-check" key={m.key} title={m.help} style={{ paddingBottom: 0 }}>
          <input type="checkbox" checked={!!v} onChange={e => set(m.key, e.target.checked)}
            data-testid={`engine-cfg-${m.key}`} /> {m.label}
        </label>
      );
    }
    if (m.key === 'horizons_days' || m.key === 'chart_ema_days') {
      const ph = m.key === 'horizons_days'
        ? (adapted ? 'automatisch aus Zeitraum' : '5, 10, 20, 50, 100')
        : '9, 21, 50, 200';
      return (
        <label className="opt-field" key={m.key} title={m.help} style={{ minWidth: 190 }}>
          {m.label}{m.key === 'horizons_days' && adapted && !userSet(m.key) ? ' (auto)' : ''}
          <input value={(v || []).join(', ')}
            onChange={e => set(m.key, e.target.value.split(',')
              .map(x => parseFloat(x.trim())).filter(x => !isNaN(x) && x > 0))}
            placeholder={ph}
            data-testid={`engine-cfg-${m.key}`} style={{ width: 150 }} />
        </label>
      );
    }
    if (m.key === 'vol_metric') {
      return (
        <label className="opt-field" key={m.key} title={m.help}>
          {m.label}
          <select value={v || 'atr'} onChange={e => set('vol_metric', e.target.value)}
            data-testid="engine-cfg-vol_metric">
            <option value="atr">ATR %</option>
            <option value="stdev">Realisierte Vola</option>
          </select>
        </label>
      );
    }
    if (m.key === 'detector') {
      return (
        <label className="opt-field" key={m.key} title={m.help}>
          {m.label}
          <select value={v || 'reactive'} onChange={e => set('detector', e.target.value)}
            data-testid="engine-cfg-detector">
            <option value="reactive">Umkehrpunkte (Standard)</option>
            <option value="ema">EMA-Steigung (einfach & glatt)</option>
            <option value="kombi">Kombi (EMA + Umkehrpunkte)</option>
            <option value="regression">Regression (alt)</option>
          </select>
        </label>
      );
    }
    return (
      <label className="opt-field" key={m.key} title={m.help}>
        {m.label}{adapted && ADAPTED_KEYS.includes(m.key) && !userSet(m.key) ? ' (auto)' : ''}
        <input type="number" step="0.05" value={v ?? ''}
          onChange={e => set(m.key, e.target.value === '' ? undefined : parseFloat(e.target.value))}
          data-testid={`engine-cfg-${m.key}`} style={{ width: 66 }} />
      </label>
    );
  };

  return (
    <div className="rl-engine-box" data-testid="regime-engine-settings">
      <div className="opt-setup" style={{ alignItems: 'center' }}>
        <label className="opt-field" title="v2 = feste Taxonomie aus Regression/ADX/Volatilität (empfohlen) · Cluster = altes K-Means-Verfahren">
          Regime-Engine
          <select value={engine} onChange={e => setEngine(e.target.value)} data-testid="regime-engine-select">
            <option value="v2">v2 · Regression + ADX + Vola</option>
            <option value="kmeans">Cluster (K-Means, alt)</option>
          </select>
        </label>
        {engine === 'v2' && (
          <label className="opt-field" title={MODE_HINT[mode]}>
            Anzahl Regime
            <select value={mode} onChange={e => set('regime_mode', Number(e.target.value))}
              data-testid="regime-mode-select">
              {[3, 5, 9].map(m => <option key={m} value={m}>{MODE_LABELS[m]}</option>)}
            </select>
          </label>
        )}
        {engine === 'v2' && (
          <label className="opt-field" title="Fenster/Glättung automatisch aus dem analysierten Zeitraum ableiten. 'auto' prüft alle Profile und nimmt das bestbewertete.">
            Glättung
            <select value={val('adapt_profile') || 'auto'}
              onChange={e => set('adapt_profile', e.target.value)}
              data-testid="regime-adapt-profile-select">
              <option value="auto">auto (bestes Profil wird ermittelt)</option>
              {profiles.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
              <option value="off">aus (feste Werte)</option>
            </select>
          </label>
        )}
        {engine === 'v2' && (
          <button className="opt-chip" onClick={() => setOpen(!open)} data-testid="regime-engine-toggle">
            {open ? <CaretDown size={11} /> : <CaretRight size={11} />} Engine-Feineinstellungen
          </button>
        )}
        {engine === 'v2' && Object.keys(config || {}).length > 0 && (
          <button className="opt-chip" onClick={() => setConfig({})} data-testid="regime-engine-reset">
            <ArrowCounterClockwise size={11} /> Standard
          </button>
        )}
        {engine === 'v2' && (
          <span className="opt-small" data-testid="regime-engine-hint">
            {MODE_HINT[mode]} {adapted
              ? '· Fenster passen sich dem Zeitraum an'
              : '· feste Fenster (keine Anpassung)'} · kein Lookahead
          </span>
        )}
      </div>
      {engine === 'v2' && open && (
        <div data-testid="regime-engine-fields">
          {groups.map(g => {
            const inactive = g.detectors && !g.detectors.includes(detector);
            return (
              <div key={g.name} style={inactive ? { opacity: 0.45 } : undefined}
                data-testid={`engine-group-${g.name.replace(/[^a-zA-Z]+/g, '-').toLowerCase()}`}>
                <div className="opt-small" style={{ fontWeight: 700, margin: '8px 0 2px', letterSpacing: 0.4 }}>
                  {g.name.toUpperCase()}
                  {inactive ? ' · WIRD VOM GEWÄHLTEN ERKENNUNGS-PRINZIP NICHT GENUTZT' : ''}
                </div>
                <div className="rl-engine-grid">{g.items.map(field)}</div>
              </div>
            );
          })}
          {!meta.length && <span className="opt-small">Lade Engine-Einstellungen…</span>}
        </div>
      )}
      {engine === 'v2' && calibrateCtx && (
        <div className="opt-setup" style={{ alignItems: 'center', marginTop: 6 }}>
          <label className="opt-field"
            title="Referenz-Regime, an der die Einstellungen gemessen werden: zentriert = OLS-Regression mit Zukunftssicht (sieht was das Auge sieht) · HMM = Markov-Switching-Modell, lernt Regime selbst · Abstimmung = beide kombiniert">
            Referenz
            <select value={truthSource} onChange={e => setTruthSource(e.target.value)}
              data-testid="regime-truth-source">
              <option value="centered">Rückblick (zentrierte Regression)</option>
              <option value="hmm">HMM (Markov-Switching, lernt selbst)</option>
              <option value="vote">Beide (Abstimmung)</option>
            </select>
          </label>
          <button className="opt-chip" onClick={startCalibrate} disabled={!!calJob}
            data-testid="regime-calibrate-btn"
            title="Sucht die Engine-Parameter, mit denen die (streng rückblickende) Live-Erkennung der Referenz am nächsten kommt: balancierte Richtungs-Trefferquote minus Strafen für zu viele Wechsel, Verzögerung und verpasste Phasen. Ergebnis wird direkt in die Einstellungen übernommen.">
            <Flask size={11} /> Wissenschaftlich kalibrieren
          </button>
          {calJob && (
            <span className="opt-small" data-testid="regime-calibrate-status">
              {calJob.phase} · {calJob.progress ?? 0}%
              <button className="opt-chip" onClick={cancelCalibrate}
                data-testid="regime-calibrate-cancel" style={{ marginLeft: 6 }}>Abbrechen</button>
            </span>
          )}
          {calErr && <span className="opt-small" style={{ color: '#e66' }}
            data-testid="regime-calibrate-error">{calErr}</span>}
          {calReport && (
            <span className="opt-small" data-testid="regime-calibrate-report"
              title={`Referenz: ${calReport.truth_source} · ${calReport.evals} Kandidaten geprüft · Ziel: ${calReport.objective}`}>
              ✓ Kalibriert ({calReport.total_days} Tage): Richtungs-Treffer{' '}
              <b>{calReport.baseline?.balanced_direction_pct}%</b> → <b>{calReport.best?.balanced_direction_pct}%</b>
              {' '}· Wechsel {calReport.best?.switches_live}/{calReport.best?.switches_truth} (Referenz)
              {calReport.best?.mean_lag_days !== null && calReport.best?.mean_lag_days !== undefined
                ? <> · Ø Erkennungs-Verzögerung {calReport.best.mean_lag_days}d</> : null}
              {' '}– Einstellungen übernommen
            </span>
          )}
        </div>
      )}
    </div>
  );
}
