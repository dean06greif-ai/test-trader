import React from 'react';
import { Plus, Gear, Lightning, Bell, BellSlash } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import './StrategyTabs.css';

const StrategyTabs = ({ 
  strategies, 
  enabledIds, 
  selected, 
  signalsEnabled, 
  strategyOverrides,
  strategyCoinConfigs = {},
  selectedCoin,
  onSelect, 
  onToggleSignals, 
  onManage, 
  onEditParams,
  onOpenStrategyAutoTrade 
}) => {
  // Auto-Trade-Status als Sortier-Prio: LIVE (0) > PAPER (1) > OFF/none (2)
  // Muss GENAU der Logik von getAutoTradeStatus() folgen, damit die
  // Sortierung mit dem angezeigten Blitz-Icon konsistent ist.
  const tradePriority = (strategyId) => {
    const perCoin = strategyCoinConfigs?.[strategyId]?.[selectedCoin];
    if (perCoin && perCoin.mode) {
      // Per-Coin-Config ist maßgeblich — auch ein explizites 'off'
      if (perCoin.mode === 'off' || perCoin.enabled === false) return 2;
      return perCoin.mode === 'live' ? 0 : 1;
    }
    const override = strategyOverrides?.[strategyId];
    if (override && override.enabled && override.mode && override.mode !== 'off') {
      return override.mode === 'live' ? 0 : 1;
    }
    return 2;
  };

  // Strategien so sortieren, dass zuerst LIVE-Trades, dann PAPER-Trades,
  // dann Strategien ohne Trades kommen. Innerhalb einer Gruppe bleibt die
  // ursprüngliche Reihenfolge (aus enabledIds) erhalten.
  const tabs = enabledIds
    .map((id, idx) => ({ strat: strategies.find(s => s.id === id), idx }))
    .filter(x => x.strat)
    .sort((a, b) => {
      const pa = tradePriority(a.strat.id);
      const pb = tradePriority(b.strat.id);
      if (pa !== pb) return pa - pb;
      return a.idx - b.idx;
    })
    .map(x => x.strat);

  // Get auto-trade status badge for a strategy — reflects the mode of the
  // CURRENTLY SELECTED COIN (per-strategy-per-coin config takes priority,
  // falls back to strategy-level override for backwards compatibility).
  const getAutoTradeStatus = (strategyId) => {
  const perCoin = strategyCoinConfigs?.[strategyId]?.[selectedCoin];
  if (perCoin && perCoin.mode) {
    // Per-Coin-Config ist maßgeblich — auch ein explizites 'off'!
    if (perCoin.mode === 'off' || perCoin.enabled === false) {
      return null; // Blitz leer, kein Fallback auf Strategy-Override
    }
    return perCoin.mode === 'live' ? 'L' : 'P';
  }
  const override = strategyOverrides?.[strategyId];
  if (!override || !override.enabled || override.mode === 'off') {
    return null;
  }
  return override.mode === 'live' ? 'L' : 'P';
};

  // Check if signals are enabled for a strategy (Bell icon state)
  const isSignalsEnabled = (strategyId) => {
    const override = strategyOverrides?.[strategyId];
    // Default to signalsEnabled state if no override
    if (override && override.signals_enabled !== undefined) {
      return override.signals_enabled;
    }
    return signalsEnabled[strategyId] !== false;
  };

  return (
    <div className="strategy-tabs-bar" data-testid="strategy-tabs">
      <div className="strategy-tabs-scroll">
        {tabs.map(strat => {
          const isActive = selected === strat.id;
          const atStatus = getAutoTradeStatus(strat.id);
          // Glocke ist an den Blitz gekoppelt: Signale kann es nur geben, wenn
          // Auto-Trade (Paper/Live) aktiv ist – Blitz aus => Glocke zwingend aus.
          const sigOn = isSignalsEnabled(strat.id) && atStatus !== null;
          
          return (
            <div
              key={strat.id}
              className={`strategy-tab ${isActive ? 'strategy-tab-active' : ''}`}
              onClick={() => onSelect(strat.id)}
              data-testid={`strategy-tab-${strat.id}`}
            >
              <span className="strategy-tab-name">{strat.name}</span>
              {strat.is_custom && <span className="strategy-tab-custom">C</span>}
              
              {/* Lightning Icon - Auto-Trade Status: gelb=LIVE, blau=PAPER, leer=AUS */}
              <button
                className={`strategy-tab-trade ${atStatus ? 'active' : ''} ${atStatus === 'L' ? 'live' : ''}`}
                onClick={(e) => { e.stopPropagation(); onOpenStrategyAutoTrade && onOpenStrategyAutoTrade(strat.id); }}
                title={atStatus === 'L' ? 'Auto-Trade LIVE (klick zum Konfigurieren)' : atStatus === 'P' ? 'Auto-Trade PAPER (klick zum Konfigurieren)' : 'Auto-Trade AUS (klick zum Konfigurieren)'}
                data-testid={`strategy-trade-toggle-${strat.id}`}
              >
                <Lightning
                  size={13}
                  weight={atStatus ? 'fill' : 'regular'}
                  color={atStatus === 'L' ? '#FFD60A' : atStatus === 'P' ? '#00A8FF' : '#5C6070'}
                />
                {atStatus === 'P' && <span className="strategy-trade-badge">P</span>}
              </button>

              {/* Bell Icon - Signal Notifications Toggle (nur möglich wenn Blitz an) */}
              <button
                className={`strategy-tab-bell ${sigOn ? 'on' : 'off'}`}
                onClick={(e) => {
                  e.stopPropagation();
                  if (atStatus === null) {
                    toast.info('Glocke nur möglich, wenn Auto-Trade (Blitz) aktiv ist');
                    return;
                  }
                  onToggleSignals(strat.id);
                }}
                title={atStatus === null
                  ? 'Glocke gesperrt – erst Auto-Trade (Blitz) aktivieren'
                  : sigOn ? 'Signale AN (klick zum Ausschalten)' : 'Signale AUS (klick zum Einschalten)'}
                data-testid={`strategy-bell-toggle-${strat.id}`}
              >
                {sigOn ? <Bell size={13} weight="fill" /> : <BellSlash size={13} weight="regular" />}
              </button>
            </div>
          );
        })}
      </div>
      <div className="strategy-tabs-actions">
        <button className="strategy-tab-action" onClick={onEditParams} title="Parameter der aktiven Strategie" data-testid="edit-strategy-params-btn">
          <Gear size={16} weight="bold" />
        </button>
        <button className="strategy-tab-action add" onClick={onManage} title="Strategien verwalten / neue erstellen" data-testid="manage-strategies-btn">
          <Plus size={16} weight="bold" />
        </button>
      </div>
    </div>
  );
};

export default StrategyTabs;
