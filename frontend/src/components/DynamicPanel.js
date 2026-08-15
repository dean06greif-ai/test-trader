import React, { useState, useEffect, useCallback } from 'react';
import { ArrowsClockwise, CheckCircle, Trash, CaretDown, CaretRight } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { regimeColor } from '../lib/regimeColors';
import { authHeaders, isAdmin } from '../auth';
import useInstruments, { assetLabel } from '../hooks/useInstruments';
import { fmtDate, fmtDateTime } from '../lib/time';

const API_URL = process.env.REACT_APP_BACKEND_URL;
const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

const TFS = ['5m', '15m', '30m', '1h', '4h'];
const NNFX_LABELS = { trend: 'NNFX: Trend', range: 'NNFX: Seitwärts', breakout: 'NNFX: Breakout' };

function LiveRegime() {
  const { symbols: COINS } = useInstruments();
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [tf, setTf] = useState('5m');
  const [days, setDays] = useState(90);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const run = async () => {
    setLoading(true);
    try {
      const r = await fetch(`${API_URL}/api/dynamic/current-regime?symbol=${symbol}&timeframe=${tf}&days=${days}`);
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Marktphase konnte nicht bestimmt werden'); return; }
      setData(d);
    } catch { toast.error('Verbindungsfehler'); }
    finally { setLoading(false); }
  };

  const cur = data?.current;
  return (
    <div className="dyn-card" data-testid="live-regime-card">
      <div className="dyn-card-head" style={{ flexWrap: 'wrap' }}>
        <b>Aktuelle Marktphase</b>
        <select value={symbol} onChange={e => setSymbol(e.target.value)} data-testid="live-regime-symbol">
          {COINS.map(c => <option key={c} value={c}>{assetLabel(c)}</option>)}
        </select>
        <select value={tf} onChange={e => setTf(e.target.value)} data-testid="live-regime-tf">
          {TFS.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={days} onChange={e => setDays(parseInt(e.target.value))} data-testid="live-regime-days">
          {[30, 60, 90, 180, 365].map(d => <option key={d} value={d}>{`${d} Tage`}</option>)}
        </select>
        <button className="opt-chip" onClick={run} disabled={loading} data-testid="live-regime-run">
          <ArrowsClockwise size={12} /> {loading ? 'Analysiere...' : 'Marktphase bestimmen'}
        </button>
      </div>
      {!data && (
        <div className="opt-small">
          Klärt sofort die Frage „In welcher Marktphase sind wir gerade?" – die Phasen werden aus dem
          gewählten Zeitraum frisch bestimmt (Multi-Timeframe-Regression, ADX/DI, Volatilität ·
          rein rückblickend, ohne Blick in die Zukunft) und zusätzlich auf die drei NNFX-Regime gemappt.
        </div>
      )}
      {data && (
        <div className="dyn-regime-state" data-testid="live-regime-result">
          <div>
            <b>{data.symbol.replace('USDT', '')}</b> · Jetzt: <b>{cur?.label || '–'}</b>
            {cur?.nnfx && <span className={`rl-nnfx-tag ${cur.nnfx}`} style={{ marginLeft: 6 }}>{NNFX_LABELS[cur.nnfx]}</span>}
            {' '}· Sicherheit: <b className={(cur?.confidence || 0) >= 70 ? 'pos' : ''}>{fmt(cur?.confidence, 0)}%</b>
            {cur?.strength && <span className="opt-small"> · Stärke {cur.strength}</span>}
            {cur?.last_switch && <span className="opt-small"> · seit {fmtDate(cur.last_switch)}</span>}
            <span className="opt-small"> · {data.switches} Phasenwechsel im Zeitraum
              {data.engine === 'v2'
                ? ' · Engine v2 (Regression + ADX + Vola)'
                : ` · Cluster-Qualität ${fmt(data.silhouette, 2)}`}</span>
          </div>
          {cur?.reason && <div className="opt-small" style={{ color: '#8A8FA3' }}>{cur.reason}</div>}
          {cur?.early_warning?.active && (
            <div className="rl-warn" data-testid="live-regime-warning" title={cur.early_warning.reason}>
              Frühwarnung: Wechsel → <b>{cur.early_warning.next_label}</b> ·
              {' '}Wahrscheinlichkeit <b>{fmt(cur.early_warning.probability_pct, 0)}%</b>
              {cur.early_warning.eta_days !== null && cur.early_warning.eta_days !== undefined
                ? <> · Schwelle in ca. <b>{cur.early_warning.eta_days}</b> Tagen</> : null}
              {cur.early_warning.pending
                ? <> · Kandidat hält seit <b>{cur.early_warning.pending_days}</b> von {cur.early_warning.confirm_days} Tagen</>
                : null}
              {cur.early_warning.hold_remaining_days > 0
                ? <> · Mindesthaltedauer noch <b>{cur.early_warning.hold_remaining_days}</b> Tage</>
                : null}
            </div>
          )}
          {data.validation && (
            <div className="opt-small" data-testid="live-regime-validation">
              Prüfung: {data.validation.passed ? <span className="pos">plausibel</span> : <span style={{ color: '#FFB74D' }}>Auffälligkeiten</span>}
              {' '}· unplausible Bars <b>{fmt(data.validation.violation_bars_pct, 2)}%</b>
              {data.validation.direction_accuracy_pct !== null && <> · Richtung korrekt <b>{fmt(data.validation.direction_accuracy_pct, 1)}%</b></>}
              {' '}· Ø Abschnitt <b>{fmt(data.validation.avg_segment_days, 1)}</b> Tage
            </div>
          )}
          <div className="opt-params-list" style={{ margin: '4px 0' }}>
            {(data.regimes || []).map(r => (
              <span key={r.id} className="opt-param-pill"
                title={`Farbe: Richtung (rot/gelb/grün) · Intensität: Volatilität`}
                style={r.id === cur?.regime
                  ? { borderColor: regimeColor(r.id, data.regimes), color: regimeColor(r.id, data.regimes) }
                  : { borderColor: `${regimeColor(r.id, data.regimes)}55` }}>
                #{r.id + 1} {r.label} · <b>{fmt(r.share_pct, 0)}%</b>
                {r.nnfx ? ` · ${NNFX_LABELS[r.nnfx]}` : ''}
              </span>
            ))}
          </div>
          <div className="opt-small">
            Letzte Phasen: {(data.timeline || []).slice(-6).map(t =>
              `${fmtDate(t.from)} ${t.label}`).join('  →  ')}
          </div>
        </div>
      )}
    </div>
  );
}

export default function DynamicPanel() {
  const [open, setOpen] = useState(false);
  const [list, setList] = useState(null);
  const [busy, setBusy] = useState({});
  const [refreshDays, setRefreshDays] = useState(30);
  const [logs, setLogs] = useState({});

  const loadLog = async (id) => {
    if (logs[id]) { setLogs(l => ({ ...l, [id]: null })); return; }
    try {
      const r = await fetch(`${API_URL}/api/dynamic/${id}/log`);
      const d = await r.json();
      setLogs(l => ({ ...l, [id]: d.log || [] }));
    } catch { toast.error('Protokoll konnte nicht geladen werden'); }
  };

  const saveSettings = async (id, patch) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    try {
      const r = await fetch(`${API_URL}/api/dynamic/${id}/settings`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(patch),
      });
      if (!r.ok) { toast.error('Einstellung fehlgeschlagen'); return; }
      load();
    } catch { toast.error('Verbindungsfehler'); }
  };

  const load = useCallback(() => {
    fetch(`${API_URL}/api/dynamic/list`).then(r => r.json())
      .then(d => setList(d.strategies || [])).catch(() => setList([]));
  }, []);

  useEffect(() => { if (open) load(); }, [open, load]);

  const refresh = async (id) => {
    setBusy(b => ({ ...b, [id]: 'refresh' }));
    try {
      const r = await fetch(`${API_URL}/api/dynamic/${id}/refresh`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: refreshDays }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Aktualisierung fehlgeschlagen'); return; }
      toast.success('Regime aktualisiert');
      load();
    } catch { toast.error('Verbindungsfehler'); }
    finally { setBusy(b => ({ ...b, [id]: null })); }
  };

  const apply = async (id) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setBusy(b => ({ ...b, [id]: 'apply' }));
    try {
      const r = await fetch(`${API_URL}/api/dynamic/${id}/apply`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Übernahme fehlgeschlagen'); return; }
      toast.success(`Aktive Regime-Konfiguration übernommen (${(d.applied || []).length} Coins)`);
      load();
    } catch { toast.error('Verbindungsfehler'); }
    finally { setBusy(b => ({ ...b, [id]: null })); }
  };

  const confirmSwitch = async (id, action) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setBusy(b => ({ ...b, [id]: action }));
    try {
      const r = await fetch(`${API_URL}/api/dynamic/${id}/${action}`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Aktion fehlgeschlagen'); return; }
      toast.success(action === 'confirm' ? 'Regime-Wechsel übernommen' : 'Vorschlag verworfen');
      load();
    } catch { toast.error('Verbindungsfehler'); }
    finally { setBusy(b => ({ ...b, [id]: null })); }
  };

  const remove = async (id) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    try {
      const r = await fetch(`${API_URL}/api/dynamic/${id}`, { method: 'DELETE', headers: authHeaders() });
      if (!r.ok) { toast.error('Löschen fehlgeschlagen'); return; }
      toast.success('Dynamische Strategie gelöscht');
      load();
    } catch { toast.error('Verbindungsfehler'); }
  };

  return (
    <div className="dyn-panel" data-testid="dyn-panel">
      <button className={`opt-chip opt-history-btn ${open ? 'on' : ''}`}
        onClick={() => setOpen(!open)} data-testid="dyn-panel-toggle"
        title="Gespeicherte dynamische Strategien: aktuelles Regime prüfen und die passende Konfiguration übernehmen">
        {open ? <CaretDown size={13} /> : <CaretRight size={13} />} Dynamische Strategien
      </button>
      {open && (
        <div className="opt-history" data-testid="dyn-panel-body">
          <LiveRegime />
          <div className="opt-small" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
            Analyse-Zeitraum für die Regime-Bestimmung:
            <select value={refreshDays} onChange={e => setRefreshDays(parseInt(e.target.value))} data-testid="dyn-refresh-days">
              {[10, 20, 30, 60, 90].map(d => <option key={d} value={d}>{`${d} Tage`}</option>)}
            </select>
          </div>
          {list === null && <div className="opt-small">Lade...</div>}
          {list !== null && list.length === 0 && (
            <div className="opt-small">Noch keine dynamischen Strategien – Modus "Dynamische Strategie" starten und Ergebnis speichern.</div>
          )}
          {(list || []).map(s => {
            const per = (s.last_state || {}).per_symbol || {};
            return (
              <div key={s.id} className="dyn-card" data-testid={`dyn-card-${s.id}`}>
                <div className="dyn-card-head">
                  <b>{s.name}</b>
                  {s.framework === 'nnfx' && (
                    <span className="rl-nnfx-tag trend" title="NNFX-Framework: pro Regime eine der drei NNFX-Strategien">NNFX</span>
                  )}
                  <span className="opt-small">
                    {s.regime_strategies
                      ? `Strategien: ${[...new Set(Object.values(s.regime_strategies))].join(', ')}`
                      : `Basis: ${s.strategy_id}`} · {s.timeframe} · {(s.symbols || []).map(x => x.replace('USDT', '')).join(', ')}
                  </span>
                  <span className="opt-small">{(s.model?.regimes || []).length} Regime</span>
                  {s.verdict?.dynamic_better === false && <span className="opt-badge bad" title="Beim Erstellen war die statische Benchmark besser">Benchmark war besser</span>}
                  <span style={{ flex: 1 }} />
                  <button className="opt-chip" onClick={() => refresh(s.id)} disabled={!!busy[s.id]} data-testid={`dyn-refresh-${s.id}`}>
                    <ArrowsClockwise size={12} /> {busy[s.id] === 'refresh' ? 'Prüfe...' : 'Regime aktualisieren'}
                  </button>
                  <button className="opt-chip" onClick={() => apply(s.id)} disabled={!!busy[s.id] || !Object.keys(per).length} data-testid={`dyn-apply-${s.id}`}
                    title="Aktive Regime-Konfiguration je Coin als Live/Paper-Override übernehmen">
                    <CheckCircle size={12} /> Konfiguration übernehmen
                  </button>
                  <button className="opt-chip" onClick={() => loadLog(s.id)} data-testid={`dyn-log-${s.id}`}
                    title="Wechsel-Protokoll: alle Regime-Wechsel mit Datum, Sicherheit und Begründung">
                    Protokoll
                  </button>
                  <button className="opt-chip" onClick={() => remove(s.id)} data-testid={`dyn-delete-${s.id}`}>
                    <Trash size={12} />
                  </button>
                </div>
                <div className="opt-small" style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginTop: 4 }}>
                  <label style={{ display: 'flex', gap: 4, alignItems: 'center', cursor: 'pointer' }}
                    title="Prüft das Regime automatisch im Hintergrund und schreibt Wechsel ins Protokoll">
                    <input type="checkbox" checked={!!s.settings?.auto_check_enabled}
                      onChange={e => saveSettings(s.id, { auto_check_enabled: e.target.checked })}
                      data-testid={`dyn-auto-check-${s.id}`} />
                    Auto-Prüfung
                  </label>
                  <label style={{ display: 'flex', gap: 4, alignItems: 'center', cursor: 'pointer' }}
                    title="Wechsel werden nicht automatisch übernommen, sondern als Vorschlag angezeigt und müssen bestätigt werden">
                    <input type="checkbox" checked={!!s.settings?.require_confirmation}
                      onChange={e => saveSettings(s.id, { require_confirmation: e.target.checked })}
                      data-testid={`dyn-require-confirm-${s.id}`} />
                    nur mit manueller Bestätigung
                  </label>
                  <label style={{ display: 'flex', gap: 4, alignItems: 'center', cursor: 'pointer' }}
                    title="Übergangsschutz: nach einem Regime-Wechsel greift eine Schutzregel gegen Flackern an den Übergängen (Standard: neue Trades gesperrt, offene laufen mit ihren Stops weiter)">
                    <input type="checkbox" checked={s.settings?.transition_protection_enabled !== false}
                      onChange={e => saveSettings(s.id, { transition_protection_enabled: e.target.checked })}
                      data-testid={`dyn-transition-${s.id}`} />
                    Übergangsschutz
                  </label>
                  {s.settings?.transition_protection_enabled !== false && (
                    <>
                      <select value={s.settings?.transition_mode || 'block_new'}
                        onChange={e => saveSettings(s.id, { transition_mode: e.target.value })}
                        data-testid={`dyn-transition-mode-${s.id}`}
                        title="block_new = nur neue Trades sperren, offene laufen mit ihren Stops weiter (realistischer fürs Live-Trading) · close_open = offene Trades der betroffenen Coins zusätzlich kontrolliert schließen">
                        <option value="block_new">neue Trades sperren (offene laufen weiter)</option>
                        <option value="close_open">offene Trades schließen + sperren</option>
                      </select>
                      <label style={{ display: 'flex', gap: 4, alignItems: 'center' }}
                        title="So lange sind nach einem Regime-Wechsel neue Trades auf den betroffenen Coins gesperrt">
                        Sperre
                        <input type="number" min={0} max={30} step={0.25}
                          value={s.settings?.transition_lock_days ?? 0.5}
                          onChange={e => saveSettings(s.id, { transition_lock_days: parseFloat(e.target.value) || 0 })}
                          style={{ width: 55 }} data-testid={`dyn-transition-lock-${s.id}`} />
                        Tage
                      </label>
                    </>
                  )}
                  {s.settings?.auto_check_enabled && (
                    <>
                      <label style={{ display: 'flex', gap: 4, alignItems: 'center' }}>alle
                        <select value={s.settings?.check_interval_minutes || 60}
                          onChange={e => saveSettings(s.id, { check_interval_minutes: parseInt(e.target.value) })}
                          data-testid={`dyn-interval-${s.id}`}>
                          {[15, 30, 60, 180, 360, 720, 1440].map(m =>
                            <option key={m} value={m}>{m >= 60 ? `${m / 60} h` : `${m} min`}</option>)}
                        </select>
                      </label>
                      <label style={{ display: 'flex', gap: 4, alignItems: 'center', cursor: 'pointer' }}
                        title="Bei einem erkannten Regime-Wechsel wird die passende Konfiguration automatisch als Coin-Override übernommen">
                        <input type="checkbox" checked={!!s.settings?.auto_apply_enabled}
                          onChange={e => saveSettings(s.id, { auto_apply_enabled: e.target.checked })}
                          data-testid={`dyn-auto-apply-${s.id}`} />
                        Auto-Übernahme bei Wechsel
                      </label>
                    </>
                  )}
                </div>
                {s.pending_switch && (
                  <div className="dyn-pending" data-testid={`dyn-pending-${s.id}`}>
                    <b>Regime-Wechsel erkannt – Bestätigung nötig</b>
                    <span className="opt-small">
                      {fmtDateTime(s.pending_switch.at)} ·
                      {' '}{Object.entries(s.pending_switch.per_symbol || {}).map(([sym, v]) =>
                        `${sym.replace('USDT', '')}: ${v.label} (${fmt(v.confidence, 0)}%)`).join(' · ')}
                    </span>
                    <span style={{ flex: 1 }} />
                    <button className="opt-chip" onClick={() => confirmSwitch(s.id, 'confirm')}
                      disabled={!!busy[s.id]} data-testid={`dyn-confirm-${s.id}`}>
                      <CheckCircle size={12} /> Übernehmen
                    </button>
                    <button className="opt-chip" onClick={() => confirmSwitch(s.id, 'dismiss')}
                      disabled={!!busy[s.id]} data-testid={`dyn-dismiss-${s.id}`}>
                      Verwerfen
                    </button>
                  </div>
                )}
                {logs[s.id] && (
                  <div className="dyn-regime-state" data-testid={`dyn-log-body-${s.id}`}>
                    <b>Wechsel-Protokoll</b>
                    {logs[s.id].length === 0 && <div className="opt-small">Noch keine Regime-Wechsel protokolliert.</div>}
                    {logs[s.id].slice(0, 20).map(e => (
                      <div key={e.id} className="opt-small" style={{ margin: '3px 0' }}>
                        {fmtDateTime(e.at)} · <b>{e.symbol?.replace('USDT', '')}</b>:
                        {' '}{e.from_label || `Regime ${e.from_regime}`} → <b>{e.to_label || `Regime ${e.to_regime}`}</b>
                        {' '}· Sicherheit {fmt(e.confidence, 0)}%
                        {e.auto_applied && <span className="pos"> · automatisch übernommen</span>}
                        <div style={{ color: '#8A8FA3' }}>{e.reason}</div>
                      </div>
                    ))}
                  </div>
                )}
                {s.last_applied && <div className="opt-small">Zuletzt übernommen: {fmtDateTime(s.last_applied)}</div>}
                {Object.entries(per).map(([sym, st]) => st.error ? (
                  <div key={sym} className="opt-small neg">{sym}: {st.error}</div>
                ) : (
                  <div key={sym} className="dyn-regime-state" data-testid={`dyn-state-${s.id}-${sym}`}>
                    <div>
                      <b>{sym.replace('USDT', '')}</b> · Aktuelles Regime: <b>{st.label || '–'}</b>
                      {st.nnfx && <span className={`rl-nnfx-tag ${st.nnfx}`} style={{ marginLeft: 6 }}>{NNFX_LABELS[st.nnfx]}</span>}
                      {s.regime_strategies?.[String(st.regime)] && (
                        <span className="opt-small"> · aktive Strategie: <b>{s.regime_strategies[String(st.regime)]}</b></span>
                      )}
                      {' '}· Sicherheit: <b className={st.confidence >= 70 ? 'pos' : ''}>{fmt(st.confidence, 0)}%</b>
                      {st.confidence < 70 && <span className="opt-small" style={{ color: '#FFB74D' }}> (unter Schwelle – aktuelle Konfiguration behalten)</span>}
                      {st.last_switch && <span className="opt-small"> · letzter Wechsel: {fmtDate(st.last_switch)}</span>}
                    </div>
                    {st.reason && <div className="opt-small" style={{ color: '#8A8FA3' }}>{st.reason}</div>}
                    {st.early_warning?.active && (
                      <div className="rl-warn" data-testid={`dyn-warn-${s.id}-${sym}`}
                        title={st.early_warning.reason}>
                        Frühwarnung: Wechsel → <b>{st.early_warning.next_label}</b> ·
                        {' '}<b>{fmt(st.early_warning.probability_pct, 0)}%</b>
                        {st.early_warning.eta_days !== null && st.early_warning.eta_days !== undefined
                          ? <> · ca. <b>{st.early_warning.eta_days}</b> Tage</> : null}
                        {s.regime_strategies?.[String(st.early_warning.next_regime)]
                          ? <> · dann Strategie <b>{s.regime_strategies[String(st.early_warning.next_regime)]}</b></>
                          : null}
                      </div>
                    )}
                    {(st.similarities || []).length > 0 && (
                      <div className="opt-small">
                        Ähnlichkeiten: {(st.similarities || []).map(x => `${x.label} ${fmt(x.similarity_pct, 0)}%`).join(' · ')}
                      </div>
                    )}
                    <div className="opt-params-list" style={{ margin: '3px 0' }}>
                      <span className="opt-small" style={{ alignSelf: 'center' }}>Aktive Konfig:</span>
                      {Object.entries(st.active_config || {}).length
                        ? Object.entries(st.active_config).map(([k, v]) => <span key={k} className="opt-param-pill">{k}: <b>{String(v)}</b></span>)
                        : <span className="opt-param-pill">Baseline (aktuelle Einstellungen)</span>}
                    </div>
                    {(st.active_sub_strategy || []).length > 0 && (
                      <div className="opt-params-list" style={{ margin: '3px 0' }}
                        data-testid={`dyn-active-sub-${s.id}-${sym}`}>
                        <span className="opt-small" style={{ alignSelf: 'center' }}>Aktive Sub-Strategie:</span>
                        {st.active_sub_strategy.map((x, i) => <span key={i} className="opt-param-pill">{x}</span>)}
                      </div>
                    )}
                    {(st.recent_performance || []).length > 0 && (
                      <div className="opt-small" title="Nur zur Info – die Umschaltung basiert auf der Regime-Ähnlichkeit, NICHT auf der jüngsten Performance (Overfitting-Schutz)">
                        Vergleich letzte {(s.last_state || {}).days} Tage: {st.recent_performance.map(p =>
                          `${p.label}: ${fmt(p.pnl, 1)} (${p.trades} T.)`).join(' · ')}
                      </div>
                    )}
                  </div>
                ))}
                {!Object.keys(per).length && <div className="opt-small">Noch nicht geprüft – "Regime aktualisieren" klicken.</div>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
