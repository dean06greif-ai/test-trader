import React, { useState, useEffect, useRef, useCallback } from 'react';
import { X, Desktop, DownloadSimple, ArrowsClockwise, Trash, Copy, Key, Database, Gear, FloppyDisk } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import SafeOverlay from './SafeOverlay';
import './LocalWorkerPanel.css';
import { assetLabel } from '../hooks/useInstruments';
import { fmtDate, fmtShort } from '../lib/time';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmtBytes = (b) => {
  if (!b) return '0 MB';
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  return `${(b / 1e6).toFixed(1)} MB`;
};
const fmtTs = (ts) => fmtDate(ts);
const fmtSpan = (a, b) => (a && b ? `${fmtTs(a)} – ${fmtTs(b)}` : '–');

const DL_DAYS = [7, 14, 30, 60, 90, 180, 360, 540, 720, 1080, 1440, 1800, 2160, 2880, 3600, 4320, 5400];

export default function LocalWorkerPanel({ onClose }) {
  const [status, setStatus] = useState(null);
  const [settings, setSettings] = useState(null);
  const [token, setToken] = useState(null);
  const [showToken, setShowToken] = useState(false);
  const [coins, setCoins] = useState([]);
  const [dlCoins, setDlCoins] = useState([]);
  const [dlDays, setDlDays] = useState(90);
  const [saving, setSaving] = useState(false);
  const pollRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const d = await fetch(`${API_URL}/api/localworker/status`).then(r => r.json());
      setStatus(d);
      setSettings(prev => prev || d.settings);
    } catch { /* keep last */ }
  }, []);

  useEffect(() => {
    load();
    fetch(`${API_URL}/api/coins`).then(r => r.json())
      .then(d => setCoins(d.coins || [])).catch(() => {});
    if (isAdmin()) {
      fetch(`${API_URL}/api/localworker/token`, { headers: authHeaders() })
        .then(r => r.json()).then(d => setToken(d.token)).catch(() => {});
    }
    pollRef.current = setInterval(load, 3000);
    return () => clearInterval(pollRef.current);
  }, [load]);

  const worker = (status?.workers || []).find(w => w.online) || (status?.workers || [])[0];
  const online = !!status?.online;
  const res = worker?.resources || {};
  const data = worker?.data || {};
  const dataJob = status?.data_jobs?.active;
  const queuedData = status?.data_jobs?.queued || [];

  const saveSettings = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setSaving(true);
    try {
      const r = await fetch(`${API_URL}/api/localworker/settings`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(settings),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Speichern fehlgeschlagen'); return; }
      setSettings(d.settings);
      toast.success('Einstellungen gespeichert – Worker übernimmt sie automatisch');
    } catch { toast.error('Verbindungsfehler'); } finally { setSaving(false); }
  };

  const startDownload = async () => {
    if (!dlCoins.length) { toast.error('Mind. 1 Coin wählen'); return; }
    try {
      const r = await fetch(`${API_URL}/api/localworker/data/download`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ symbols: dlCoins, days: dlDays }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      toast.success(`Download gestartet: ${dlCoins.map(s => s.replace('USDT', '')).join(', ')} (${dlDays} Tage)`);
      load();
    } catch { toast.error('Verbindungsfehler'); }
  };

  const updateAll = async () => {
    try {
      const r = await fetch(`${API_URL}/api/localworker/data/update`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Start fehlgeschlagen'); return; }
      toast.success('Aktualisierung aller lokalen Daten gestartet');
      load();
    } catch { toast.error('Verbindungsfehler'); }
  };

  const deleteSymbol = async (sym) => {
    try {
      const r = await fetch(`${API_URL}/api/localworker/data/delete`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ symbol: sym }),
      });
      const d = await r.json();
      if (!r.ok) { toast.error(d.detail || 'Löschen fehlgeschlagen'); return; }
      toast.success(`${sym} wird gelöscht`);
      load();
    } catch { toast.error('Verbindungsfehler'); }
  };

  const cancelDataJob = async () => {
    if (!dataJob?.id) return;
    try {
      await fetch(`${API_URL}/api/localworker/data/cancel/${dataJob.id}`, {
        method: 'POST', headers: authHeaders(),
      });
      toast.info('Abbruch angefordert...');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const regenToken = async () => {
    try {
      const d = await fetch(`${API_URL}/api/localworker/token/regenerate`, {
        method: 'POST', headers: authHeaders(),
      }).then(r => r.json());
      setToken(d.token);
      toast.success('Neues Token erzeugt – Worker mit neuem Token neu starten');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const copy = (text, label) => {
    navigator.clipboard?.writeText(text).then(() => toast.success(`${label} kopiert`))
      .catch(() => toast.error('Kopieren fehlgeschlagen'));
  };

  const set = (k, v) => setSettings(prev => ({ ...prev, [k]: v }));

  return (
    <SafeOverlay className="lw-overlay" onClose={onClose}>
      <div className="lw-panel" onClick={e => e.stopPropagation()} data-testid="localworker-panel">
        <div className="lw-header">
          <h2><Desktop size={20} weight="bold" style={{ color: '#7CFFB2' }} /> LOKALE AUSFÜHRUNG</h2>
          <button className="lw-close" onClick={onClose} data-testid="lw-close"><X size={22} weight="bold" /></button>
        </div>

        {/* ---- Status ---- */}
        <div className="lw-status" data-testid="lw-status">
          <span className={`lw-dot ${online ? 'on' : ''}`} data-testid="lw-status-dot" />
          <span className="lw-status-name">
            {online ? `Worker verbunden: ${worker?.name || '–'}` : 'Kein Worker verbunden'}
          </span>
          {worker && (
            <span className="lw-status-meta" data-testid="lw-status-meta">
              {res.cores ? `${res.cores} Kerne` : ''}
              {worker.sim_workers ? ` · Multi-Core: ${worker.sim_workers} Prozesse` : ''}
              {res.ram_total_mb ? ` · RAM ${Math.round((res.ram_used_mb || 0) / 1000)}/${Math.round(res.ram_total_mb / 1000)} GB` : ''}
              {res.cpu_percent != null ? ` · CPU ${res.cpu_percent}%` : ''}
              {worker.gpu?.available
                ? ` · GPU: ${worker.gpu.name || 'erkannt'}${worker.gpu.enabled ? ' (aktiv)' : ' (aus)'}`
                : ' · GPU: –'}
              {worker.version ? ` · v${worker.version}` : ''}
            </span>
          )}
        </div>
        {worker && worker.outdated && (
          <div className="lw-hint" data-testid="lw-outdated-warning"
            style={{ borderLeft: '2px solid #ffb454', paddingLeft: 8, marginTop: 6 }}>
            <b>Worker veraltet (v{worker.version} · empfohlen v{status?.required_version}).</b>{' '}
            Der neue Kerzen-Speicher (spaltenbasiert, .npy) und die Fehlerbehandlung fehlen –
            große Zeiträume (5400 Tage) können abstürzen. Bitte Worker beenden, das
            aktuelle Paket herunterladen, über den alten Ordner entpacken und neu starten.
            Deine heruntergeladenen Kerzen bleiben erhalten und werden automatisch konvertiert.{' '}
            <a className="lw-btn inline" href={`${API_URL}/api/localworker/package`}
              data-testid="lw-package-download-update">
              <DownloadSimple size={13} weight="bold" /> Neues Paket laden
            </a>
          </div>
        )}

        {/* ---- Einrichtung ---- */}
        <details className="lw-section" open={!online} data-testid="lw-setup">
          <summary><Key size={14} weight="bold" /> Einrichtung (einmalig)</summary>
          <ol className="lw-steps">
            <li>
              Worker-Paket herunterladen und entpacken:{' '}
              <a className="lw-btn inline" href={`${API_URL}/api/localworker/package`}
                data-testid="lw-package-download">
                <DownloadSimple size={13} weight="bold" /> Worker herunterladen (Zip)
              </a>
            </li>
            <li>Python 3.10+ installieren, dann im Worker-Ordner: <code>pip install -r requirements.txt</code></li>
            <li>
              Worker starten:
              <div className="lw-cmd" data-testid="lw-cmd">
                <code>python worker.py --server {API_URL} --token {showToken ? (token || '…') : '••••••••'}</code>
                <button className="lw-mini" onClick={() => setShowToken(!showToken)} data-testid="lw-token-show">
                  {showToken ? 'verbergen' : 'Token zeigen'}
                </button>
                <button className="lw-mini" data-testid="lw-cmd-copy"
                  onClick={() => copy(`python worker.py --server ${API_URL} --token ${token || ''}`, 'Startbefehl')}>
                  <Copy size={12} weight="bold" /> kopieren
                </button>
              </div>
            </li>
          </ol>
          <div className="lw-hint">
            Der Worker verbindet sich selbst mit der Website – keine Portfreigaben nötig.
            Server &amp; Token werden gespeichert, danach reicht <code>python worker.py</code>.
            <button className="lw-mini danger" onClick={regenToken} data-testid="lw-token-regen"
              style={{ marginLeft: 8 }}>Token erneuern</button>
          </div>
        </details>

        {/* ---- Einstellungen ---- */}
        <div className="lw-section-title"><Gear size={14} weight="bold" /> EINSTELLUNGEN (werden automatisch an den Worker übertragen)</div>
        {settings && (
          <div className="lw-grid" data-testid="lw-settings">
            <label title="Anzahl der Prozesse für Multi-Core-Simulation (Backtest-Paare & Optimizer-Iterationen parallel). 0 = alle Kerne, 1 = sequenziell.">
              CPU-Kerne Multi-Core (0 = alle)
              <input type="number" min={0} max={128} value={settings.cpu_cores}
                onChange={e => set('cpu_cores', parseInt(e.target.value) || 0)}
                data-testid="lw-set-cores" />
            </label>
            <label>RAM-Limit Kerzen-Cache (MB)
              <input type="number" min={512} step={512} value={settings.ram_limit_mb}
                onChange={e => set('ram_limit_mb', parseInt(e.target.value) || 4096)}
                data-testid="lw-set-ram" />
            </label>
            <label>Max. parallele Jobs
              <input type="number" min={1} max={8} value={settings.max_parallel_jobs}
                onChange={e => set('max_parallel_jobs', parseInt(e.target.value) || 1)}
                data-testid="lw-set-parallel" />
            </label>
            <label title="GPU-Beschleunigung (NVIDIA/CuPy) für die Indikator-Vorberechnung (SMA, Bollinger, Stochastik). Automatische Erkennung mit CPU-Fallback – im Worker einmalig 'pip install cupy-cuda12x' ausführen.">
              GPU nutzen (NVIDIA/CuPy)
              <select value={settings.use_gpu ? '1' : '0'}
                onChange={e => set('use_gpu', e.target.value === '1')} data-testid="lw-set-gpu">
                <option value="0">Aus (CPU)</option>
                <option value="1">An – Auto-Erkennung, CPU-Fallback</option>
              </select>
            </label>
            <label>Daten-Ordner (leer = Standard des Workers)
              <input type="text" placeholder="z.B. D:/KryptoDaten" value={settings.data_dir}
                onChange={e => set('data_dir', e.target.value)} data-testid="lw-set-datadir" />
            </label>
            <label className="lw-check">
              <input type="checkbox" checked={!!settings.auto_update_enabled}
                onChange={e => set('auto_update_enabled', e.target.checked)}
                data-testid="lw-set-autoupdate" />
              Daten automatisch aktualisieren
            </label>
            {settings.auto_update_enabled && (
              <label>Update-Intervall (Minuten)
                <input type="number" min={5} max={1440} value={settings.auto_update_minutes}
                  onChange={e => set('auto_update_minutes', parseInt(e.target.value) || 60)}
                  data-testid="lw-set-autominutes" />
              </label>
            )}
            <button className="lw-btn save" onClick={saveSettings} disabled={saving}
              data-testid="lw-settings-save">
              <FloppyDisk size={14} weight="bold" /> {saving ? 'Speichert...' : 'Speichern'}
            </button>
          </div>
        )}

        {/* ---- Daten-Verwaltung ---- */}
        <div className="lw-section-title">
          <Database size={14} weight="bold" /> LOKALE MARKTDATEN
          <span className="lw-data-summary" data-testid="lw-data-summary">
            {data.dir ? ` ${data.dir} · ${fmtBytes(data.total_bytes)} belegt · ${data.disk_free_gb ?? '–'} GB frei` : ' (Worker offline – keine Daten-Info)'}
          </span>
        </div>
        <div className="lw-hint" style={{ marginTop: -4 }}>
          Gespeichert werden 1-Minuten-Kerzen – alle Timeframes werden daraus berechnet,
          ein Download deckt also alle Timeframes ab. Backtester, Optimizer &amp; Discovery
          nutzen vorhandene Daten automatisch (nur die neuesten Minuten werden nachgeladen).
        </div>

        {(dataJob || queuedData.length > 0) && (
          <div className="lw-datajob" data-testid="lw-datajob">
            {dataJob && (
              <>
                <div className="lw-progress-bar"><div style={{ width: `${dataJob.progress || 0}%` }} /></div>
                <div className="lw-datajob-row">
                  <span data-testid="lw-datajob-text">{dataJob.phase} · {dataJob.progress || 0}%</span>
                  <button className="lw-mini danger" onClick={cancelDataJob} data-testid="lw-datajob-cancel">
                    <X size={12} weight="bold" /> Abbrechen
                  </button>
                </div>
              </>
            )}
            {queuedData.length > 0 && (
              <div className="lw-hint">Warteschlange: {queuedData.length} Daten-Job(s)</div>
            )}
          </div>
        )}

        <div className="lw-download" data-testid="lw-download">
          <div className="lw-chips">
            {coins.map(c => (
              <button key={c} className={`lw-chip ${dlCoins.includes(c) ? 'on' : ''}`}
                onClick={() => setDlCoins(dlCoins.includes(c) ? dlCoins.filter(x => x !== c) : [...dlCoins, c])}
                data-testid={`lw-dl-coin-${c}`}>
                {assetLabel(c)}
              </button>
            ))}
            <button className="lw-chip" onClick={() => setDlCoins(dlCoins.length === coins.length ? [] : [...coins])}
              data-testid="lw-dl-all">{dlCoins.length === coins.length ? 'Keine' : 'Alle'}</button>
          </div>
          <div className="lw-download-row">
            <label>Zeitraum
              <select value={dlDays} onChange={e => setDlDays(parseInt(e.target.value))} data-testid="lw-dl-days">
                {DL_DAYS.map(d => <option key={d} value={d}>{d} Tage</option>)}
              </select>
            </label>
            <button className="lw-btn" onClick={startDownload} disabled={!online || !!dataJob}
              data-testid="lw-dl-start">
              <DownloadSimple size={14} weight="bold" /> Herunterladen
            </button>
            <button className="lw-btn ghost" onClick={updateAll}
              disabled={!online || !!dataJob || !(data.symbols || []).length}
              data-testid="lw-dl-update-all">
              <ArrowsClockwise size={14} weight="bold" /> Alle aktualisieren
            </button>
          </div>
        </div>

        <table className="lw-table" data-testid="lw-data-table">
          <thead>
            <tr><th>Coin</th><th>Kerzen (1m)</th><th>Zeitraum</th><th>Größe</th><th>Aktualisiert</th><th /></tr>
          </thead>
          <tbody>
            {(data.symbols || []).length === 0 && (
              <tr><td colSpan={6} className="lw-empty" data-testid="lw-data-empty">
                {online ? 'Noch keine lokalen Daten – oben Coins wählen und herunterladen.' : 'Worker offline'}
              </td></tr>
            )}
            {(data.symbols || []).map(s => (
              <tr key={s.symbol} data-testid={`lw-data-row-${s.symbol}`}>
                <td className="mono">{s.symbol}</td>
                <td>{s.candles ? s.candles.toLocaleString('de-DE') : '–'}</td>
                <td>{fmtSpan(s.first_ts, s.last_ts)}</td>
                <td>{fmtBytes(s.bytes)}</td>
                <td>{fmtShort(s.updated)}</td>
                <td>
                  <button className="lw-mini danger" onClick={() => deleteSymbol(s.symbol)}
                    disabled={!online || !!dataJob} title="Lokale Daten löschen"
                    data-testid={`lw-data-delete-${s.symbol}`}>
                    <Trash size={12} weight="bold" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </SafeOverlay>
  );
}
