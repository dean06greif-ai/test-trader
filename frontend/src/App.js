import React, { useState, useEffect, useRef, useCallback } from 'react';
import './App.css';
import './components/extra.css';
import Header from './components/Header';
import CoinSidebar from './components/CoinSidebar';
import MainChart from './components/MainChart';
import SignalPanel from './components/SignalPanel';
import StrategyTabs from './components/StrategyTabs';
import PerformanceAnalytics from './components/PerformanceAnalytics';
import AlertModal from './components/AlertModal';
import SettingsPanel from './components/SettingsPanel';
import StrategyBuilder from './components/StrategyBuilder';
import StrategyAutoTradeModal from './components/StrategyAutoTradeModal';
import AITradingPanel from './components/AITradingPanel';
import StrategyComparison from './components/StrategyComparison';
import Backtester from './components/Backtester';
import Optimizer from './components/Optimizer';
import RegimeLab from './components/RegimeLab';
import LiquidityPanel from './components/LiquidityPanel';
import ErrorBoundary from './components/ErrorBoundary';
import AdminLogin from './components/AdminLogin';
// mobile.css MUST be imported LAST so its media queries override
// the component-level CSS (Header.css, StrategyTabs.css, ...).
import './components/mobile.css';
import { Toaster } from 'sonner';
import { toast } from './lib/toast';
import { isAdmin as isAdminFn, clearToken, authHeaders } from './auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;

function App() {
  const [selectedCoin, setSelectedCoin] = useState('BTCUSDT');
  const [strategies, setStrategies] = useState([]);
  const [enabledIds, setEnabledIds] = useState([]);
  const [signalsEnabled, setSignalsEnabled] = useState({});
  const [selectedStrategy, setSelectedStrategy] = useState(null);
  const [signals, setSignals] = useState([]);
  const [chartSignal, setChartSignal] = useState(null);
  const [performance, setPerformance] = useState([]);
  const [ruleStates, setRuleStates] = useState({});
  const [candleData, setCandleData] = useState({});
  const [notifications, setNotifications] = useState({});
  const [autotradeCoins, setAutotradeCoins] = useState({});
  const [strategyOverrides, setStrategyOverrides] = useState({});
  const [strategyCoinConfigs, setStrategyCoinConfigs] = useState({});
  const [sessionActive, setSessionActive] = useState(false);
  const [currentSession, setCurrentSession] = useState('');
  const [customSessions, setCustomSessions] = useState([]);
  const [berlinTime, setBerlinTime] = useState('');
  const [showAlert, setShowAlert] = useState(false);
  const [currentAlert, setCurrentAlert] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showStrategySettings, setShowStrategySettings] = useState(false);
  const [showAIPanel, setShowAIPanel] = useState(false);
  const [showBuilder, setShowBuilder] = useState(false);
  const [strategyAutoTradeId, setStrategyAutoTradeId] = useState(null);
  const [showComparison, setShowComparison] = useState(false);
  const [showBacktester, setShowBacktester] = useState(false);
  const [showOptimizer, setShowOptimizer] = useState(false);
  const [showRegimeLab, setShowRegimeLab] = useState(false);
  const [showLiquidity, setShowLiquidity] = useState(false);
  const [adminAuthed, setAdminAuthed] = useState(isAdminFn());
  const [showLogin, setShowLogin] = useState(false);
  const [controlState, setControlState] = useState({ trades_paused: false, signals_paused: false });

  const wsRef = useRef(null);
  const audioRef = useRef(null);
  const notificationsRef = useRef({});
  const reconnectRef = useRef(null);

  useEffect(() => { notificationsRef.current = notifications; }, [notifications]);

  // ---- data loaders ----
  const loadStrategies = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/strategies`).then(r => r.json());
      setStrategies(data.strategies || []);
      setEnabledIds(data.enabled || []);
      setSignalsEnabled(data.signals_enabled || {});
      setSelectedStrategy(prev => {
        if (prev && (data.enabled || []).includes(prev)) return prev;
        return (data.enabled || [])[0] || null;
      });
    } catch (e) { console.error(e); }
  }, []);

  const loadAutotrade = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/autotrade/config`).then(r => r.json());
      setAutotradeCoins((data.config && data.config.coins) || {});
      setStrategyOverrides(data.strategy_overrides || (data.config && data.config.strategy_overrides) || {});
    } catch (e) { console.error(e); }
    // load per-strategy per-coin configs (admin-only)
    try {
      const res = await fetch(`${API_URL}/api/autotrade/strategy_coin_configs`, { headers: authHeaders() });
      if (res.ok) {
        const data = await res.json();
        setStrategyCoinConfigs(data.configs || {});
      }
    } catch (e) { /* silent — non-admin users won't have access */ }
  }, []);

  const loadSignals = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/signals?limit=80`).then(r => r.json());
      setSignals(data.signals || []);
    } catch (e) { console.error(e); }
  }, []);

  const loadPerformance = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/performance`).then(r => r.json());
      setPerformance(data.performance || []);
    } catch (e) { console.error(e); }
  }, []);

  const loadControlState = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/control/state`).then(r => r.json());
      setControlState({
        trades_paused: !!data.trades_paused,
        signals_paused: !!data.signals_paused,
      });
    } catch (e) { console.error(e); }
  }, []);

  // ---- WebSocket with auto-reconnect ----
  const connectWS = useCallback(() => {
    try {
      const wsUrl = API_URL.replace('http', 'ws') + '/api/ws';
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => { toast.success('Verbunden mit Scanner'); };
      ws.onmessage = (event) => {
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message.type === 'signal') {
          const signal = message.data;
          setSignals(prev => [signal, ...prev].slice(0, 120));
          const notifyEnabled = notificationsRef.current[signal.symbol] !== false;
          if (notifyEnabled && signal.signal_class !== 'PRE_SIGNAL') {
            // Mobil: kein blockierendes Alert-Popup mehr (musste immer
            // weggeklickt werden) – Toast + Sound reichen. Desktop unverändert.
            const isMobile = window.matchMedia('(max-width: 640px)').matches;
            if (!isMobile) { setCurrentAlert(signal); setShowAlert(true); }
            if (audioRef.current) audioRef.current.play().catch(() => {});
            toast.success(`${signal.type} · ${signal.symbol.replace('USDT','')}`, {
              description: `${signal.strategy_name} · Entry $${signal.entry_price}`, duration: 5000,
            });
          }
        } else if (message.type === 'candle') {
          setCandleData(prev => ({ ...prev, [message.symbol]: message.data }));
        } else if (message.type === 'rule_states') {
          setRuleStates(prev => ({ ...prev, [message.symbol]: message.data }));
        } else if (message.type === 'daily_reset') {
          setSignals([]);
          toast.info('Täglicher Reset: Signale zurückgesetzt, Analyse bleibt erhalten');
          loadPerformance();
        } else if (message.type === 'analytics_cleared') {
          loadSignals();
          loadPerformance();
        } else if (message.type === 'control_state') {
          setControlState({
            trades_paused: !!message.data?.trades_paused,
            signals_paused: !!message.data?.signals_paused,
          });
        }
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        if (reconnectRef.current) clearTimeout(reconnectRef.current);
        reconnectRef.current = setTimeout(connectWS, 3000);
      };
    } catch (e) { console.error('WS connect failed', e); }
  }, [loadPerformance, loadSignals]);

  useEffect(() => {
    connectWS();
    try {
      if (typeof window !== 'undefined' && 'Notification' in window && window.Notification.permission === 'default') {
        window.Notification.requestPermission().catch(() => {});
      }
    } catch (_) { /* iOS Safari & private mode: Notification API not available */ }
    return () => {
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      if (wsRef.current) { wsRef.current.onclose = null; wsRef.current.close(); }
    };
  }, [connectWS]);

  useEffect(() => { loadStrategies(); loadAutotrade(); loadSignals(); loadPerformance(); loadControlState(); }, [loadStrategies, loadAutotrade, loadSignals, loadPerformance, loadControlState]);
  useEffect(() => {
    const iv = setInterval(loadPerformance, 60000);
    return () => clearInterval(iv);
  }, [loadPerformance]);

  // session + notifications
  useEffect(() => {
    const fetchSession = async () => {
      try {
        const data = await fetch(`${API_URL}/api/session/status`).then(r => r.json());
        setSessionActive(data.is_active);
        setCurrentSession(data.current_session || '');
        setCustomSessions(data.custom_sessions || []);
        setBerlinTime(data.berlin_time || '');
      } catch (e) { console.error(e); }
    };
    const fetchNotif = async () => {
      try {
        const data = await fetch(`${API_URL}/api/settings`).then(r => r.json());
        setNotifications(data.notifications || {});
      } catch (e) { console.error(e); }
    };
    fetchSession(); fetchNotif();
    const iv = setInterval(fetchSession, 30000);
    return () => clearInterval(iv);
  }, [showSettings]);

  // ---- admin gate ----
  const requireAdmin = (action) => {
    if (isAdminFn()) { action(); }
    else { setShowLogin(true); toast.info('Admin-Login erforderlich'); }
  };
  const handleAdminClick = () => {
    if (adminAuthed) { clearToken(); setAdminAuthed(false); toast.success('Admin abgemeldet'); }
    else { setShowLogin(true); }
  };

  // ---- actions ----
  const toggleNotification = async (symbol) => {
    const current = notifications[symbol] !== false;
    const updated = { ...notifications, [symbol]: !current };
    setNotifications(updated);
    await fetch(`${API_URL}/api/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ notifications: updated }) });
    toast.success(`${symbol.replace('USDT','')}: Alerts ${!current ? 'AN' : 'AUS'}`);
  };

  const toggleCoinAutoTrade = async (symbol, next) => {
    if (!isAdminFn()) {
      setShowLogin(true);
      toast.info('Admin-Login erforderlich');
      return;
    }
    setAutotradeCoins(prev => ({
      ...prev,
      [symbol]: { ...(prev[symbol] || {}), enabled: !!next },
    }));
    try {
      const res = await fetch(`${API_URL}/api/autotrade/coin/${symbol}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ enabled: !!next }),
      });
      if (res.status === 401) {
        toast.error('Nicht autorisiert – bitte als Admin anmelden');
        loadAutotrade();
        return;
      }
      if (!res.ok) {
        toast.error('Fehler beim Umschalten');
        loadAutotrade();
        return;
      }
      toast.success(`${symbol.replace('USDT', '')}: Auto-Trade ${next ? 'AN' : 'AUS'}`);
      loadAutotrade();
    } catch (e) {
      toast.error('Verbindungsfehler');
      loadAutotrade();
    }
  };

  const toggleSignals = async (strategyId) => {
    const currentOverride = strategyOverrides[strategyId] || {};
    const current = currentOverride.signals_enabled !== false;
    const newEnabled = !current;
    
    try {
      const res = await fetch(`${API_URL}/api/autotrade/strategy/${strategyId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ signals_enabled: newEnabled })
      });
      if (res.ok) {
        setStrategyOverrides(prev => ({
          ...prev,
          [strategyId]: { ...prev[strategyId], signals_enabled: newEnabled }
        }));
        toast.success(`Signale ${newEnabled ? 'AN' : 'AUS'}`);
      }
    } catch (e) {
      console.error(e);
      toast.error('Fehler beim Umschalten');
    }
  };

  const strategyMeta = strategies.find(s => s.id === selectedStrategy);
  const ruleState = ruleStates[selectedCoin]?.[selectedStrategy];
  const latestSignal = signals.find(s => s.symbol === selectedCoin && s.strategy_id === selectedStrategy);
  return (
    <ErrorBoundary onReset={() => window.location.reload()}>
    <div className="App">
      <Toaster position="bottom-right" theme="dark" richColors />
      <audio ref={audioRef} src="/alert.mp3" preload="auto" />

      <Header
        sessionActive={sessionActive}
        currentSession={currentSession}
        customSessions={customSessions}
        activeStrategy={strategyMeta}
        berlinTime={berlinTime}
        adminAuthed={adminAuthed}
        onAdminClick={handleAdminClick}
        onSettingsClick={() => requireAdmin(() => setShowSettings(true))}
        onCompareClick={() => setShowComparison(true)}
        onBacktestClick={() => setShowBacktester(true)}
        onOptimizerClick={() => setShowOptimizer(true)}
        onRegimeLabClick={() => setShowRegimeLab(true)}
        onLiquidityClick={() => setShowLiquidity(true)}
      />

      <div className="app-layout">
        <CoinSidebar
          selectedCoin={selectedCoin}
          onSelectCoin={setSelectedCoin}
          performance={performance}
          notifications={notifications}
          onToggleNotification={toggleNotification}
          ruleStates={ruleStates}
          selectedStrategy={selectedStrategy}
          autotradeCoins={autotradeCoins}
          onToggleAutoTrade={toggleCoinAutoTrade}
        />

        <div className="main-content">
          <StrategyTabs
            strategies={strategies}
            enabledIds={enabledIds}
            selected={selectedStrategy}
            signalsEnabled={signalsEnabled}
            strategyOverrides={strategyOverrides}
            strategyCoinConfigs={strategyCoinConfigs}
            selectedCoin={selectedCoin}
            onSelect={setSelectedStrategy}
            onToggleSignals={(id) => requireAdmin(() => toggleSignals(id))}
            onManage={() => requireAdmin(() => setShowBuilder(true))}
            onEditParams={() => {
              if (selectedStrategy === 'ai_trader') requireAdmin(() => setShowAIPanel(true));
              else requireAdmin(() => setShowStrategySettings(true));
            }}
            onOpenStrategyAutoTrade={(id) => requireAdmin(() => setStrategyAutoTradeId(id))}
          />
          <ErrorBoundary onReset={() => setSelectedCoin('BTCUSDT')}>
            <MainChart symbol={selectedCoin} candleData={candleData[selectedCoin]}
              signal={chartSignal} onClearSignal={() => setChartSignal(null)} />
          </ErrorBoundary>
          <SignalPanel
            symbol={selectedCoin}
            ruleState={ruleState}
            latestSignal={latestSignal}
            strategyMeta={strategyMeta}
            onShowInChart={(sig) => setChartSignal(prev => (prev && prev.id === sig.id ? null : sig))}
          />
        </div>

        <div className="right-panel">
          <ErrorBoundary>
            <PerformanceAnalytics
              performance={performance}
              strategies={strategies}
              enabledIds={enabledIds}
              signals={signals}
              selectedCoin={selectedCoin}
              selectedStrategy={selectedStrategy}
              strategyOverrides={strategyOverrides}
              strategyCoinConfigs={strategyCoinConfigs}
              isAdmin={adminAuthed}
              onNeedAdmin={() => requireAdmin(() => {})}
              onCleared={() => { loadSignals(); loadPerformance(); }}
              onShowChart={setSelectedCoin}
            />
          </ErrorBoundary>
        </div>
      </div>

      {showAlert && currentAlert && (
        <AlertModal signal={currentAlert} onClose={() => setShowAlert(false)} />
      )}
      {showSettings && (
        <SettingsPanel
          onClose={() => { setShowSettings(false); loadStrategies(); }}
          focusStrategy={selectedStrategy}
          mode="general"
          controlState={controlState}
          onControlChanged={loadControlState}
        />
      )}
      {showStrategySettings && (
        <SettingsPanel
          onClose={() => { setShowStrategySettings(false); loadStrategies(); }}
          focusStrategy={selectedStrategy}
          mode="strategy"
          controlState={controlState}
          onControlChanged={loadControlState}
        />
      )}
      {showAIPanel && (
        <AITradingPanel selectedCoin={selectedCoin} onClose={() => setShowAIPanel(false)} />
      )}
      {showBuilder && (
        <StrategyBuilder
          strategies={strategies}
          enabledIds={enabledIds}
          onClose={() => setShowBuilder(false)}
          onChanged={loadStrategies}
        />
      )}
     {strategyAutoTradeId && (
  <StrategyAutoTradeModal
    strategyId={strategyAutoTradeId}
    strategyName={strategies.find(s => s.id === strategyAutoTradeId)?.name}
    symbol={selectedCoin}
    onClose={() => setStrategyAutoTradeId(null)}
    onSaved={loadAutotrade}
        />
      )}
      {showLogin && (
        <AdminLogin
          onClose={() => setShowLogin(false)}
          onSuccess={() => {
            setAdminAuthed(true);
            // Nach dem Login sofort alle auth-abhängigen Daten neu laden,
            // damit z.B. der blaue Paper-Blitz ohne Seiten-Reload erscheint.
            loadAutotrade();
            loadStrategies();
            loadControlState();
            loadPerformance();
          }}
        />
      )}
      {showComparison && (
        <StrategyComparison onClose={() => setShowComparison(false)} />
      )}
      {showBacktester && (
        <Backtester onClose={() => setShowBacktester(false)} />
      )}
      {showOptimizer && (
        <Optimizer onClose={() => setShowOptimizer(false)} />
      )}
      {showRegimeLab && (
        <RegimeLab onClose={() => setShowRegimeLab(false)} />
      )}
      {showLiquidity && (
        <LiquidityPanel symbol={selectedCoin} onClose={() => setShowLiquidity(false)} />
      )}
    </div>
    </ErrorBoundary>
  );
}

export default App;