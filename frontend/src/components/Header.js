import React, { useState, useEffect, useCallback } from 'react';
import { Clock, Gear, ChartLineUp, Wallet, TrendUp, TrendDown, Lock, LockOpen, Trophy, ClockCounterClockwise, MagicWand, ChartScatter, Drop, BellRinging, Flask } from '@phosphor-icons/react';
import { authHeaders } from '../auth';
import CapitalModal from './CapitalModal';
import './Header.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const BalanceWidget = () => {
  const [bal, setBal] = useState(null);
  // Statt eines gemeinsamen Modal-States pflegen wir den Scope-Lock explizit.
  // null = geschlossen, 'live' oder 'paper' = geöffnet mit fixem Scope.
  const [capitalScope, setCapitalScope] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await fetch(`${API_URL}/api/autotrade/balance`).then(r => r.json());
      setBal(d);
    } catch (_) { /* ignore */ }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, [load]);

  if (!bal) {
    // Skeleton während des initialen Loads: identisches Layout, damit der
    // Header nicht "nachspringt", sobald die Balance-Daten eintreffen.
    return (
      <div className="balance-widget-wrapper" data-testid="bitunix-balance-skeleton">
        <div className="balance-widget bw-skeleton" aria-busy="true">
          <div className="bw-mode live">
            <Wallet size={14} weight="fill" />
            LIVE
          </div>
          <div className="bw-stack">
            <span className="bw-usdt-label">USDT</span>
            <span className="bw-primary-value mono">—</span>
            <span className="bw-sub-line">
              <span className="bw-sub-label">frei</span>
              <span className="mono">—</span>
            </span>
          </div>
        </div>
        <div className="paper-overlay bw-skeleton" aria-busy="true">
          <div className="paper-overlay-mode">
            <Wallet size={12} weight="fill" />
            PAPER
          </div>
          <div className="overlay-stack">
            <span className="bw-usdt-label">PnL</span>
            <div className="paper-overlay-pnl">
              <span className="bw-primary-value bw-value-muted mono">—</span>
            </div>
            <span className="bw-sub-line">
              <span className="bw-sub-label">frei</span>
              <span className="mono">—</span>
            </span>
          </div>
        </div>
      </div>
    );
  }
  const isLive = bal.mode === 'live';
  const pnl = bal.realized_pnl || 0;
  const pnlPos = pnl >= 0;

  // Paper overlay data
  const paperPnl = bal.paper_pnl ?? null;
  const paperPnlPos = (paperPnl || 0) >= 0;

  const liveAlloc = bal.allocation?.live;
  const paperAlloc = bal.allocation?.paper;
  const alloc = isLive ? liveAlloc : paperAlloc;

  // Hauptwidget öffnet immer mit dem aktuellen Modus als gesperrtem Scope.
  const openMainCapital = () => setCapitalScope(isLive ? 'live' : 'paper');
  const openPaperCapital = (e) => {
    e.stopPropagation();
    setCapitalScope('paper');
  };
  const openLiveCapital = (e) => {
    e.stopPropagation();
    setCapitalScope('live');
  };

  return (
    <div className="balance-widget-wrapper">
      {/* Live Badge - im Paper-Modus sichtbar (LINKS, Konvention: Live immer links). */}
      {!isLive && (
        <div className="live-overlay bw-clickable" data-testid="live-overlay"
          onClick={openLiveCapital}
          title="Live-Kapital anpassen"
          role="button" tabIndex={0}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') openLiveCapital(e); }}>
          <div className="live-overlay-mode">
            <Wallet size={12} weight="fill" />
            LIVE
          </div>
          <div className="overlay-stack">
            <span className="bw-usdt-label">USDT</span>
            <span className="bw-primary-value mono" data-testid="live-overlay-balance">
              {bal.margin_balance != null ? Number(bal.margin_balance).toFixed(2) : '—'}
            </span>
            {liveAlloc?.free != null && (
              <span className="bw-sub-line" data-testid="live-overlay-free">
                <span className="bw-sub-label">frei</span>
                <span className="mono">{Number(liveAlloc.free).toFixed(2)}</span>
              </span>
            )}
          </div>
        </div>
      )}

      <div className="balance-widget bw-clickable" data-testid="bitunix-balance-widget"
        onClick={openMainCapital}
        title={isLive ? 'Live-Kapital anpassen' : 'Paper-Kapital anpassen'}
        role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') openMainCapital(); }}>
        <div className={`bw-mode ${isLive ? 'live' : 'paper'}`} data-testid="bw-mode">
          <Wallet size={14} weight="fill" />
          {isLive ? 'LIVE' : 'PAPER'}
        </div>
        {isLive ? (
          bal.bitunix_configured ? (
            <div className="bw-stack" data-testid="bw-live-stack">
              <span className="bw-usdt-label">USDT</span>
              <span className="bw-primary-value mono" data-testid="bw-total">
                {bal.margin_balance != null ? Number(bal.margin_balance).toFixed(2) : (bal.bitunix_error ? 'API-Fehler' : '—')}
              </span>
              {alloc?.free != null ? (
                <span className="bw-sub-line" data-testid="bw-alloc">
                  <span className="bw-sub-label">frei</span>
                  <span className="mono">{Number(alloc.free).toFixed(2)}</span>
                </span>
              ) : (
                <span className="bw-sub-line" data-testid="bw-free">
                  <span className="bw-sub-label">Kapital</span>
                  <span className="mono">{bal.available != null ? Number(bal.available).toFixed(2) : '—'}</span>
                </span>
              )}
            </div>
          ) : (
            <div className="bw-item bw-warn" data-testid="bw-unconfigured">Bitunix nicht konfiguriert</div>
          )
        ) : (
          <div className="bw-stack" data-testid="bw-paper-stack">
            <span className="bw-usdt-label">PnL</span>
            <span className={`bw-primary-value mono ${pnlPos ? 'pos' : 'neg'}`}>
              {pnlPos ? <TrendUp size={13} weight="bold" /> : <TrendDown size={13} weight="bold" />}
              {pnl.toFixed(2)}
            </span>
            {alloc?.free != null && (
              <span className="bw-sub-line" data-testid="bw-alloc">
                <span className="bw-sub-label">frei</span>
                <span className="mono">{Number(alloc.free).toFixed(2)}</span>
              </span>
            )}
          </div>
        )}
      </div>

      {/* Paper Badge - im Live-Modus sichtbar, klickbar für Paper-Kapital. */}
      {isLive && (
        <div className="paper-overlay bw-clickable" data-testid="paper-overlay"
          onClick={openPaperCapital}
          title="Paper-Kapital anpassen"
          role="button" tabIndex={0}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') openPaperCapital(e); }}>
          <div className="paper-overlay-mode">
            <Wallet size={12} weight="fill" />
            PAPER
          </div>
          <div className="overlay-stack">
            <span className="bw-usdt-label">PnL</span>
            <div className="paper-overlay-pnl">
              {paperPnl != null && paperPnl !== 0 ? (
                <span className={`bw-primary-value mono ${paperPnlPos ? 'pos' : 'neg'}`}>
                  {paperPnlPos ? <TrendUp size={13} weight="bold" /> : <TrendDown size={13} weight="bold" />}
                  {(paperPnl || 0).toFixed(2)}
                </span>
              ) : (
                <span className="bw-primary-value bw-value-muted mono">—</span>
              )}
            </div>
            {paperAlloc?.free != null && (
              <span className="bw-sub-line" data-testid="paper-overlay-free">
                <span className="bw-sub-label">frei</span>
                <span className="mono">{Number(paperAlloc.free).toFixed(2)}</span>
              </span>
            )}
          </div>
        </div>
      )}

      {/* Live Badge wurde nach LINKS verschoben (siehe oben) – Konvention: Live links, Paper rechts. */}

      {capitalScope && (
        <CapitalModal
          lockedScope={capitalScope}
          onClose={() => setCapitalScope(null)}
          onSaved={load}
        />
      )}
    </div>
  );
};

const NotificationBell = () => {
  const [items, setItems] = useState([]);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState('unread');
  const boxRef = React.useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const close = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const d = await fetch(`${API_URL}/api/notifications?filter=all&limit=100`).then(r => r.json());
        if (stop) return;
        // Keine Popups mehr: Meldungen sind ausschließlich über die Glocke einsehbar.
        setItems(d.notifications || []);
      } catch (_) { /* ignore */ }
    };
    load();
    const iv = setInterval(load, 60000);
    return () => { stop = true; clearInterval(iv); };
  }, []);

  const unread = items.filter(n => !n.read);
  const read = items.filter(n => n.read);
  const shown = view === 'unread' ? unread : read;

  const markRead = async () => {
    if (!unread.length) return;
    try {
      await fetch(`${API_URL}/api/notifications/read`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ ids: unread.map(n => n.id) }),
      });
    } catch (_) { /* ignore */ }
    const ts = new Date().toISOString();
    setItems(prev => prev.map(n => (n.read ? n : { ...n, read: true, read_at: ts })));
  };
  const tabStyle = (active) => ({
    background: active ? 'rgba(255,255,255,0.12)' : 'transparent',
    border: '1px solid rgba(255,255,255,0.15)', borderRadius: 6,
    color: 'inherit', cursor: 'pointer', fontSize: 11, padding: '2px 8px',
  });
  return (
    <span style={{ position: 'relative', display: 'inline-flex' }} ref={boxRef}>
      <button className="icon-btn" onClick={() => setOpen(v => !v)} data-testid="header-notifications-btn"
        title="Benachrichtigungen anzeigen"
        style={{ position: 'relative', color: unread.length ? '#FF3366' : undefined }}>
        <BellRinging size={18} weight={unread.length ? 'fill' : 'regular'} />
        {unread.length > 0 && (
          <span style={{ position: 'absolute', top: -4, right: -4, background: '#FF3366', color: '#fff', borderRadius: 8, fontSize: 10, padding: '0 4px' }}>{unread.length}</span>
        )}
      </button>
      {open && (
        <div className="notif-dropdown" data-testid="header-notifications-dropdown">
          <div className="notif-dd-head">
            <span style={{ display: 'inline-flex', gap: 6 }}>
              <button style={tabStyle(view === 'unread')} onClick={() => setView('unread')}
                data-testid="header-notifications-tab-unread">
                Ungelesen ({unread.length})
              </button>
              <button style={tabStyle(view === 'read')} onClick={() => setView('read')}
                data-testid="header-notifications-tab-read">
                Gelesen ({read.length})
              </button>
            </span>
            {view === 'unread' && unread.length > 0 && (
              <button className="notif-dd-read" onClick={markRead} data-testid="header-notifications-mark-read">
                Alle als gelesen
              </button>
            )}
          </div>
          <div className="notif-dd-list">
            {!shown.length && (
              <div className="notif-dd-item" data-testid="header-notifications-empty">
                <span>{view === 'unread' ? 'Keine ungelesenen Mitteilungen.' : 'Keine gelesenen Mitteilungen.'}</span>
              </div>
            )}
            {shown.map(n => (
              <div className="notif-dd-item" key={n.id}>
                <b>{n.title}</b>
                <span>{n.message}</span>
                {n.meta && (n.meta.role || n.meta.provider || n.meta.reason) && (
                  <span className="notif-dd-meta">
                    {n.meta.role ? <>Assistent: <b>{n.meta.role}</b></> : null}
                    {n.meta.provider ? <>{n.meta.role ? ' · ' : ''}Modell: {n.meta.provider}/{n.meta.model}</> : null}
                    {n.meta.reason ? <>{(n.meta.role || n.meta.provider) ? ' · ' : ''}Ursache: {n.meta.reason}</> : null}
                    {n.meta.fallback ? <> · Fallback: {n.meta.fallback}</> : null}
                    {n.meta.detail ? <> · {n.meta.detail}</> : null}
                  </span>
                )}
                <em>
                  {n.source ? `${n.source} · ` : ''}
                  {(n.created_at || n.ts)
                    ? new Date(n.created_at || n.ts).toLocaleString('de-DE', { timeZone: 'Europe/Berlin' })
                    : ''}
                </em>
              </div>
            ))}
          </div>
          {view === 'read' && (
            <div style={{ fontSize: 10, opacity: 0.6, padding: '4px 10px 8px' }}
              data-testid="header-notifications-retention-hint">
              Gelesene Mitteilungen werden nach 7 Tagen automatisch gelöscht.
            </div>
          )}
        </div>
      )}
    </span>
  );
};

const ToolsMenu = ({ onBacktestClick, onOptimizerClick, onRegimeLabClick }) => {
  const [open, setOpen] = useState(false);
  const ref = React.useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const close = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const items = [
    { label: 'Backtester', desc: 'Historische Daten, alle Timeframes',
      Icon: ClockCounterClockwise, onClick: onBacktestClick, testid: 'tools-menu-backtester' },
    { label: 'Strategie-Optimizer', desc: 'Parameter & Discovery',
      Icon: MagicWand, onClick: onOptimizerClick, testid: 'tools-menu-optimizer' },
    { label: 'Regime-Lab', desc: 'Marktphasen analysieren & prüfen',
      Icon: ChartScatter, onClick: onRegimeLabClick, testid: 'tools-menu-regime-lab' },
  ];

  return (
    <div className="tools-menu" ref={ref}>
      <button
        className={`btn ${open ? 'tools-menu-open' : ''}`}
        onClick={() => setOpen(v => !v)}
        title="Analyse-Tools: Backtester · Strategie-Optimizer · Regime-Lab"
        data-testid="tools-menu-button"
      >
        <Flask size={20} weight="bold" />
      </button>
      {open && (
        <div className="tools-menu-dropdown" data-testid="tools-menu-dropdown">
          {items.map(({ label, desc, Icon, onClick, testid }) => (
            <button
              key={testid}
              className="tools-menu-item"
              data-testid={testid}
              onClick={() => { setOpen(false); onClick && onClick(); }}
            >
              <Icon size={16} weight="bold" />
              <span className="tools-menu-text">
                <b>{label}</b>
                <small>{desc}</small>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

const Header = ({ sessionActive, onSettingsClick, currentSession, customSessions, activeStrategy, adminAuthed, onAdminClick, onCompareClick, onBacktestClick, onOptimizerClick, onRegimeLabClick, onLiquidityClick }) => {
  const [currentTime, setCurrentTime] = useState(new Date());

  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentTime(new Date());
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const formatTime = (date) => {
    return date.toLocaleTimeString('de-DE', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      timeZone: 'Europe/Berlin',
    });
  };

  const is24_7 = !customSessions || customSessions.length === 0;
  const enabledSessions = (customSessions || []).filter(s => s.enabled !== false);

  return (
    <header className="header" data-testid="main-header">
      <div className="header-left">
        <div className="header-brand">
          <ChartLineUp size={28} weight="bold" className="brand-icon" />
          <div className="header-brand-text">
            <h1 className="header-title">CRYPTO SCANNER</h1>
            {activeStrategy && (
              <div className="header-strategy" data-testid="active-strategy-display">
                🎯 {activeStrategy.name}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="header-right">
        <NotificationBell />
        {/* Uhrzeit + Session-Badge als eigener Block NEBEN den Labels (keine Überlagerung mehr) */}
        <div className="header-session" data-testid="header-session">
          <div className="session-status">
            <Clock size={18} weight="bold" />
            <span className="mono">{formatTime(currentTime)}</span>
            <span className={`badge ${sessionActive ? 'badge-active' : 'badge-inactive'}`} data-testid="session-status-badge">
              {sessionActive
                ? (currentSession ? `${currentSession.toUpperCase()} · ACTIVE` : 'TRADING ACTIVE')
                : 'OUTSIDE SESSIONS'}
            </span>
          </div>
          {!is24_7 && (
            <div className="session-times">
              {enabledSessions.length === 0 ? (
                <span className="text-muted">Keine aktiven Sessions</span>
              ) : (
                enabledSessions.map((s, i) => (
                  <span key={i} className="text-muted">
                    {i > 0 && <span style={{ margin: '0 4px' }}>|</span>}
                    {s.name}: {s.start}-{s.end}
                  </span>
                ))
              )}
            </div>
          )}
        </div>
        <BalanceWidget />
        <button className="btn" onClick={onCompareClick} title="Strategie-Vergleich" data-testid="compare-strategies-button">
          <Trophy size={20} weight="bold" />
        </button>
        <ToolsMenu
          onBacktestClick={onBacktestClick}
          onOptimizerClick={onOptimizerClick}
          onRegimeLabClick={onRegimeLabClick}
        />
        <button className="btn" onClick={onLiquidityClick} title="Liquidität (Liquidations-Heatmap & Liquidity Levels)" data-testid="liquidity-button">
          <Drop size={20} weight="bold" />
        </button>
        <button
          className={`btn btn-admin ${adminAuthed ? 'is-admin' : ''}`}
          onClick={onAdminClick}
          title={adminAuthed ? 'Admin abmelden' : 'Admin-Login'}
          aria-label={adminAuthed ? 'Admin abmelden' : 'Admin-Login'}
          data-testid="admin-lock-button"
        >
          {adminAuthed
            ? <LockOpen size={20} weight="bold" />
            : <Lock size={20} weight="bold" />}
        </button>
        <button className="btn" onClick={onSettingsClick} data-testid="settings-button">
          <Gear size={20} weight="bold" />
        </button>
      </div>
    </header>
  );
};

export default Header;
