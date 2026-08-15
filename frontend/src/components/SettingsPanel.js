import React, { useState, useEffect } from 'react';
import SafeOverlay from './SafeOverlay';
import { X, TelegramLogo, Lightning, ChartLineUp, Plus, Trash, Sliders, PauseCircle, PlayCircle, Power, ArrowsClockwise } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import useInstruments, { assetLabel } from '../hooks/useInstruments';
import TIMEFRAMES, { RULE_TIMEFRAMES, TF_MINUTES } from '../constants/timeframes';
import './SettingsPanel.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const SettingsPanel = ({ onClose, focusStrategy, mode = 'all', controlState, onControlChanged }) => {
  // mode: 'all' = alle Tabs, 'general' = Steuerung + Telegram, 'strategy' = Strategie + Zeitfenster
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const defaultTab = mode === 'general' ? 'control' : 'strategy';
  const [activeTab, setActiveTab] = useState(defaultTab); // strategy, sessions, control, telegram
  const [settings, setSettings] = useState({
    custom_sessions: [],
    pre_signal_enabled: true,
    active_strategy: 'scalping_4_rules',
    strategy_params: {},
    coin_params: {},
    strategy_timeframes: {},
  });
  const [strategies, setStrategies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [paramCoin, setParamCoin] = useState(''); // '' = Global, else per-coin override
  const [sessionScope, setSessionScope] = useState('global'); // 'global' or strategy_id
  const [busy, setBusy] = useState(false);
  const [defDirty, setDefDirty] = useState(false);
  const importParamsRef = React.useRef(null);
  const { symbols: ALL_COINS } = useInstruments();
  const [notifyCfg, setNotifyCfg] = useState(null);
  const [guard, setGuard] = useState(null);
  const [syncStatus, setSyncStatus] = useState(null);
  const [syncBusy, setSyncBusy] = useState(false);
  const [watchdogStatus, setWatchdogStatus] = useState(null);
  const [watchdogBusy, setWatchdogBusy] = useState(false);

  const NOTIFY_LABELS = {
    ai_failure: 'KI-Ausfall (Primär + Backup gescheitert, Fallback übernimmt)',
    backtest_done: 'Backtest fertig',
    optimizer_done: 'Optimizer fertig',
    trade_opened: 'Trade eröffnet',
    trade_closed: 'Trade geschlossen (SL/TP/manuell)',
    kill_switch: 'Kill-Switch ausgelöst',
    watchdog: 'Positions-Watchdog (fehlender SL, übernommene Positionen)',
    daily_summary: 'Tägliche Zusammenfassung (Mitternacht)',
    token_alert: 'Token-Kosten-Wächter (ungewöhnlich hoher Verbrauch einer KI-Rolle)',
    website_ai_failure: 'KI-Ausfall zusätzlich als Website-Meldung',
  };

  useEffect(() => {
    fetch(`${API_URL}/api/telegram/notify-config`).then(r => r.json()).then(setNotifyCfg).catch(() => {});
    const loadGuard = () => {
      fetch(`${API_URL}/api/trade-guard`).then(r => r.json()).then(setGuard).catch(() => {});
      fetch(`${API_URL}/api/autotrade/sync-status`).then(r => r.json()).then(setSyncStatus).catch(() => {});
      fetch(`${API_URL}/api/autotrade/watchdog/status`).then(r => r.json()).then(setWatchdogStatus).catch(() => {});
    };
    loadGuard();
    const iv = setInterval(loadGuard, 30000);
    return () => clearInterval(iv);
  }, []);

  const runBitunixSync = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setSyncBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/autotrade/sync-bitunix`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || 'Abgleich fehlgeschlagen');
      if (d.status === 'skipped') toast.info(d.detail);
      else toast.success(d.synced
        ? `Abgleich fertig – ${d.synced} extern geschlossene Position(en) übernommen`
        : 'Abgleich fertig – alle Live-Positionen stimmen überein');
      fetch(`${API_URL}/api/autotrade/sync-status`).then(r => r.json()).then(setSyncStatus).catch(() => {});
    } catch (e) { toast.error(e.message); } finally { setSyncBusy(false); }
  };

  const runWatchdog = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setWatchdogBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/autotrade/watchdog/run`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || 'Watchdog-Lauf fehlgeschlagen');
      if (d.status === 'skipped') toast.info(d.detail);
      else {
        const r = d.result || {};
        toast.success(`Watchdog fertig – ${r.positions || 0} Position(en) geprüft` +
          `${r.sl_fixed ? `, ${r.sl_fixed} SL nachgezogen` : ''}` +
          `${r.adopted ? `, ${r.adopted} übernommen` : ''}`);
      }
      fetch(`${API_URL}/api/autotrade/watchdog/status`).then(r => r.json()).then(setWatchdogStatus).catch(() => {});
    } catch (e) { toast.error(e.message); } finally { setWatchdogBusy(false); }
  };

  const setWatchdogManageExternal = async (val) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    try {
      const res = await fetch(`${API_URL}/api/autotrade/watchdog/config`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ manage_external: val }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || 'Speichern fehlgeschlagen');
      setWatchdogStatus(s => ({ ...(s || {}), settings: d.settings }));
      toast.success(val
        ? 'Watchdog verwaltet externe Positionen jetzt aktiv (SL-Zwang & Aufräumen)'
        : 'Externe Positionen werden nur beobachtet, nicht angefasst');
    } catch (e) { toast.error(`Watchdog-Einstellung: ${e.message}`); }
  };

  const setWatchdogEnabled = async (val) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    try {
      const res = await fetch(`${API_URL}/api/autotrade/watchdog/config`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ enabled: val }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || 'Speichern fehlgeschlagen');
      setWatchdogStatus(s => ({ ...(s || {}), settings: d.settings }));
      toast.success(val
        ? 'Watchdog eingeschaltet – Überwachung läuft wieder'
        : 'Watchdog komplett ausgeschaltet – keine Überwachung, keine Eingriffe');
    } catch (e) { toast.error(`Watchdog-Einstellung: ${e.message}`); }
  };

  const clearWatchdogData = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (!window.confirm('Watchdog-Verlauf wirklich löschen? Entfernt die Lauf-Statistik '
      + 'und alle als „Manuell (Bitunix)" übernommenen Trades. Das kann nicht rückgängig gemacht werden.')) return;
    try {
      const res = await fetch(`${API_URL}/api/autotrade/watchdog/clear`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || 'Löschen fehlgeschlagen');
      toast.success(`Watchdog-Verlauf gelöscht (${d.deleted_trades || 0} Extern-Trade(s) entfernt)`);
      fetch(`${API_URL}/api/autotrade/watchdog/status`).then(r => r.json()).then(setWatchdogStatus).catch(() => {});
    } catch (e) { toast.error(e.message); }
  };

  const saveNotify = async (key, val) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setNotifyCfg(c => ({ ...c, [key]: val }));
    const res = await fetch(`${API_URL}/api/telegram/notify-config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ [key]: val }),
    });
    if (!res.ok) toast.error('Speichern fehlgeschlagen');
  };

  const saveGuard = async (patch) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    setGuard(g => ({ ...g, config: { ...g.config, ...patch } }));
    const res = await fetch(`${API_URL}/api/trade-guard/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(patch),
    });
    if (res.ok) setGuard(await res.json());
    else toast.error('Speichern fehlgeschlagen');
  };

  const resumeGuard = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const res = await fetch(`${API_URL}/api/trade-guard/resume`, { method: 'POST', headers: authHeaders() });
    if (res.ok) {
      const d = await res.json();
      setGuard(g => ({ ...g, state: d.state }));
      toast.success('Kill-Switch aufgehoben – Auto-Trading wieder aktiv');
    }
  };

  useEffect(() => {
    Promise.all([
      fetch(`${API_URL}/api/settings`).then(r => r.json()),
      fetch(`${API_URL}/api/strategies`).then(r => r.json())
    ])
      .then(([settingsData, strategiesData]) => {
        setSettings({
          custom_sessions: settingsData.custom_sessions || [],
          strategy_sessions: settingsData.strategy_sessions || {},
          pre_signal_enabled: settingsData.pre_signal_enabled !== false,
          active_strategy: settingsData.active_strategy || 'scalping_4_rules',
          strategy_params: settingsData.strategy_params || {},
          coin_params: settingsData.coin_params || {},
          strategy_timeframes: settingsData.strategy_timeframes || {},
        });
        setStrategies(strategiesData.strategies || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const saveSettings = async (updatedSettings) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich zum Speichern'); return; }
    setSaving(true);
    try {
      const response = await fetch(`${API_URL}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(updatedSettings)
      });

      if (response.status === 401) {
        toast.error('Nicht autorisiert – bitte als Admin anmelden');
        return;
      }
      if (response.ok) {
        const data = await response.json();
        setSettings(prev => ({
          ...prev,
          custom_sessions: data.settings.custom_sessions || [],
          strategy_sessions: data.settings.strategy_sessions || {},
          pre_signal_enabled: data.settings.pre_signal_enabled !== false,
          active_strategy: data.settings.active_strategy || 'scalping_4_rules',
          strategy_params: data.settings.strategy_params || {},
          coin_params: data.settings.coin_params || {},
          strategy_timeframes: data.settings.strategy_timeframes || {},
        }));
        toast.success('Gespeichert');
      } else {
        toast.error('Fehler beim Speichern');
      }
    } catch (error) {
      toast.error('Verbindungsfehler beim Speichern');
    } finally {
      setSaving(false);
    }
  };

  // ---- Control State Toggle (same API as Header) ----
  const toggleControl = async (kind) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    if (busy) return;
    setBusy(true);
    try {
      const path = kind === 'trades' ? 'stop-trades' : 'stop-signals';
      const r = await fetch(`${API_URL}/api/control/${path}`, {
        method: 'POST', headers: { ...authHeaders() },
      });
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      if (kind === 'trades') {
        toast.success(d.trades_paused
          ? 'Trades gestoppt – keine neuen Trades; offene Positionen bleiben unangetastet'
          : 'Trades wieder aktiv');
      } else {
        toast.success(d.signals_paused ? 'Signals gestoppt' : 'Signals wieder aktiv');
      }
      onControlChanged && onControlChanged();
    } catch (e) {
      toast.error('Fehler: ' + (e.message || 'unbekannt'));
    } finally {
      setBusy(false);
    }
  };

  const updateStrategyTimeframe = (strategyId, tf) => {
    const tfs = { ...(settings.strategy_timeframes || {}), [strategyId]: tf };
    setSettings({ ...settings, strategy_timeframes: tfs });
    saveSettings({ strategy_timeframes: tfs });
    toast.success(`Timeframe ${tf} gespeichert – gilt für Signale, Paper & Live`);
  };

  // ---- Custom/Discovery-Strategien: Regeln & Indikator-Perioden dauerhaft anpassen ----
  const updateDefinition = (strategyId, mut) => {
    setStrategies(prev => prev.map(s => (s.id === strategyId
      ? { ...s, definition: mut({ ...(s.definition || {}) }) } : s)));
    setDefDirty(true);
  };

  const updateDefRuleValue = (strategyId, side, idx, value) =>
    updateDefinition(strategyId, def => ({
      ...def,
      [side]: (def[side] || []).map((r, i) => (i === idx ? { ...r, value } : r)),
    }));

  // Timeframe-Override pro Regel (Multi-Timeframe): '' = Strategie-TF
  const updateDefRuleTf = (strategyId, side, idx, tf) =>
    updateDefinition(strategyId, def => ({
      ...def,
      [side]: (def[side] || []).map((r, i) => {
        if (i !== idx) return r;
        const nr = { ...r, label: '' };
        if (tf) nr.timeframe = tf; else delete nr.timeframe;
        return nr;
      }),
    }));

  // Regel-TF muss ≥ Strategie-TF und ein Vielfaches davon sein (wie Backend)
  const ruleTfValid = (tf, baseTf) => {
    const base = TF_MINUTES[baseTf] || 1;
    const mins = TF_MINUTES[tf] || 0;
    return mins >= base && mins % base === 0;
  };

  const updateDefIndicator = (strategyId, key, value) =>
    updateDefinition(strategyId, def => ({
      ...def, indicators: { ...(def.indicators || {}), [key]: value },
    }));

  const saveDefinition = async (strategyId) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const s = strategies.find(x => x.id === strategyId);
    if (!s?.definition) return;
    try {
      const res = await fetch(`${API_URL}/api/strategies/custom`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(s.definition),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Speichern fehlgeschlagen'); return; }
      setDefDirty(false);
      toast.success('Strategie-Regeln gespeichert – gilt für Signale, Paper & Live');
    } catch { toast.error('Verbindungsfehler'); }
  };

  const updateStrategyParam = (strategyId, paramKey, value) => {
    const v = parseFloat(value);
    if (paramCoin) {
      const cp = settings.coin_params || {};
      const stratCp = cp[strategyId] || {};
      const coinCp = { ...(stratCp[paramCoin] || {}), [paramKey]: v };
      const newCp = { ...cp, [strategyId]: { ...stratCp, [paramCoin]: coinCp } };
      setSettings({ ...settings, coin_params: newCp });
    } else {
      const currentParams = settings.strategy_params[strategyId] || {};
      const newParams = { ...settings.strategy_params, [strategyId]: { ...currentParams, [paramKey]: v } };
      setSettings({ ...settings, strategy_params: newParams });
    }
  };

  const commitParams = (strategyId) => {
    if (paramCoin) saveSettings({ coin_params: settings.coin_params });
    else saveSettings({ strategy_params: settings.strategy_params });
  };

  const resetStrategyParams = (strategyId) => {
    if (paramCoin) {
      const cp = { ...(settings.coin_params || {}) };
      if (cp[strategyId]) { delete cp[strategyId][paramCoin]; }
      setSettings({ ...settings, coin_params: cp });
      saveSettings({ coin_params: cp });
      toast.success(`${paramCoin} Parameter zurückgesetzt`);
    } else {
      const newParams = { ...settings.strategy_params };
      delete newParams[strategyId];
      setSettings({ ...settings, strategy_params: newParams });
      saveSettings({ strategy_params: newParams });
      toast.success('Parameter auf Standard zurückgesetzt');
    }
  };

  const getCurrentParamValue = (strategyId, paramKey, defaultValue) => {
    const globalVal = settings.strategy_params[strategyId]?.[paramKey];
    if (paramCoin) {
      const coinVal = settings.coin_params?.[strategyId]?.[paramCoin]?.[paramKey];
      return coinVal ?? globalVal ?? defaultValue;
    }
    return globalVal ?? defaultValue;
  };

  // Sessions handlers
  const togglePreSignal = (value) => {
    setSettings({ ...settings, pre_signal_enabled: value });
    saveSettings({ pre_signal_enabled: value });
  };

  // Sessions handlers – arbeiten je nach Scope auf globalen oder
  // strategie-eigenen Zeitfenstern (strategy_sessions[strategyId])
  const isGlobalScope = sessionScope === 'global';
  const scopedSessions = isGlobalScope
    ? settings.custom_sessions
    : (settings.strategy_sessions?.[sessionScope] || []);

  const setScopedSessions = (list, save = true) => {
    if (isGlobalScope) {
      setSettings(prev => ({ ...prev, custom_sessions: list }));
      if (save) saveSettings({ custom_sessions: list });
    } else {
      const ss = { ...(settings.strategy_sessions || {}) };
      if (list.length) ss[sessionScope] = list;
      else delete ss[sessionScope];
      setSettings(prev => ({ ...prev, strategy_sessions: ss }));
      if (save) saveSettings({ strategy_sessions: ss });
    }
  };

  const addSession = () => {
    const newSession = {
      start: "09:00", end: "12:00",
      name: `Session ${scopedSessions.length + 1}`,
      enabled: true
    };
    setScopedSessions([...scopedSessions, newSession]);
  };

  const removeSession = (index) => {
    setScopedSessions(scopedSessions.filter((_, i) => i !== index));
  };

  const updateSession = (index, field, value) => {
    const updated = [...scopedSessions];
    updated[index] = { ...updated[index], [field]: value };
    setScopedSessions(updated, false);
  };

  const commitSessionUpdate = () => setScopedSessions([...scopedSessions]);

  const toggleSession = (index) => {
    const updated = [...scopedSessions];
    updated[index] = { ...updated[index], enabled: !updated[index].enabled };
    setScopedSessions(updated);
  };

  const enable24_7 = () => {
    setScopedSessions([]);
    toast.success(isGlobalScope ? '24/7 Modus aktiviert' : 'Strategie folgt jetzt dem globalen Zeitfenster');
  };

  const restoreDefaults = () => {
    setScopedSessions([
      { start: "09:00", end: "12:00", name: "London", enabled: true },
      { start: "15:30", end: "18:30", name: "US", enabled: true }
    ]);
  };

  const handleTestTelegram = async () => {
    setTesting(true);
    try {
      const response = await fetch(`${API_URL}/api/telegram/test`, { method: 'POST', headers: { ...authHeaders() } });
      if (response.ok) toast.success('Telegram Test erfolgreich!');
      else if (response.status === 401) toast.error('Admin-Login erforderlich');
      else toast.error('Fehler');
    } catch {
      toast.error('Verbindungsfehler');
    } finally {
      setTesting(false);
    }
  };

  const activeStrategy = strategies.find(s => s.id === focusStrategy)
    || strategies.find(s => s.id === settings.active_strategy)
    || strategies[0];
  const is24_7 = scopedSessions.length === 0;

  // ---- Komplettes Strategie-Backup direkt aus dem ⚙-Panel ----
  const exportStrategyBackup = async () => {
    if (!activeStrategy) return;
    try {
      const res = await fetch(`${API_URL}/api/strategies/${activeStrategy.id}/export`);
      if (!res.ok) { toast.error('Export fehlgeschlagen'); return; }
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const safe = (activeStrategy.name || activeStrategy.id).replace(/[^a-z0-9äöüß_-]+/gi, '_');
      a.download = `strategie-backup-${safe}-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast.success(`"${activeStrategy.name}" komplett exportiert (inkl. aller Parameter & Trade-Einstellungen)`);
    } catch { toast.error('Verbindungsfehler beim Export'); }
  };

  const importStrategyBackup = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); e.target.value = ''; return; }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const d = JSON.parse(reader.result);
        if (d.type !== 'strategy_backup') { toast.error('Keine gültige Strategie-Backup-Datei'); return; }
        const res = await fetch(`${API_URL}/api/strategies/import`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify(d),
        });
        const out = await res.json();
        if (!res.ok) { toast.error(out.detail || 'Import fehlgeschlagen'); return; }
        toast.success(`Strategie "${d.name || out.id}" 1:1 wiederhergestellt – Panel neu öffnen, um die Werte zu sehen`);
      } catch { toast.error('Datei konnte nicht gelesen werden'); }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  return (
    <SafeOverlay className="settings-overlay" onClose={onClose} closeOnOutside={false}>
      <div className="settings-panel" onClick={(e) => e.stopPropagation()} data-testid="settings-panel">
        <div className="settings-header">
          <h2>EINSTELLUNGEN {saving && <span className="text-muted" style={{fontSize: '12px'}}>· Speichere...</span>}</h2>
          <button className="settings-close" onClick={onClose} data-testid="settings-close-button">
            <X size={24} weight="bold" />
          </button>
        </div>

        {/* Tab Navigation */}
        <div className="settings-tabs">
          {mode !== 'general' && (
            <button 
              className={`settings-tab ${activeTab === 'strategy' ? 'active' : ''}`}
              onClick={() => setActiveTab('strategy')}
              data-testid="tab-strategy"
            >
              <ChartLineUp size={16} weight="bold" />
              Strategie
            </button>
          )}
          {mode !== 'general' && (
            <button 
              className={`settings-tab ${activeTab === 'sessions' ? 'active' : ''}`}
              onClick={() => setActiveTab('sessions')}
              data-testid="tab-sessions"
            >
              <Lightning size={16} weight="bold" />
              Zeitfenster
            </button>
          )}
          {/* NEW: Steuerung Tab - links neben Telegram */}
          {mode !== 'strategy' && (
            <button 
              className={`settings-tab ${activeTab === 'control' ? 'active' : ''}`}
              onClick={() => setActiveTab('control')}
              data-testid="tab-control"
            >
              <Power size={16} weight="bold" />
              Steuerung
            </button>
          )}
          {mode !== 'strategy' && (
            <button 
              className={`settings-tab ${activeTab === 'telegram' ? 'active' : ''}`}
              onClick={() => setActiveTab('telegram')}
              data-testid="tab-telegram"
            >
              <TelegramLogo size={16} weight="bold" />
              Telegram
            </button>
          )}
        </div>

        <div className="settings-content">
          {/* STRATEGY TAB */}
          {activeTab === 'strategy' && (
            <>
              {activeStrategy && (
                <div className="settings-section">
                  <div className="section-simple-header">
                    <h3>
                      <Sliders size={18} weight="bold" style={{marginRight: '8px', display: 'inline-block', verticalAlign: 'middle'}} />
                      {activeStrategy.name}
                    </h3>
                    <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span className="text-muted" style={{ fontSize: 12 }}>Gilt für:</span>
                      <select
                        value={paramCoin}
                        onChange={(e) => setParamCoin(e.target.value)}
                        data-testid="param-coin-select"
                        style={{ background: '#0A0A0A', border: '1px solid #2A2D3A', borderRadius: 8, padding: '7px 10px', color: '#fff' }}
                      >
                        <option value="">Alle Coins (Global)</option>
                        {ALL_COINS.map(c => <option key={c} value={c}>{assetLabel(c)}</option>)}
                      </select>
                      {paramCoin && <span className="param-custom-badge">PRO COIN</span>}
                    </div>
                    <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span className="text-muted" style={{ fontSize: 12 }}>Timeframe (Signale, Paper &amp; Live):</span>
                      <select
                        value={settings.strategy_timeframes?.[activeStrategy.id] || activeStrategy.timeframe || '1m'}
                        onChange={(e) => updateStrategyTimeframe(activeStrategy.id, e.target.value)}
                        data-testid="strategy-timeframe-select"
                        style={{ background: '#0A0A0A', border: '1px solid #2A2D3A', borderRadius: 8, padding: '7px 10px', color: '#fff' }}
                      >
                        {TIMEFRAMES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}
                      </select>
                      {settings.strategy_timeframes?.[activeStrategy.id] &&
                        settings.strategy_timeframes[activeStrategy.id] !== '1m' && (
                          <span className="param-custom-badge">CUSTOM</span>
                        )}
                    </div>
                  </div>

                  <div className="params-list">
                    {Object.entries(activeStrategy.params || {}).map(([paramKey, paramMeta]) => {
                      const currentValue = getCurrentParamValue(
                        activeStrategy.id, 
                        paramKey, 
                        paramMeta.value
                      );
                      const isCustom = paramCoin
                        ? settings.coin_params?.[activeStrategy.id]?.[paramCoin]?.[paramKey] !== undefined
                        : settings.strategy_params[activeStrategy.id]?.[paramKey] !== undefined;
                      
                      return (
                        <div key={paramKey} className="param-item" data-testid={`param-${paramKey}`}>
                          <div className="param-info">
                            <div className="param-label">
                              {paramMeta.label}
                              {isCustom && <span className="param-custom-badge">CUSTOM</span>}
                            </div>
                            <div className="param-description">
                              {paramMeta.description}
                            </div>
                            <div className="param-range">
                              Min: {paramMeta.min} · Max: {paramMeta.max} · Default: {paramMeta.value}
                            </div>
                          </div>
                          <div className="param-input-wrapper">
                            <input
                              type="number"
                              className="param-input"
                              value={currentValue}
                              min={paramMeta.min}
                              max={paramMeta.max}
                              step={paramMeta.step}
                              onChange={(e) => updateStrategyParam(activeStrategy.id, paramKey, e.target.value)}
                              onBlur={() => commitParams(activeStrategy.id)}
                              data-testid={`param-input-${paramKey}`}
                            />
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {activeStrategy.is_custom && activeStrategy.definition && (
                    <div data-testid="custom-def-editor" style={{ marginTop: 16 }}>
                      <div className="param-label" style={{ color: '#B388FF', marginBottom: 8 }}>
                        REGELN &amp; INDIKATOR-PERIODEN (Custom/Discovery · dauerhaft)
                        {defDirty && <span className="param-custom-badge" style={{ marginLeft: 8 }}>NICHT GESPEICHERT</span>}
                      </div>
                      <div className="params-list">
                        {(activeStrategy.definition.long_rules || []).map((r, i) => (
                          <div key={`L${i}`} className="param-item" data-testid={`def-rule-long-${i}`}>
                            <div className="param-info">
                              <div className="param-label" style={{ color: '#30D158' }}>
                                LONG: {r.label || `${r.indicator} ${r.op}`}
                                {r.timeframe && <span className="param-custom-badge" style={{ marginLeft: 6 }}>@{r.timeframe}</span>}
                              </div>
                            </div>
                            <div className="param-input-wrapper" style={{ display: 'flex', gap: 6 }}>
                              {typeof r.value === 'number' ? (
                                <input type="number" step="any" className="param-input" value={r.value}
                                  onChange={e => updateDefRuleValue(activeStrategy.id, 'long_rules', i,
                                    e.target.value === '' ? 0 : parseFloat(e.target.value))}
                                  data-testid={`def-rule-long-input-${i}`} />
                              ) : (
                                <input type="text" className="param-input" value={String(r.value)} disabled
                                  title="Indikator-Vergleich – im Strategie-Builder änderbar" />
                              )}
                              <select className="param-input" value={r.timeframe || ''}
                                onChange={e => updateDefRuleTf(activeStrategy.id, 'long_rules', i, e.target.value)}
                                title={`Timeframe nur für diese Regel (Multi-Timeframe). Standard = Strategie-TF (${activeStrategy.definition.timeframe || '1m'}). Nur Vielfache des Strategie-TF wählbar.`}
                                data-testid={`def-rule-long-tf-${i}`}
                                style={{ minWidth: 92 }}>
                                <option value="">TF: Strategie</option>
                                {RULE_TIMEFRAMES.map(t => (
                                  <option key={t.v} value={t.v}
                                    disabled={!ruleTfValid(t.v, activeStrategy.definition.timeframe || '1m')}>
                                    TF: {t.l}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </div>
                        ))}
                        {(activeStrategy.definition.short_rules || []).map((r, i) => (
                          <div key={`S${i}`} className="param-item" data-testid={`def-rule-short-${i}`}>
                            <div className="param-info">
                              <div className="param-label" style={{ color: '#FF6482' }}>
                                SHORT: {r.label || `${r.indicator} ${r.op}`}
                                {r.timeframe && <span className="param-custom-badge" style={{ marginLeft: 6 }}>@{r.timeframe}</span>}
                              </div>
                            </div>
                            <div className="param-input-wrapper" style={{ display: 'flex', gap: 6 }}>
                              {typeof r.value === 'number' ? (
                                <input type="number" step="any" className="param-input" value={r.value}
                                  onChange={e => updateDefRuleValue(activeStrategy.id, 'short_rules', i,
                                    e.target.value === '' ? 0 : parseFloat(e.target.value))}
                                  data-testid={`def-rule-short-input-${i}`} />
                              ) : (
                                <input type="text" className="param-input" value={String(r.value)} disabled
                                  title="Indikator-Vergleich – im Strategie-Builder änderbar" />
                              )}
                              <select className="param-input" value={r.timeframe || ''}
                                onChange={e => updateDefRuleTf(activeStrategy.id, 'short_rules', i, e.target.value)}
                                title={`Timeframe nur für diese Regel (Multi-Timeframe). Standard = Strategie-TF (${activeStrategy.definition.timeframe || '1m'}). Nur Vielfache des Strategie-TF wählbar.`}
                                data-testid={`def-rule-short-tf-${i}`}
                                style={{ minWidth: 92 }}>
                                <option value="">TF: Strategie</option>
                                {RULE_TIMEFRAMES.map(t => (
                                  <option key={t.v} value={t.v}
                                    disabled={!ruleTfValid(t.v, activeStrategy.definition.timeframe || '1m')}>
                                    TF: {t.l}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </div>
                        ))}
                        {Object.entries(activeStrategy.definition.indicators || {}).map(([k, v]) => (
                          typeof v === 'number' ? (
                            <div key={k} className="param-item" data-testid={`def-ind-${k}`}>
                              <div className="param-info">
                                <div className="param-label">{k}</div>
                                <div className="param-description">Indikator-Periode/Einstellung</div>
                              </div>
                              <div className="param-input-wrapper">
                                <input type="number" step="any" className="param-input" value={v}
                                  onChange={e => updateDefIndicator(activeStrategy.id, k,
                                    e.target.value === '' ? 0 : parseFloat(e.target.value))}
                                  data-testid={`def-ind-input-${k}`} />
                              </div>
                            </div>
                          ) : null
                        ))}
                      </div>
                      <button className="btn" onClick={() => saveDefinition(activeStrategy.id)}
                        disabled={!defDirty} data-testid="save-definition-btn"
                        style={{ marginTop: 8 }}>
                        {defDirty ? '💾 Regeln speichern' : 'Regeln gespeichert'}
                      </button>
                    </div>
                  )}

                  <button 
                    className="btn btn-reset"
                    onClick={() => resetStrategyParams(activeStrategy.id)}
                    data-testid="reset-params-btn"
                  >
                    Alle Parameter zurücksetzen
                  </button>
                  <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                    <button className="btn" onClick={exportStrategyBackup} data-testid="strategy-export-btn"
                      title="Komplette Strategie sichern: Regeln, Parameter, Timeframe, Zeitfenster, Live/Paper-Einstellungen">
                      ⬇ Strategie komplett exportieren
                    </button>
                    <button className="btn" onClick={() => importParamsRef.current?.click()} data-testid="strategy-import-btn"
                      title="Backup-Datei laden – stellt alle Einstellungen 1:1 wieder her">
                      ⬆ Backup laden
                    </button>
                    <input ref={importParamsRef} type="file" accept=".json,application/json"
                      style={{ display: 'none' }} onChange={importStrategyBackup} data-testid="strategy-import-file" />
                  </div>
                </div>
              )}

              <div className="settings-section">
                <div className="setting-toggle">
                  <div className="toggle-info">
                    <div className="toggle-title">Pre-Signal Warnungen aktivieren</div>
                    <div className="toggle-description">
                      Frühwarnungen wenn Signal in Kürze zu erwarten ist
                    </div>
                  </div>
                  <label className="switch">
                    <input 
                      type="checkbox" 
                      checked={settings.pre_signal_enabled !== false}
                      onChange={(e) => togglePreSignal(e.target.checked)}
                      data-testid="pre-signal-toggle"
                    />
                    <span className="slider"></span>
                  </label>
                </div>
              </div>
            </>
          )}

          {/* SESSIONS TAB */}
          {activeTab === 'sessions' && (
            <>
              <div className="settings-section">
                <div className="session-scope-row" style={{ marginBottom: 12 }}>
                  <label style={{ display: 'block', fontSize: 12, marginBottom: 4 }}>
                    Zeitfenster gelten für:
                  </label>
                  <select
                    value={sessionScope}
                    onChange={(e) => setSessionScope(e.target.value)}
                    data-testid="session-scope-select"
                    style={{ width: '100%', padding: '8px', background: 'rgba(0,0,0,0.3)', color: 'inherit', border: '1px solid rgba(255,255,255,0.15)', borderRadius: 6 }}
                  >
                    <option value="global">🌍 Global (alle Strategien ohne eigenes Fenster)</option>
                    {strategies.map(s => (
                      <option key={s.id} value={s.id}>
                        {s.name}{(settings.strategy_sessions?.[s.id]?.length) ? ' · eigenes Zeitfenster ✓' : ''}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="session-mode-info">
                  {is24_7 ? (
                    <div className="mode-badge mode-badge-active">
                      {isGlobalScope ? '⚡ 24/7 MODUS AKTIV' : '⚡ Folgt dem GLOBALEN Zeitfenster'}
                    </div>
                  ) : (
                    <div className="mode-badge">
                      📅 {scopedSessions.filter(s => s.enabled).length} Zeitfenster
                      {!isGlobalScope && ' (nur diese Strategie)'}
                    </div>
                  )}
                </div>

                <div className="sessions-list">
                  {scopedSessions.map((session, index) => (
                    <div key={index} className="session-item" data-testid={`session-${index}`}>
                      <label className="switch switch-small">
                        <input 
                          type="checkbox" 
                          checked={session.enabled !== false}
                          onChange={() => toggleSession(index)}
                        />
                        <span className="slider"></span>
                      </label>
                      <input 
                        type="text" className="session-name"
                        value={session.name || ''}
                        onChange={(e) => updateSession(index, 'name', e.target.value)}
                        onBlur={commitSessionUpdate}
                      />
                      <div className="session-times">
                        <input 
                          type="time" value={session.start || '09:00'}
                          onChange={(e) => updateSession(index, 'start', e.target.value)}
                          onBlur={commitSessionUpdate}
                          className="time-input"
                        />
                        <span className="text-muted">-</span>
                        <input 
                          type="time" value={session.end || '12:00'}
                          onChange={(e) => updateSession(index, 'end', e.target.value)}
                          onBlur={commitSessionUpdate}
                          className="time-input"
                        />
                      </div>
                      <button 
                        className="btn-icon-remove"
                        onClick={() => removeSession(index)}
                      >
                        <Trash size={16} />
                      </button>
                    </div>
                  ))}
                </div>

                <div className="session-actions">
                  <button className="btn btn-add-session" onClick={addSession} data-testid="add-session-btn">
                    <Plus size={16} weight="bold" />
                    Zeitfenster hinzufügen
                  </button>
                  {!is24_7 && (
                    <button className="btn btn-24-7" onClick={enable24_7} data-testid="enable-24-7-btn">
                      <Lightning size={16} weight="bold" />
                      {isGlobalScope ? '24/7 Modus' : 'Eigenes Fenster löschen (global nutzen)'}
                    </button>
                  )}
                  {is24_7 && (
                    <button className="btn" onClick={restoreDefaults} data-testid="restore-defaults-btn">
                      Standard (London + US)
                    </button>
                  )}
                </div>
                
                <div className="info-hint">
                  💡 Alle Zeiten in deutscher Zeit (MEZ/CET). Ohne Zeitfenster → 24/7 Modus.
                  Strategien mit eigenem Zeitfenster ignorieren das globale Fenster.
                  Im Backtester lassen sich Zeitfenster pro Strategie ebenfalls testen (⚙-Panel).
                </div>
              </div>
            </>
          )}

          {/* NEW: STEUERUNG TAB */}
          {activeTab === 'control' && (
            <>
              <div className="settings-section">
                <div className="section-simple-header">
                  <h3>
                    <Power size={18} weight="bold" style={{marginRight: '8px', display: 'inline-block', verticalAlign: 'middle'}} />
                    Master-Steuerung
                  </h3>
                </div>

                {/* Guard-Status: sofort sichtbar, WAS gerade neue Trades blockiert und warum */}
                {(controlState?.trades_paused || guard?.state?.paused) ? (
                  <div className="guard-alert" data-testid="guard-alert-banner">
                    <div className="guard-alert-title">🛑 Auto-Trading ist aktuell blockiert</div>
                    {controlState?.trades_paused && (
                      <div className="guard-alert-row" data-testid="guard-alert-master">
                        <span>
                          <b>Master-Schalter „Trades AUS"</b> – der Bot eröffnet keine neuen
                          Trades (alle Strategien, KI-Trader &amp; Custom-Trades).
                        </span>
                        <button className="guard-alert-btn" disabled={busy}
                          onClick={() => toggleControl('trades')}
                          data-testid="guard-alert-resume-trades">
                          Trades wieder aktivieren
                        </button>
                      </div>
                    )}
                    {guard?.state?.paused && (
                      <div className="guard-alert-row" data-testid="guard-alert-killswitch">
                        <span>
                          <b>Kill-Switch (Drawdown-Guard):</b> {guard.state.reason}
                          <br />
                          <small>
                            pausiert bis {new Date(guard.state.paused_until).toLocaleString('de-DE', { timeZone: 'Europe/Berlin' })} – danach automatisch wieder aktiv
                          </small>
                        </span>
                        <button className="guard-alert-btn" onClick={resumeGuard}
                          data-testid="guard-alert-resume-killswitch">
                          Jetzt aufheben
                        </button>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="guard-ok" data-testid="guard-ok-banner">
                    ✓ Keine Blockade aktiv – Auto-Trading läuft nach den Coin-Einstellungen.
                  </div>
                )}

                <div className="control-card" data-testid="control-trades-card">
                  <div className="control-card-header">
                    <div className="control-card-info">
                      <div className="control-card-title">Bot-Trades</div>
                      <div className="control-card-desc">
                        Alle automatischen Trades global starten oder stoppen. Beim Stoppen werden
                        NUR neue Trades verhindert – bereits offene Positionen bleiben unangetastet
                        und werden normal weiter verwaltet (SL/TP/Break-Even).
                      </div>
                    </div>
                    <button
                      className={`control-master-btn ${controlState?.trades_paused ? 'paused' : 'active'}`}
                      onClick={() => toggleControl('trades')}
                      disabled={busy}
                      data-testid="control-toggle-trades"
                    >
                      {controlState?.trades_paused
                        ? <PlayCircle size={22} weight="fill" />
                        : <PauseCircle size={22} weight="fill" />}
                      <span className="control-master-label">
                        {controlState?.trades_paused ? 'TRADES AUS' : 'TRADES AN'}
                      </span>
                      <span className={`control-master-pill ${controlState?.trades_paused ? 'off' : 'on'}`}>
                        {controlState?.trades_paused ? 'GESTOPPT' : 'AKTIV'}
                      </span>
                    </button>
                  </div>
                </div>

                <div className="control-card" data-testid="control-signals-card">
                  <div className="control-card-header">
                    <div className="control-card-info">
                      <div className="control-card-title">Signale</div>
                      <div className="control-card-desc">
                        Alle Signal-Benachrichtigungen global aktivieren oder deaktivieren.
                      </div>
                    </div>
                    <button
                      className={`control-master-btn ${controlState?.signals_paused ? 'paused' : 'active'}`}
                      onClick={() => toggleControl('signals')}
                      disabled={busy}
                      data-testid="control-toggle-signals"
                    >
                      {controlState?.signals_paused
                        ? <PlayCircle size={22} weight="fill" />
                        : <PauseCircle size={22} weight="fill" />}
                      <span className="control-master-label">
                        {controlState?.signals_paused ? 'SIGNALE AUS' : 'SIGNALE AN'}
                      </span>
                      <span className={`control-master-pill ${controlState?.signals_paused ? 'off' : 'on'}`}>
                        {controlState?.signals_paused ? 'GESTOPPT' : 'AKTIV'}
                      </span>
                    </button>
                  </div>
                </div>

                <div className="control-card" data-testid="control-sync-card">
                  <div className="control-card-header">
                    <div className="control-card-info">
                      <div className="control-card-title">Bitunix-Abgleich</div>
                      <div className="control-card-desc">
                        Gleicht offene Live-Trades mit den echten Bitunix-Positionen ab –
                        extern geschlossene Positionen werden auch hier geschlossen.
                        Läuft automatisch jede Minute.
                        <br />
                        <span className="sync-status-line" data-testid="bitunix-sync-status">
                          {syncStatus?.last_sync_at
                            ? <>Letzter Abgleich: <b>{new Date(syncStatus.last_sync_at).toLocaleString('de-DE', { timeZone: 'Europe/Berlin' })}</b>
                              {syncStatus.last_synced ? ` · ${syncStatus.last_synced} übernommen` : ' · alles synchron'}
                              {typeof syncStatus.open_live === 'number' ? ` · ${syncStatus.open_live} offene Live-Position(en)` : ''}</>
                            : (syncStatus?.configured === false
                              ? 'Keine Bitunix-API-Keys konfiguriert – Abgleich nicht möglich.'
                              : 'Noch kein Abgleich durchgeführt.')}
                        </span>
                      </div>
                    </div>
                    <button
                      className="control-master-btn active"
                      onClick={runBitunixSync}
                      disabled={syncBusy || syncStatus?.configured === false}
                      data-testid="bitunix-sync-btn"
                    >
                      <ArrowsClockwise size={22} weight="bold" className={syncBusy ? 'spin' : ''} />
                      <span className="control-master-label">
                        {syncBusy ? 'GLEICHT AB…' : 'MIT BITUNIX ABGLEICHEN'}
                      </span>
                    </button>
                  </div>
                </div>

                <div className="control-card" data-testid="control-watchdog-card">
                  <div className="control-card-header">
                    <div className="control-card-info">
                      <div className="control-card-title">Positions-Watchdog</div>
                      <div className="control-card-desc">
                        Prüft alle offenen Bitunix-Positionen: Bei Website-Trades werden fehlende
                        Stop-Losses automatisch nachgezogen. Manuell über die Bitunix-App eröffnete
                        Positionen werden nur als „Manuell (Bitunix)" sichtbar gemacht und
                        NICHT angefasst (kein SL-Zwang, kein Aufräumen von Resten).
                        Läuft automatisch alle {watchdogStatus?.settings?.interval_sec || 120} Sekunden.
                        <br />
                        <span className="sync-status-line" data-testid="watchdog-status">
                          {watchdogStatus?.last_run_at
                            ? <>Letzter Lauf: <b>{new Date(watchdogStatus.last_run_at).toLocaleString('de-DE', { timeZone: 'Europe/Berlin' })}</b>
                              {` · ${watchdogStatus.positions ?? 0} Position(en) geprüft`}
                              {watchdogStatus.sl_fixed ? ` · ${watchdogStatus.sl_fixed} SL nachgezogen` : ''}
                              {watchdogStatus.adopted ? ` · ${watchdogStatus.adopted} übernommen` : ''}
                              {watchdogStatus.emergency_closed ? ` · ${watchdogStatus.emergency_closed} Notfall-Close` : ''}</>
                            : (watchdogStatus?.configured === false
                              ? 'Keine Bitunix-API-Keys konfiguriert – Watchdog inaktiv.'
                              : 'Noch kein Lauf durchgeführt.')}
                        </span>
                      </div>
                    </div>
                    <button
                      className="control-master-btn active"
                      onClick={runWatchdog}
                      disabled={watchdogBusy || watchdogStatus?.configured === false
                        || watchdogStatus?.settings?.enabled === false}
                      data-testid="watchdog-run-btn"
                    >
                      <ArrowsClockwise size={22} weight="bold" className={watchdogBusy ? 'spin' : ''} />
                      <span className="control-master-label">
                        {watchdogBusy ? 'PRÜFT…' : 'JETZT PRÜFEN'}
                      </span>
                    </button>
                  </div>
                  <label className="tg-toggle-row" data-testid="watchdog-enabled-toggle"
                    style={{ marginTop: 10 }}
                    title="AUS: Der Watchdog stoppt KOMPLETT – keine Prüfzyklen, keine SL-Nachzüge, keine Übernahmen, keine Eingriffe.">
                    <input type="checkbox"
                      checked={watchdogStatus?.settings?.enabled !== false}
                      onChange={e => setWatchdogEnabled(e.target.checked)} />
                    <span><b>Watchdog aktiv</b> – AUS stoppt die Überwachung komplett
                      (keine Prüfzyklen, keine Eingriffe)</span>
                  </label>
                  <label className="tg-toggle-row" data-testid="watchdog-manage-external-toggle"
                    style={{ marginTop: 10 }}
                    title="AN: Der Watchdog behandelt auch manuell über die Bitunix-App eröffnete Positionen wie eigene (fehlender SL wird nachgezogen, Reste aufgeräumt). AUS: Externe Positionen werden nur sichtbar gemacht, aber nie angefasst.">
                    <input type="checkbox"
                      checked={!!watchdogStatus?.settings?.manage_external}
                      disabled={watchdogStatus?.configured === false}
                      onChange={e => setWatchdogManageExternal(e.target.checked)} />
                    <span><b>Externe Positionen aktiv verwalten</b> – SL-Zwang &amp; Aufräumen auch
                      für manuell in der Bitunix-App eröffnete Positionen (Standard: AUS, nur beobachten)</span>
                  </label>
                  <button
                    onClick={clearWatchdogData}
                    data-testid="watchdog-clear-btn"
                    title="Löscht die Watchdog-Lauf-Statistik und alle übernommenen Extern-Trades (mit Sicherheitsabfrage)"
                    style={{
                      marginTop: 12, padding: '6px 12px', fontSize: 12, fontWeight: 700,
                      background: 'rgba(255, 51, 102, 0.12)', color: '#FF3366',
                      border: '1px solid rgba(255, 51, 102, 0.4)', borderRadius: 6, cursor: 'pointer',
                    }}
                  >
                    🗑 Verlauf &amp; Statistik löschen
                  </button>
                </div>

                <div className="info-hint">
                  💡 Diese Einstellungen gelten global für alle Strategien und Coins. Änderungen werden sofort wirksam.
                </div>
              </div>

              <div className="settings-section" data-testid="trade-guard-section">
                <div className="section-simple-header">
                  <h3>🛑 Kill-Switch &amp; Anti-Stacking (Risiko-Notbremse)</h3>
                </div>
                {guard?.state?.paused && (
                  <div className="info-box" style={{ borderColor: '#FF3366' }}>
                    <div className="info-text">
                      🛑 <strong>Kill-Switch AKTIV:</strong> {guard.state.reason}
                      <br />Auto-Trading pausiert bis {new Date(guard.state.paused_until).toLocaleString('de-DE')} – danach automatisch wieder an.
                      <br /><button className="btn btn-long" style={{ marginTop: 6 }} onClick={resumeGuard} data-testid="guard-resume-btn">Jetzt manuell aufheben</button>
                    </div>
                  </div>
                )}
                {guard && (
                  <div className="control-card">
                    <label className="tg-toggle-row" data-testid="guard-killswitch-toggle">
                      <input type="checkbox" checked={guard.config.kill_switch_enabled}
                        onChange={e => saveGuard({ kill_switch_enabled: e.target.checked })} />
                      <span><b>Kill-Switch aktiv</b> – stoppt Auto-Trading automatisch bis Mitternacht (UTC) bei:</span>
                    </label>
                    <div className="tg-guard-grid" style={{ display: 'flex', gap: 14, flexWrap: 'wrap', margin: '8px 0 12px 22px' }}>
                      <label style={{ fontSize: 12 }}>Max. Tagesverlust %
                        <input type="number" step={0.5} min={0} className="guard-num-input" style={{ width: 70, marginLeft: 6 }}
                          value={guard.config.max_daily_loss_pct}
                          onChange={e => saveGuard({ max_daily_loss_pct: parseFloat(e.target.value) || 0 })}
                          data-testid="guard-daily-loss-input" />
                      </label>
                      <label style={{ fontSize: 12 }}>Max. Verlust-Trades in Folge
                        <input type="number" step={1} min={0} className="guard-num-input" style={{ width: 60, marginLeft: 6 }}
                          value={guard.config.max_consecutive_losses}
                          onChange={e => saveGuard({ max_consecutive_losses: parseInt(e.target.value) || 0 })}
                          data-testid="guard-consec-losses-input" />
                      </label>
                      <label style={{ fontSize: 12 }} title="Bezugskapital für den Tagesverlust-%. 0 = automatisch (Summe der eingesetzten Margins des Tages)">Bezugskapital (0=auto)
                        <input type="number" step={10} min={0} className="guard-num-input" style={{ width: 80, marginLeft: 6 }}
                          value={guard.config.ref_capital}
                          onChange={e => saveGuard({ ref_capital: parseFloat(e.target.value) || 0 })} />
                      </label>
                    </div>
                    <label className="tg-toggle-row" data-testid="guard-antistacking-toggle">
                      <input type="checkbox" checked={guard.config.anti_stacking_enabled}
                        onChange={e => saveGuard({ anti_stacking_enabled: e.target.checked })} />
                      <span><b>Anti-Stacking</b> – gleiche Richtung + gleiches Asset + gleicher Timeframe blockiert für</span>
                    </label>
                    <label style={{ fontSize: 12, marginLeft: 22 }}>Cooldown (Minuten)
                      <input type="number" step={5} min={1} className="guard-num-input" style={{ width: 60, marginLeft: 6 }}
                        value={guard.config.stacking_cooldown_min}
                        onChange={e => saveGuard({ stacking_cooldown_min: parseFloat(e.target.value) || 30 })}
                        data-testid="guard-cooldown-input" />
                    </label>
                    <div className="info-hint" style={{ marginTop: 8 }}>
                      💡 Anderer Timeframe oder Gegenrichtung (Hedge) bleibt IMMER erlaubt – Multi-Timeframe-Einstiege und übergeordnete Long/Short-Kombis funktionieren weiter.
                    </div>
                  </div>
                )}
              </div>
            </>
          )}

          {/* TELEGRAM TAB */}
          {activeTab === 'telegram' && (
            <>
              <div className="settings-section">
                <div className="info-box" style={{ borderColor: '#00FF66' }}>
                  <div className="info-text">
                    ✅ <strong>Bot verbunden:</strong> @Krypto_Strategy_Alert_Bot
                    <br />
                    Signale werden automatisch an dich gesendet
                  </div>
                </div>

                <button 
                  className="btn btn-long" 
                  onClick={handleTestTelegram} 
                  disabled={testing}
                  data-testid="test-telegram-button"
                >
                  {testing ? 'Teste...' : 'Test-Nachricht senden'}
                </button>
              </div>

              <div className="settings-section" data-testid="telegram-notify-section">
                <div className="section-simple-header">
                  <h3>🔔 Telegram-Meldungen (an/aus)</h3>
                </div>
                {!notifyCfg && <div>Lade…</div>}
                {notifyCfg && Object.keys(NOTIFY_LABELS).map(key => (
                  <label key={key} className="tg-toggle-row" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 2px', cursor: 'pointer', fontSize: 13 }} data-testid={`notify-toggle-${key}`}>
                    <input type="checkbox" checked={notifyCfg[key] !== false}
                      onChange={e => saveNotify(key, e.target.checked)} />
                    <span>{NOTIFY_LABELS[key]}</span>
                  </label>
                ))}
                <div className="info-hint">
                  💡 KI-Ausfall meldet sich erst, wenn Primär- UND Backup-KI einer Anfrage scheitern und ein Notfall-Fallback übernehmen muss (max. 1× pro 30 Min).
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </SafeOverlay>
  );
};

export default SettingsPanel;
