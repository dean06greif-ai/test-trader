import React from 'react';
import { CheckCircle, Circle, Info, TrendUp, TrendDown } from '@phosphor-icons/react';
import './SignalPanel.css';

const SignalPanel = ({ symbol, ruleState, latestSignal, strategyMeta, onShowInChart }) => {
  const rules = ruleState?.rules || [];
  const bias = ruleState?.bias;
  const longCount = ruleState?.long_count || 0;
  const shortCount = ruleState?.short_count || 0;
  const total = ruleState?.rules_total || rules.length;
  const allLong = rules.length > 0 && rules.every(r => r.long);
  const allShort = rules.length > 0 && rules.every(r => r.short);

  const circleFor = (rule) => {
    if (rule.long) return <CheckCircle size={22} weight="fill" className="text-long" />;
    if (rule.short) return <CheckCircle size={22} weight="fill" className="text-short" />;
    return <Circle size={22} className="text-muted" />;
  };

  return (
    <div className="signal-panel" data-testid="signal-panel">
      <div className="panel-header">
        <div>
          <h3>{(strategyMeta?.name || 'STRATEGIE').toUpperCase()}</h3>
          <div className="panel-subtitle">Live Signal · {symbol?.replace('USDT', '')}</div>
        </div>
        <div className="panel-header-right">
          {bias && (
            <div className={`bias-pill ${bias === 'LONG' ? 'bias-long' : 'bias-short'}`} data-testid="bias-pill">
              {bias === 'LONG' ? <TrendUp size={13} weight="bold" /> : <TrendDown size={13} weight="bold" />}
              {bias} {bias === 'LONG' ? longCount : shortCount}/{total}
            </div>
          )}
          {strategyMeta && <span className="tf-badge"><Info size={12} weight="bold" /> {strategyMeta.timeframe}</span>}
        </div>
      </div>

      {(allLong || allShort) && (
        <div className={`all-aligned ${allLong ? 'aligned-long' : 'aligned-short'}`} data-testid="all-aligned-banner">
          ALLE REGELN {allLong ? 'LONG' : 'SHORT'} · VOLLES SIGNAL
        </div>
      )}

      {ruleState?.indicators?.phase && (
        <div className="pbd-phase-bar" data-testid="pbd-phase-bar">
          {['P', 'B', 'D'].map((ph) => {
            const order = { 'P': 1, 'B': 2, 'D': 3 };
            const cur = order[ruleState.indicators.phase] || 0;
            const active = order[ph] <= cur;
            const isNow = ruleState.indicators.phase === ph;
            const labels = { P: 'Purge', B: 'Break', D: 'Displacement' };
            return (
              <div
                key={ph}
                className={`pbd-phase-step ${active ? 'done' : ''} ${isNow ? 'now' : ''}`}
                data-testid={`pbd-phase-${ph}`}
              >
                <span className="pbd-phase-dot">{ph}</span>
                <span className="pbd-phase-label">{labels[ph]}</span>
              </div>
            );
          })}
          {typeof ruleState.indicators.confluence === 'number' && (
            <div className="pbd-confluence" data-testid="pbd-confluence" title="Confluence-Score (A-Setup >= 55)">
              <span className="pbd-conf-label">CONF</span>
              <span className={`pbd-conf-value mono ${ruleState.indicators.confluence >= 55 ? 'good' : ''}`}>
                {ruleState.indicators.confluence}
              </span>
            </div>
          )}
        </div>
      )}

      {rules.length > 0 ? (
        <div className="rules-grid">
          {rules.map(rule => (
            <div key={rule.id} className={`rule-item ${rule.long ? 'rule-long' : rule.short ? 'rule-short' : ''}`} data-testid={`rule-${rule.id}`}>
              <div className="rule-icon">{circleFor(rule)}</div>
              <div className="rule-content">
                <div className="rule-label">
                  {rule.label}
                  {rule.timeframe && (
                    <span className="rule-tf-badge" data-testid={`rule-tf-badge-${rule.id}`}
                      title={`Multi-Timeframe-Filter: Diese Regel wird auf dem ${rule.timeframe}-Timeframe geprüft`}>
                      @{rule.timeframe}
                    </span>
                  )}
                </div>
                <div className="rule-description">{rule.description}</div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="rules-loading text-muted">Regeln werden geladen...</div>
      )}

      {ruleState?.indicators && (
        <div className="indicator-strip">
          {ruleState.indicators.phase ? (
            <span>PHASE <b className="mono">{ruleState.indicators.phase}</b></span>
          ) : (
            <span>RSI <b className="mono">{ruleState.indicators.rsi}</b></span>
          )}
          <span>PREIS <b className="mono">${ruleState.indicators.price}</b></span>
        </div>
      )}

      {latestSignal && (
        <div
          className={`current-signal clickable ${latestSignal.type === 'LONG' ? 'signal-long' : 'signal-short'}`}
          data-testid="latest-signal"
          onClick={() => onShowInChart && onShowInChart(latestSignal)}
          title="Anklicken: Signal mit Entry/SL/TP-Linien und allen Regeln (inkl. Timeframe je Regel) im Chart anzeigen"
        >
          <div className="signal-header">
            <span className={`badge ${latestSignal.type === 'LONG' ? 'badge-long' : 'badge-short'}`}>
              {latestSignal.signal_class === 'PRE_SIGNAL' ? 'PRE-' : ''}{latestSignal.type} SIGNAL
            </span>
            <span className="mono text-muted" style={{ fontSize: '11px' }}>
              {new Date(latestSignal.timestamp).toLocaleTimeString('de-DE', { timeZone: 'Europe/Berlin' })}
            </span>
          </div>
          <div className="signal-prices">
            <div className="price-item"><span className="price-label">ENTRY</span><span className="price-value mono">${latestSignal.entry_price}</span></div>
            <div className="price-item"><span className="price-label">STOP LOSS</span><span className="price-value mono text-short">${latestSignal.stop_loss}</span></div>
            <div className="price-item"><span className="price-label">TP1</span><span className="price-value mono text-long">${latestSignal.take_profit_1}</span></div>
            <div className="price-item"><span className="price-label">TP FULL</span><span className="price-value mono text-long">${latestSignal.take_profit_full}</span></div>
          </div>
          <div className="signal-crv"><span className="crv-label">CRV</span><span className="crv-value mono">{latestSignal.crv}</span></div>
        </div>
      )}

      {!latestSignal && rules.length > 0 && (
        <div className="no-signal-hint text-muted">Kein aktives Signal heute · warte auf Regel-Alignment</div>
      )}
    </div>
  );
};

export default SignalPanel;
