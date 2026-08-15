import React, { useEffect, useState } from 'react';
import { X } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import './NewTradeModal.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

/**
 * Bitunix-ähnlicher Order-Dialog: Live/Paper-Reiter, Coin-Dropdown
 * (voreingestellt: aktuell gewählter Coin), Margin-Schieberegler auf Basis
 * des freien Kapitals (absolut in USDT + %), Hebel, SL/TP.
 * Läuft über POST /api/ai/trade/open (source=manuell) – alle Guards greifen.
 */
export default function NewTradeModal({ defaultSymbol, onClose, onOpened }) {
  const [coins, setCoins] = useState([]);
  const [capital, setCapital] = useState(null);
  const [mode, setMode] = useState('paper');
  const [symbol, setSymbol] = useState(defaultSymbol || 'BTCUSDT');
  const [side, setSide] = useState('LONG');
  const [margin, setMargin] = useState(0);
  const [leverage, setLeverage] = useState(10);
  const [slPct, setSlPct] = useState(0.8);
  const [tpPct, setTpPct] = useState(2.0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/api/coins`).then(r => r.json())
      .then(d => setCoins(d.coins || d.symbols || []))
      .catch(() => {});
    fetch(`${API_URL}/api/autotrade/capital`).then(r => r.json())
      .then(d => setCapital(d))
      .catch(() => {});
  }, []);

  const alloc = capital?.allocation?.[mode] || {};
  const free = Math.max(0, Number(alloc.free ?? 0));
  const maxMargin = free > 0 ? free : 0;

  useEffect(() => {
    // Beim Moduswechsel: Margin auf 25% des freien Kapitals voreinstellen
    setMargin(m => {
      const def = Math.round(maxMargin * 0.25 * 100) / 100;
      return (m <= 0 || m > maxMargin) ? def : m;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, capital]);

  const marginPct = maxMargin > 0 ? (margin / maxMargin) * 100 : 0;
  const posSize = margin * (Number(leverage) || 1);

  const coinList = (coins || []).map(c => (typeof c === 'string' ? { symbol: c, name: c } : c));

  const submit = async () => {
    if (!(margin > 0)) { toast.error('Margin muss größer 0 sein'); return; }
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/trade/open`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          symbol, side, mode,
          margin_usdt: Number(margin),
          leverage: Number(leverage) || undefined,
          sl_pct: Number(slPct) || undefined,
          tpf_pct: Number(tpPct) || undefined,
          tp1_pct: Number(tpPct) ? Number(tpPct) / 2 : undefined,
          reason: 'Manuell eröffnet (Trade-Dialog)',
          source: 'manuell',
        }),
      });
      const data = await res.json();
      if (!res.ok || data.status !== 'ok') throw new Error(data.detail || 'Trade wurde abgelehnt');
      toast.success(`${side} ${symbol} (${mode.toUpperCase()}) eröffnet @ ${data.entry}`);
      onOpened && onOpened();
      onClose();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="ntm-overlay" onClick={onClose} data-testid="new-trade-modal">
      <div className="ntm" onClick={e => e.stopPropagation()}>
        <div className="ntm-head">
          <span>Neuer Trade</span>
          <button className="ntm-x" onClick={onClose} data-testid="new-trade-close"><X size={16} /></button>
        </div>

        <div className="ntm-tabs">
          {['paper', 'live'].map(m => (
            <button key={m} className={`ntm-tab ${m} ${mode === m ? 'on' : ''}`}
              onClick={() => setMode(m)} data-testid={`new-trade-mode-${m}`}>
              {m.toUpperCase()}
            </button>
          ))}
        </div>

        <label className="ntm-row">
          <span>Coin</span>
          <select value={symbol} onChange={e => setSymbol(e.target.value)} data-testid="new-trade-symbol">
            {!coinList.some(c => c.symbol === symbol) && <option value={symbol}>{symbol}</option>}
            {coinList.map(c => <option key={c.symbol} value={c.symbol}>{c.name || c.symbol}</option>)}
          </select>
        </label>

        <div className="ntm-sides">
          <button className={`ntm-side long ${side === 'LONG' ? 'on' : ''}`}
            onClick={() => setSide('LONG')} data-testid="new-trade-long">▲ LONG</button>
          <button className={`ntm-side short ${side === 'SHORT' ? 'on' : ''}`}
            onClick={() => setSide('SHORT')} data-testid="new-trade-short">▼ SHORT</button>
        </div>

        <div className="ntm-margin">
          <div className="ntm-margin-head">
            <span>Margin</span>
            <span className="ntm-free" data-testid="new-trade-free-capital">
              Frei: {maxMargin.toFixed(2)} USDT{alloc.used_margin != null ? ` · gebunden ${Number(alloc.used_margin).toFixed(2)}` : ''}
            </span>
          </div>
          <input type="range" min={0} max={maxMargin || 1} step={maxMargin > 100 ? 1 : 0.01}
            value={Math.min(margin, maxMargin || 1)}
            onChange={e => setMargin(Number(e.target.value))}
            disabled={maxMargin <= 0}
            data-testid="new-trade-margin-slider" />
          <div className="ntm-margin-vals">
            <span className="ntm-margin-input">
              <input type="number" min={0} max={maxMargin || undefined} step="any" value={margin}
                onChange={e => setMargin(Math.max(0, Number(e.target.value)))}
                data-testid="new-trade-margin-input" />
              <em>USDT</em>
            </span>
            <span className="ntm-margin-pct mono">{marginPct.toFixed(0)}%</span>
          </div>
          {maxMargin <= 0 && <div className="ntm-warn">Kein freies Kapital in diesem Modus – Kapital-Zuweisung prüfen.</div>}
        </div>

        {[
          ['Hebel', leverage, setLeverage, 1, 100, 1, 'x'],
          ['Stop-Loss', slPct, setSlPct, 0.15, 12, 0.05, '%'],
          ['Take-Profit', tpPct, setTpPct, 0.3, 60, 0.1, '%'],
        ].map(([label, val, set, min, max, step, unit]) => (
          <label className="ntm-row" key={label}>
            <span>{label}</span>
            <span className="ntm-input">
              <input type="number" min={min} max={max} step={step} value={val}
                onChange={e => set(e.target.value)}
                data-testid={`new-trade-${label === 'Hebel' ? 'leverage' : (label === 'Stop-Loss' ? 'sl' : 'tp')}`} />
              <em>{unit}</em>
            </span>
          </label>
        ))}

        <div className="ntm-summary mono" data-testid="new-trade-position-size">
          Positionsgröße: {posSize.toFixed(2)} USDT ({margin > 0 ? margin.toFixed(2) : '0'} × {leverage}x)
        </div>
        <div className="ntm-hint">Alle Schutz-Guards (Kill-Switch, Limits, Kapital) bleiben aktiv.</div>

        <button className={`ntm-submit ${side === 'LONG' ? 'long' : 'short'}`}
          disabled={busy || maxMargin <= 0} onClick={submit} data-testid="new-trade-submit">
          {busy ? 'Wird eröffnet…' : `${side} ${mode.toUpperCase()} eröffnen`}
        </button>
      </div>
    </div>
  );
}
