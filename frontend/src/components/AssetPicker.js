import React from 'react';
import useInstruments, { assetLabel } from '../hooks/useInstruments';
import './AssetPicker.css';

/**
 * Gruppierte Asset-Auswahl (Krypto / Resources / Indices / Forex).
 * Nutzt die Klassen der jeweiligen Ansicht weiter (chipClass), damit das
 * bestehende Design von Backtester und Optimizer unverändert bleibt.
 */
const AssetPicker = ({ selected = [], onToggle, chipClass = 'bt-chip',
                       testIdPrefix = 'bt-coin', extraClass, renderExtra }) => {
  const { groups } = useInstruments();

  return (
    <div className="asset-picker" data-testid={`${testIdPrefix}-picker`}>
      {groups.map(group => (
        <div className="asset-picker-group" key={group.name}>
          <div className="asset-picker-title">{group.name}</div>
          <div className="asset-picker-chips">
            {(group.symbols || []).map(meta => {
              const s = meta.symbol;
              const on = selected.includes(s);
              const hint = meta.tradable === false
                ? `${meta.name} · kein Bitunix-Kontrakt (Backtest/Paper), Historie ca. ${meta.max_hist_days} Tage`
                : `${meta.name} · Historie ca. ${meta.max_hist_days} Tage`;
              return (
                <button key={s} type="button" title={hint}
                  className={`${chipClass} ${on ? 'on' : ''} ${extraClass ? extraClass(s) : ''}`}
                  onClick={() => onToggle(s)}
                  data-testid={`${testIdPrefix}-${s}`}>
                  {assetLabel(s)}
                  {renderExtra ? renderExtra(s) : null}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
};

export default AssetPicker;
