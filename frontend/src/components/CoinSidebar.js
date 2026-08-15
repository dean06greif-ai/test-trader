import React, { useState } from 'react';
import { TrendUp, TrendDown, Circle, Bell, BellSlash, Lightning, CaretDown, CaretRight } from '@phosphor-icons/react';
import useInstruments, { assetLabel } from '../hooks/useInstruments';
import './CoinSidebar.css';

const OPEN_STORE_KEY = 'krypto_sidebar_open_groups';
const DEFAULT_OPEN = ['TOP 10 COINS'];

const readOpen = () => {
  try {
    const raw = localStorage.getItem(OPEN_STORE_KEY);
    return raw ? JSON.parse(raw) : DEFAULT_OPEN;
  } catch { return DEFAULT_OPEN; }
};

const CoinSidebar = ({ selectedCoin, onSelectCoin, performance, notifications = {}, onToggleNotification,
                       ruleStates = {}, selectedStrategy, autotradeCoins = {}, onToggleAutoTrade }) => {
  const { groups } = useInstruments();
  const [openGroups, setOpenGroups] = useState(readOpen);

  const toggleGroup = (name) => setOpenGroups(prev => {
    const next = prev.includes(name) ? prev.filter(n => n !== name) : [...prev, name];
    try { localStorage.setItem(OPEN_STORE_KEY, JSON.stringify(next)); } catch { /* ignore */ }
    return next;
  });

  const getPerf = (s) => (performance || []).find(p => p.symbol === s) || {};

  const renderItem = (meta) => {
    const coin = meta.symbol;
    const perf = getPerf(coin);
    const isSelected = coin === selectedCoin;
    const hasSignals = perf.total_signals > 0;
    const notifyOn = notifications[coin] !== false;
    const state = ruleStates[coin]?.[selectedStrategy];
    const bias = state?.bias;
    const autoOn = autotradeCoins[coin]?.enabled;

    let dotClass = 'coin-indicator';
    if (bias === 'LONG') dotClass = 'coin-indicator-long';
    else if (bias === 'SHORT') dotClass = 'coin-indicator-short';
    else if (hasSignals) dotClass = 'coin-indicator-active';

    return (
      <div key={coin} className={`coin-item ${isSelected ? 'coin-item-selected' : ''}`} onClick={() => onSelectCoin(coin)} data-testid={`coin-item-${coin}`}>
        <div className="coin-header">
          <div className="coin-name">
            <Circle size={8} weight="fill" className={dotClass} />
            <span className="mono">{assetLabel(coin)}</span>
            {meta.tradable === false && (
              <span className="coin-paper-badge" title="Kein Bitunix-Kontrakt – Analyse, Backtest und Paper-Trading" data-testid={`coin-paper-badge-${coin}`}>P</span>
            )}
            {state && (
              <span className="coin-bias-count mono">{Math.max(state.long_count || 0, state.short_count || 0)}/{state.rules_total || 0}</span>
            )}
          </div>
          <div className="coin-header-right">
            <button
              className={`auto-toggle ${autoOn ? 'auto-on' : ''}`}
              onClick={(e) => { e.stopPropagation(); onToggleAutoTrade && onToggleAutoTrade(coin, !autoOn); }}
              title={autoOn ? 'Auto-Trade AKTIV – klicken zum Deaktivieren' : 'Auto-Trade INAKTIV – klicken zum Aktivieren'}
              data-testid={`autotrade-btn-${coin}`}
            >
              <Lightning size={14} weight={autoOn ? 'fill' : 'regular'} />
            </button>
            <button
              className={`notify-toggle ${notifyOn ? 'notify-on' : 'notify-off'}`}
              onClick={(e) => { e.stopPropagation(); onToggleNotification && onToggleNotification(coin); }}
              title={notifyOn ? 'Alerts an' : 'Alerts aus'}
              data-testid={`notify-toggle-${coin}`}
            >
              {notifyOn ? <Bell size={14} weight="fill" /> : <BellSlash size={14} />}
            </button>
          </div>
        </div>

        {hasSignals && (
          <div className="coin-stats">
            <div className="coin-stat"><TrendUp size={12} className="text-long" /><span className="mono text-secondary">{perf.long_signals || 0}</span></div>
            <div className="coin-stat"><TrendDown size={12} className="text-short" /><span className="mono text-secondary">{perf.short_signals || 0}</span></div>
            <div className="coin-stat"><span className="text-muted">WR</span><span className="mono text-secondary">{perf.win_rate?.toFixed(0) || 0}%</span></div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="coin-sidebar" data-testid="coin-sidebar">
      <div className="sidebar-header"><h3>MARKETS</h3><div className="sidebar-subtitle">Live Scanner</div></div>
      {groups.map(group => {
        const items = group.symbols || [];
        // Fix: Gruppen (z.B. TOP 10 COINS) lassen sich jetzt immer manuell
        // einklappen – vorher hielt `selectedCoin` die Gruppe zwangsweise offen.
        const isOpen = openGroups.includes(group.name);
        return (
          <div key={group.name} className="coin-group">
            <button className="coin-group-title" onClick={() => toggleGroup(group.name)} data-testid={`group-${group.name}`}>
              {isOpen ? <CaretDown size={11} weight="bold" /> : <CaretRight size={11} weight="bold" />}
              <span>{group.name}</span>
              <span className="coin-group-count mono">{items.length}</span>
            </button>
            {isOpen && <div className="coin-list">{items.map(renderItem)}</div>}
          </div>
        );
      })}
    </div>
  );
};

export default CoinSidebar;
