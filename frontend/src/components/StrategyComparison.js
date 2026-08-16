import React, { useState, useEffect, useCallback } from 'react';
import { X, Trophy, CaretDown, CaretUp } from '@phosphor-icons/react';
import SafeOverlay from './SafeOverlay';
import './StrategyComparison.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

export default function StrategyComparison({ onClose }) {
  const [data, setData] = useState(null);
  const [mode, setMode] = useState('all');
  const [days, setDays] = useState(0);
  const [expanded, setExpanded] = useState(null);
  const [coinExpanded, setCoinExpanded] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await fetch(`${API_URL}/api/analytics/strategy-comparison?mode=${mode}&days=${days}`).then(r => r.json());
      setData(d);
    } catch (e) { console.error(e); }
    setLoading(false);
  }, [mode, days]);

  useEffect(() => { load(); }, [load]);

  const rows = data?.comparison || [];
  const bestPnl = rows.length ? Math.max(...rows.map(r => r.pnl)) : 0;

  return (
    <SafeOverlay className="sc-overlay" onClose={onClose}>
      <div className="sc-panel" onClick={e => e.stopPropagation()} data-testid="strategy-comparison-modal">
        <div className="sc-header">
          <h2><Trophy size={20} weight="fill" style={{ color: '#FFD700' }} /> STRATEGIE-VERGLEICH</h2>
          <button className="sc-close" onClick={onClose} data-testid="comparison-close"><X size={22} weight="bold" /></button>
        </div>

        <div className="sc-filters">
          <div className="sc-seg" data-testid="comparison-mode-filter">
            {['all', 'live', 'paper'].map(m => (
              <button key={m} className={mode === m ? 'active' : ''} onClick={() => setMode(m)} data-testid={`comparison-mode-${m}`}>
                {m === 'all' ? 'ALLE' : m.toUpperCase()}
              </button>
            ))}
          </div>
          <div className="sc-seg" data-testid="comparison-days-filter">
            {[{ v: 0, l: 'Gesamt' }, { v: 30, l: '30 Tage' }, { v: 7, l: '7 Tage' }].map(o => (
              <button key={o.v} className={days === o.v ? 'active' : ''} onClick={() => setDays(o.v)}>{o.l}</button>
            ))}
          </div>
          <div className="sc-total">{data?.total_trades ?? 0} geschlossene Trades</div>
        </div>

        {loading && <div className="sc-empty">Lädt...</div>}
        {!loading && rows.length === 0 && (
          <div className="sc-empty" data-testid="comparison-empty">
            Noch keine geschlossenen Trades für diesen Filter.<br />
            Tipp: Nutze den <b>Backtester</b>, um Strategien sofort auf historischen Daten zu vergleichen.
          </div>
        )}

        {!loading && rows.length > 0 && (
          <div className="sc-table-wrap">
            <table className="sc-table" data-testid="comparison-table">
              <thead>
                <tr>
                  <th>#</th><th>Strategie</th><th>Trades</th><th>Win-Rate</th>
                  <th>PnL (USDT)</th><th>Ø PnL</th><th>Profit-Faktor</th>
                  <th>Max DD</th><th>Ø Dauer</th><th>Gebühren</th><th>L / S</th><th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <React.Fragment key={r.strategy_id}>
                    <tr className={r.pnl === bestPnl && r.pnl > 0 ? 'sc-best' : ''} data-testid={`comparison-row-${r.strategy_id}`}>
                      <td>{r.pnl === bestPnl && r.pnl > 0 ? <Trophy size={14} weight="fill" style={{ color: '#FFD700' }} /> : i + 1}</td>
                      <td className="sc-name" title={r.is_manual ? 'Manuell eröffnete Trades (Bitunix-App oder Website) – als eigene Vergleichsgruppe' : r.strategy_name}>{r.strategy_name}
                        {r.is_manual && <span className="sc-manual-badge" data-testid="sc-manual-badge">manuell</span>}
                        {r.open_trades > 0 && <span className="sc-open-badge">{r.open_trades} offen</span>}
                      </td>
                      <td>{r.trades}</td>
                      <td className={r.win_rate >= 50 ? 'pos' : 'neg'}>{fmt(r.win_rate, 1)}%</td>
                      <td className={`mono ${r.pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(r.pnl)}</td>
                      <td className={`mono ${r.avg_pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(r.avg_pnl, 3)}</td>
                      <td className={r.profit_factor >= 1 ? 'pos' : 'neg'}>{fmt(r.profit_factor)}</td>
                      <td className="mono neg">{fmt(r.max_drawdown)}</td>
                      <td>{fmt(r.avg_duration_min, 1)} min</td>
                      <td className="mono">{fmt(r.fees)}</td>
                      <td>{r.long_trades}/{r.short_trades}</td>
                      <td>
                        <button className="sc-expand" onClick={() => { setExpanded(expanded === r.strategy_id ? null : r.strategy_id); setCoinExpanded(null); }} data-testid={`comparison-expand-${r.strategy_id}`}>
                          {expanded === r.strategy_id ? <CaretUp size={14} /> : <CaretDown size={14} />}
                        </button>
                      </td>
                    </tr>
                    {expanded === r.strategy_id && (
                      <tr className="sc-detail-row">
                        <td colSpan={12}>
                          <div className="sc-coins">
                            {(r.by_symbol || []).map(s => {
                              const key = `${r.strategy_id}|${s.symbol}`;
                              const open = coinExpanded === key;
                              return (
                                <button key={s.symbol} className={`sc-coin-chip ${open ? 'open' : ''}`}
                                  onClick={() => setCoinExpanded(open ? null : key)}
                                  title="Klicken für mehr Insights zu den Trades dieser Strategie auf diesem Coin"
                                  data-testid={`comparison-coin-${r.strategy_id}-${s.symbol}`}>
                                  <b>{s.symbol.replace('USDT', '')}</b>
                                  <span>{s.trades} Trades</span>
                                  <span className={s.win_rate >= 50 ? 'pos' : 'neg'}>{fmt(s.win_rate, 0)}% WR</span>
                                  <span className={`mono ${s.pnl >= 0 ? 'pos' : 'neg'}`}>{fmt(s.pnl)} USDT</span>
                                  {open ? <CaretUp size={11} /> : <CaretDown size={11} />}
                                </button>
                              );
                            })}
                          </div>
                          {(() => {
                            const s = (r.by_symbol || []).find(x => `${r.strategy_id}|${x.symbol}` === coinExpanded);
                            if (!s) return null;
                            return (
                              <div className="sc-coin-insights" data-testid={`comparison-coin-insights-${r.strategy_id}-${s.symbol}`}>
                                <div><span>W / V / BE</span><b>{s.wins} / {s.losses} / {s.breakevens ?? 0}</b></div>
                                <div><span>Ø PnL</span><b className={`mono ${(s.avg_pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>{fmt(s.avg_pnl, 3)}</b></div>
                                <div><span>Profit-Faktor</span><b className={(s.profit_factor ?? 0) >= 1 ? 'pos' : 'neg'}>{fmt(s.profit_factor)}</b></div>
                                <div><span>Bester Trade</span><b className="mono pos">{fmt(s.best_trade)}</b></div>
                                <div><span>Schlechtester</span><b className="mono neg">{fmt(s.worst_trade)}</b></div>
                                <div><span>Ø Dauer</span><b>{fmt(s.avg_duration_min, 1)} min</b></div>
                                <div><span>Long / Short</span><b>{s.long_trades ?? 0} / {s.short_trades ?? 0}</b></div>
                                <div><span>Paper / Live</span><b>{s.paper_trades ?? 0} / {s.live_trades ?? 0}</b></div>
                                <div><span>Gebühren</span><b className="mono">{fmt(s.fees)}</b></div>
                              </div>
                            );
                          })()}
                          <div className="sc-extremes">
                              Bester Trade: <span className="pos mono">{fmt(r.best_trade)}</span> ·
                              Schlechtester: <span className="neg mono">{fmt(r.worst_trade)}</span> ·
                              Paper/Live: {r.paper_trades}/{r.live_trades}
                            </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </SafeOverlay>
  );
}
