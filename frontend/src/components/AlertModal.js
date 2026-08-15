import React, { useEffect } from 'react';
import { X, TrendUp, TrendDown } from '@phosphor-icons/react';
import './AlertModal.css';

const AlertModal = ({ signal, onClose }) => {
  useEffect(() => {
    // Auto-close after 10 seconds
    const timer = setTimeout(() => {
      onClose();
    }, 10000);

    return () => clearTimeout(timer);
  }, [onClose]);

  const getCoinName = (symbol) => {
    return symbol.replace('USDT', '');
  };

  const isLong = signal.type === 'LONG';

  return (
    <div className="alert-modal-overlay" data-testid="alert-modal" onClick={onClose}>
      <div className={`alert-modal ${isLong ? 'alert-long' : 'alert-short'}`} onClick={(e) => e.stopPropagation()}>
        <button className="alert-close" onClick={onClose} data-testid="alert-close-button">
          <X size={24} weight="bold" />
        </button>

        <div className="alert-icon">
          {isLong ? (
            <TrendUp size={64} weight="bold" className="text-long" />
          ) : (
            <TrendDown size={64} weight="bold" className="text-short" />
          )}
        </div>

        <h2 className="alert-title">{signal.type} SIGNAL DETECTED!</h2>
        <div className="alert-coin mono">{getCoinName(signal.symbol)}</div>
        {signal.strategy_name && (
          <div className="alert-strategy" data-testid="alert-strategy">STRATEGIE: {signal.strategy_name}</div>
        )}

        <div className="alert-details">
          <div className="alert-detail-item">
            <span className="detail-label">ENTRY PREIS</span>
            <span className="detail-value mono">${signal.entry_price}</span>
          </div>
          <div className="alert-detail-item">
            <span className="detail-label">STOP LOSS</span>
            <span className="detail-value mono text-short">${signal.stop_loss}</span>
          </div>
          <div className="alert-detail-item">
            <span className="detail-label">TAKE PROFIT 1 (40%)</span>
            <span className="detail-value mono text-long">${signal.take_profit_1}</span>
          </div>
          <div className="alert-detail-item">
            <span className="detail-label">TAKE PROFIT FULL</span>
            <span className="detail-value mono text-long">${signal.take_profit_full}</span>
          </div>
        </div>

        <div className="alert-crv">
          <span className="crv-label">COST-REWARD RATIO</span>
          <span className="crv-value mono">{signal.crv}</span>
        </div>

        <div className="alert-action">
          <div className="action-text">HANDELN SIE INNERHALB VON 2 KERZEN!</div>
          <div className="action-subtext">(≈ 2 Minuten)</div>
        </div>
      </div>
    </div>
  );
};

export default AlertModal;
