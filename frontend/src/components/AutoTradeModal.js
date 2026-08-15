import React, { useState, useEffect } from 'react';
import { X, Lightning, TrendUp, TrendDown, Warning } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import SafeOverlay from './SafeOverlay';
import './AutoTradeModal.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const AutoTradeModal = ({ symbol, onClose }) => {
  const [cfg, setCfg] = useState(null);
  const [mode, setMode] = useState('paper');
  const [bitunixOk, setBitunixOk] = useState(false);
  const [trades, setTrades] = useState([]);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    const [c, t] = await Promise.all([
      fetch(`${API_URL}/api/autotrade/config`).then(r => r.json()),
      fetch(`${API_URL}/api/autotrade/trades?limit=20`).then(r => r.json()),
    ]);
    const defaults = c.defaults || {};
    const coinCfg = { ...defaults, ...((c.config.coins || {})[symbol] || {}) };
    setCfg(coinCfg);
    setMode(c.config.mode || 'paper');
    setBitunixOk(c.bitunix_configured);
    setTrades((t.trades || []).filter(x => x.symbol === symbol));
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [symbol]);

  const update = (k, v) => setCfg(prev => ({ ...prev, [k]: v }));

  const save = async () => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich zum Speichern'); return; }
    setSaving(true);
    try {
      const r1 = await fetch(`${API_URL}/api/autotrade/config`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ mode }),
      });
      if (r1.status === 401) { toast.error('Nicht autorisiert – bitte als Admin anmelden'); return; }
      if (!r1.ok) { toast.error('Fehler beim Speichern (Modus)'); return; }
      const res = await fetch(`${API_URL}/api/autotrade/coin/${symbol}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(cfg),
      });
      if (res.status === 401) {
        toast.error('Nicht autorisiert – bitte als Admin anmelden');
        return;
      }
      if (res.ok) {
        toast.success(`Auto-Trade ${symbol.replace('USDT', '')} gespeichert (${mode.toUpperCase()})`);
        await load();
      } else {
        const err = await res.json().catch(() => ({}));
        toast.error(err.detail || 'Fehler beim Speichern');
      }
    } catch { toast.error('Verbindungsfehler beim Speichern'); }
    finally { setSaving(false); }
  };

  const toggleEnabled = async () => {
    const next = !cfg.enabled;
    update('enabled', next);
  };

  const closeTrade = async (id) => {
    if (!isAdmin()) { toast.error('Admin-Login erforderlich'); return; }
    const res = await fetch(`${API_URL}/api/autotrade/close/${id}`, {
      method: 'POST', headers: { ...authHeaders() },
    });
    if (res.ok) { toast.success('Trade geschlossen'); load(); }
    else toast.error('Fehler beim Schließen');
  };

  if (!cfg) return null;
  const posSize = ((cfg.max_capital || 0) * (cfg.leverage || 1)).toFixed(0);
  const openTrades = trades.filter(t => t.status === 'open');

  return (
    <SafeOverlay className="at-overlay" onClose={onClose}>
      <div className="at-panel" onClick={e => e.stopPropagation()} data-testid="autotrade-modal">
        <div className="at-header">
          <div className="at-title">
            <Lightning size={20} weight="fill" />
            <span>AUTO-TRADE · {symbol.replace('USDT', '')}</span>
          </div>
          <button className="at-close" onClick={onClose} data-testid="autotrade-close"><X size={22} weight="bold" /></button>
        </div>

        {/* Mode + enable */}
        <div className="at-mode-row">
          <div className="at-mode-toggle" data-testid="autotrade-mode">
            <button className={mode === 'live' ? 'active live' : ''} onClick={() => setMode('live')} data-testid="mode-live">LIVE</button>
            <button className={mode === 'paper' ? 'active' : ''} onClick={() => setMode('paper')} data-testid="mode-paper">PAPER</button>
          </div>
          <label className="at-enable">
            <span>Auto-Trade aktiv</span>
            <label className="switch">
              <input type="checkbox" checked={!!cfg.enabled} onChange={toggleEnabled} data-testid="autotrade-enable-toggle" />
              <span className="slider"></span>
            </label>
          </label>
        </div>

        {mode === 'live' && (
          <div className={`at-warn ${bitunixOk ? '' : 'err'}`}>
            <Warning size={16} weight="fill" />
            {bitunixOk ? 'LIVE Modus: Echte Orders auf Bitunix (nur auf deinem Server ausführbar).' : 'Bitunix API nicht konfiguriert!'}
          </div>
        )}

        {/* Capital + leverage (Bitunix-like) */}
        <div className="at-section">
          <div className="at-field">
            <label>Max. Kapital (USDT Margin)</label>
            <input type="number" value={cfg.max_capital} min={1} step={1}
              onChange={e => update('max_capital', parseFloat(e.target.value))} data-testid="at-max-capital" />
          </div>
          <div className="at-field">
            <label>Hebel: <b>{cfg.leverage}x</b></label>
            <input type="range" min={1} max={200} value={cfg.leverage}
              onChange={e => update('leverage', parseInt(e.target.value))} data-testid="at-leverage" />
          </div>
        </div>
        <div className="at-possize">Positionsgröße: <b>{posSize} USDT</b> · Order: {cfg.order_type}</div>

        {/* SL system */}
        <div className="at-block">
          <div className="at-block-title">STOP LOSS</div>
          <div className="at-seg">
            <button className={cfg.sl_mode === 'structure' ? 'active' : ''} onClick={() => update('sl_mode', 'structure')} data-testid="sl-mode-structure">Struktur (Support/Widerstand)</button>
            <button className={cfg.sl_mode === 'fixed' ? 'active' : ''} onClick={() => update('sl_mode', 'fixed')} data-testid="sl-mode-fixed">Fest %</button>
          </div>
          {cfg.sl_mode === 'structure' ? (
            <div className="at-section">
              <div className="at-field"><label>Ticks unter/über Tief/Hoch</label>
                <input type="number" value={cfg.sl_ticks} onChange={e => update('sl_ticks', parseInt(e.target.value))} data-testid="at-sl-ticks" /></div>
              <div className="at-field"><label>Lookback (Kerzen)</label>
                <input type="number" value={cfg.sl_lookback} onChange={e => update('sl_lookback', parseInt(e.target.value))} data-testid="at-sl-lookback" /></div>
            </div>
          ) : (
            <div className="at-field"><label>SL Abstand %</label>
              <input type="number" step={0.1} value={cfg.sl_fixed_percent} onChange={e => update('sl_fixed_percent', parseFloat(e.target.value))} data-testid="at-sl-percent" /></div>
          )}
        </div>

        {/* TP system */}
        <div className="at-block">
          <div className="at-block-title">TAKE PROFIT (dynamisch)</div>
          <div className="at-section">
            <div className="at-field"><label>TP1 bei CRV</label>
              <input type="number" step={0.1} value={cfg.tp1_crv} onChange={e => update('tp1_crv', parseFloat(e.target.value))} data-testid="at-tp1-crv" /></div>
            <div className="at-field"><label>TP1 schließt % der Position</label>
              <input type="number" min={1} max={99} value={cfg.tp1_close_percent} onChange={e => update('tp1_close_percent', parseInt(e.target.value))} data-testid="at-tp1-close" /></div>
          </div>
          <div className="at-field"><label>TP Full bei CRV</label>
            <input type="number" step={0.1} value={cfg.tp_full_crv} onChange={e => update('tp_full_crv', parseFloat(e.target.value))} data-testid="at-tpfull-crv" /></div>
          <label className="at-check">
            <input type="checkbox" checked={!!cfg.breakeven_enabled} onChange={e => update('breakeven_enabled', e.target.checked)} data-testid="at-breakeven" />
            <span>Bei CRV 1 → Stop Loss auf Break-Even + Gebühren</span>
          </label>
          {cfg.breakeven_enabled && (
            <div className="at-field small"><label>Gebühren % (Round-Trip Offset)</label>
              <input type="number" step={0.01} value={cfg.fee_percent} onChange={e => update('fee_percent', parseFloat(e.target.value))} data-testid="at-fee" /></div>
          )}
          <label className="at-check">
            <input type="checkbox" checked={!!cfg.trade_pre_signals} onChange={e => update('trade_pre_signals', e.target.checked)} data-testid="at-pre-signals" />
            <span>Auch Pre-Signale traden</span>
          </label>
        </div>

        {/* Gewinnsicherung */}
        <div className="at-block">
          <div className="at-block-title">GEWINNSICHERUNG</div>
          <label className="at-check" style={{ marginTop: 0 }}>
            <input type="checkbox" checked={!!cfg.profit_secure_enabled} onChange={e => update('profit_secure_enabled', e.target.checked)} data-testid="at-profit-secure" />
            <span>Bei Gewinn: Stop-Loss in den Gewinn ziehen &amp; Marge freisetzen</span>
          </label>
          {cfg.profit_secure_enabled && (
            <div className="at-section" style={{ marginTop: 10 }}>
              <div className="at-field"><label>Auslöser: Gewinn % auf Marge</label>
                <input type="number" step={5} min={1} value={cfg.profit_secure_trigger_pct ?? 30} onChange={e => update('profit_secure_trigger_pct', parseFloat(e.target.value) || 0)} data-testid="at-ps-trigger" /></div>
              <div className="at-field"><label>Gesicherter Gewinn-Anteil %</label>
                <input type="number" step={5} min={1} max={95} value={cfg.profit_lock_pct ?? 50} onChange={e => update('profit_lock_pct', parseFloat(e.target.value) || 0)} data-testid="at-ps-lock" /></div>
            </div>
          )}
        </div>

        <button className="at-save" onClick={save} disabled={saving} data-testid="autotrade-save">
          {saving ? 'Speichere...' : 'Einstellungen speichern'}
        </button>

        {/* Open trades */}
        {openTrades.length > 0 && (
          <div className="at-block">
            <div className="at-block-title">OFFENE TRADES</div>
            {openTrades.map(t => {
              const c = t.computed || {};
              const livePnl = c.live_pnl != null ? c.live_pnl : (t.realized_pnl || 0);
              return (
              <div key={t.id} className="at-trade" data-testid={`open-trade-${t.id}`}>
                <div className={`at-trade-side ${t.side === 'LONG' ? 'long' : 'short'}`}>
                  {t.side === 'LONG' ? <TrendUp size={14} /> : <TrendDown size={14} />} {t.side}
                </div>
                <div className="at-trade-info mono">
                  Entry {t.entry} · SL {t.sl} · TP {t.tpf} {t.tp1_hit ? '· TP1✓' : ''}
                  {c.current_price != null && (
                    <> · Kurs <b data-testid={`open-trade-price-${t.id}`}>{c.current_price}</b></>
                  )}
                  {c.live_pnl != null && (
                    <> · PnL <b className={livePnl >= 0 ? 'long' : 'short'} data-testid={`open-trade-pnl-${t.id}`}>{livePnl >= 0 ? '+' : ''}{livePnl.toFixed(2)} $</b></>
                  )}
                </div>
                <button className="at-trade-close" onClick={() => closeTrade(t.id)} data-testid={`close-trade-${t.id}`}>Schließen</button>
              </div>
              );
            })}
          </div>
        )}
      </div>
    </SafeOverlay>
  );
};

export default AutoTradeModal;
