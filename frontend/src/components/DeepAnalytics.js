import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  ChartBar, TrendUp, TrendDown, Target, CheckCircle, XCircle, Warning,
  ArrowsClockwise, CaretDown, CaretRight, Clock, Lightning, ListChecks,
} from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import './DeepAnalytics.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmt = (v, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  return Number(v).toLocaleString('de-DE', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
};
const pct = (v, digits = 1) => (v === null || v === undefined) ? '–' : `${fmt(v, digits)}%`;
const money = (v) => (v === null || v === undefined) ? '–' : `${fmt(v, 2)} USDT`;

const statusColor = { green: '#00FF66', yellow: '#FFB020', red: '#FF3366', gray: '#5C6070' };
const statusLabel = { green: 'BEWÄHRT', yellow: 'BEOBACHTEN', red: 'NICHT BEWÄHRT', gray: 'ZU WENIG DATEN' };

// ---------- Equity curve (SVG) ----------
const EquityChart = ({ data, height = 140 }) => {
  if (!data || data.length === 0) {
    return <div className="da-empty">Noch keine geschlossenen Trades für die Equity-Kurve.</div>;
  }
  const w = 640;
  const padX = 8, padY = 8;
  const ys = data.map(d => d.equity);
  const minY = Math.min(0, ...ys);
  const maxY = Math.max(0, ...ys);
  const spanY = (maxY - minY) || 1;
  const stepX = (w - padX * 2) / Math.max(1, data.length - 1);
  const points = data.map((d, i) => {
    const x = padX + i * stepX;
    const y = padY + (1 - (d.equity - minY) / spanY) * (height - padY * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const zeroY = padY + (1 - (0 - minY) / spanY) * (height - padY * 2);
  const last = data[data.length - 1];
  const positive = last.equity >= 0;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="da-equity" preserveAspectRatio="none">
      <line x1={padX} x2={w - padX} y1={zeroY} y2={zeroY} stroke="#2A2D3A" strokeDasharray="3,3" />
      <polyline
        fill="none"
        stroke={positive ? '#00FF66' : '#FF3366'}
        strokeWidth="1.6"
        points={points}
      />
    </svg>
  );
};

// ---------- Traffic light ----------
const StatusDot = ({ status }) => (
  <span className="da-dot" style={{ background: statusColor[status] || '#5C6070' }} />
);

// ---------- Trade row (expandable) ----------
const TradeRow = ({ trade, onLoadDetail }) => {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState(null);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail && onLoadDetail) {
      const d = await onLoadDetail(trade.id);
      setDetail(d);
    }
  };

  const pnl = trade.realized_pnl || 0;
  const pnlCls = pnl > 0 ? 'da-pos' : pnl < 0 ? 'da-neg' : '';
  const cap = parseFloat(trade.max_capital) || 0;
  const pnlPct = cap ? (pnl / cap) * 100 : null;
  const time = trade.closed_at || trade.opened_at;
  return (
    <div className="da-trade">
      <button className="da-trade-head" onClick={toggle} data-testid={`da-trade-toggle-${trade.id}`}>
        {open ? <CaretDown size={12} /> : <CaretRight size={12} />}
        <span className={`da-badge ${trade.side === 'LONG' ? 'da-long' : 'da-short'}`}>{trade.side}</span>
        <span className="da-symbol">{(trade.symbol || '').replace('USDT', '')}</span>
        <span className="da-strat">{trade.strategy_name || trade.strategy_id}</span>
        <span className="da-reason">{trade.exit_reason || trade.result}</span>
        <span className={`da-pnl ${pnlCls}`}>{pnl >= 0 ? '+' : ''}{fmt(pnl, 4)}</span>
        {pnlPct != null && (
          <span className={`da-pnl ${pnlCls}`} data-testid={`da-trade-pnl-pct-${trade.id}`}>
            ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)
          </span>
        )}
        <span className="da-time">{time ? new Date(time).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin' }) : '–'}</span>
      </button>
      {open && (
        <div className="da-trade-body" data-testid={`da-trade-body-${trade.id}`}>
          {!detail && <div className="da-loading">Lade Details…</div>}
          {detail && (
            <>
              <div className="da-kv-grid">
                <div><span>Haltedauer</span><b>{detail.hold_human || '–'}</b></div>
                <div><span>R-Multiple</span><b className={detail.r_multiple >= 0 ? 'da-pos' : 'da-neg'}>{detail.r_multiple ?? '–'} R</b></div>
                <div><span>CRV geplant</span><b>{detail.planned_crv ?? '–'}</b></div>
                <div><span>CRV real</span><b className={(detail.realized_crv || 0) >= 0 ? 'da-pos' : 'da-neg'}>{detail.realized_crv ?? '–'}</b></div>
                <div><span>Exit-Grund</span><b>{detail.exit_reason}</b></div>
                <div><span>Gebühren</span><b>{fmt(detail.fee_amount, 4)} USDT</b></div>
                <div><span>Gebühren-Anteil am PnL</span><b>{detail.fee_pct_of_pnl != null ? pct(detail.fee_pct_of_pnl, 1) : '–'}</b></div>
                <div><span>PnL nach Gebühren</span><b className={detail.pnl_after_fees >= 0 ? 'da-pos' : 'da-neg'}>{fmt(detail.pnl_after_fees, 4)}</b></div>
                <div><span>Entry / Exit</span><b className="da-mono">{fmt(detail.entry, 4)} → {fmt(detail.exit_price, 4)}</b></div>
                <div><span>SL initial / final</span><b className="da-mono">{fmt(detail.initial_sl, 4)} → {fmt(detail.sl, 4)}</b></div>
                <div><span>TP1 / TP-Voll</span><b className="da-mono">{fmt(detail.tp1, 4)} / {fmt(detail.tpf, 4)}</b></div>
                <div><span>Hebel</span><b>{detail.leverage}x</b></div>
                <div><span>Position (Notional)</span><b>{fmt(detail.position_notional, 2)} USDT</b></div>
                <div><span>Entry-Zeit</span><b>{detail.entry_weekday_name || '–'} · {detail.entry_hour != null ? String(detail.entry_hour).padStart(2, '0') + ':00' : '–'}</b></div>
                <div><span>Signal-Klasse</span><b>{detail.signal_class || detail.source_signal?.signal_class || '–'}</b></div>
              </div>
              {detail.events?.length > 0 && (
                <div className="da-events">
                  <div className="da-events-title">Verlauf</div>
                  {detail.events.map((e, i) => (<div key={i} className="da-event">{e}</div>))}
                </div>
              )}
              {detail.source_signal && (
                <div className="da-signal-ref">
                  <span className="da-events-title">Auslösendes Signal</span>
                  <span>{detail.source_signal.strategy_id}</span>
                  <span>·</span>
                  <span>{detail.source_signal.signal_class || 'SIGNAL'}</span>
                  <span>·</span>
                  <span className="da-mono">Entry {fmt(detail.source_signal.entry_price, 4)}</span>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
};

// ---------- Strategy card ----------
const StrategyCard = ({ row, onOpen, expanded, detail, loadingDetail }) => {
  const at = row.all_time || {};
  const roll = row.rolling || {};
  return (
    <div className={`da-strat-card status-${row.status}`} data-testid={`da-strat-${row.strategy_id}`}>
      <button className="da-strat-head" onClick={() => onOpen(row.strategy_id)} data-testid={`da-strat-open-${row.strategy_id}`}>
        <StatusDot status={row.status} />
        <div className="da-strat-name">{row.strategy_name}</div>
        <div className="da-strat-status" style={{ color: statusColor[row.status] }}>{statusLabel[row.status]}</div>
        <div className="da-strat-kpis">
          <span>WR <b>{pct(at.win_rate, 1)}</b></span>
          <span>PF <b>{at.profit_factor_inf ? '∞' : fmt(at.profit_factor, 2)}</b></span>
          <span>E <b>{fmt(at.expectancy, 3)}</b></span>
          <span>Trades <b>{at.trades}</b></span>
          <span className={(at.total_pnl || 0) >= 0 ? 'da-pos' : 'da-neg'}>PnL <b>{fmt(at.total_pnl, 2)}</b></span>
        </div>
        {expanded ? <CaretDown size={14} /> : <CaretRight size={14} />}
      </button>
      {row.hints?.length > 0 && (
        <div className="da-hints">
          {row.hints.map((h, i) => (
            <div key={i} className="da-hint">
              <Warning size={12} />
              <span>{h}</span>
            </div>
          ))}
        </div>
      )}
      {expanded && (
        <div className="da-strat-body">
          {loadingDetail && <div className="da-loading">Lade Strategie-Details…</div>}
          {detail && (
            <>
              <div className="da-breakdown-row">
                <div className="da-breakdown">
                  <div className="da-bk-title">Rolling (letzte {roll.window})</div>
                  <div className="da-kv-inline">
                    <span>WR <b>{pct(roll.win_rate, 1)}</b></span>
                    <span>PF <b>{roll.profit_factor_inf ? '∞' : fmt(roll.profit_factor, 2)}</b></span>
                    <span>E <b>{fmt(roll.expectancy, 3)}</b></span>
                    <span>Trades <b>{roll.trades}</b></span>
                  </div>
                </div>
                <div className="da-breakdown">
                  <div className="da-bk-title">Long vs Short</div>
                  {(detail.by_side || []).map(s => (
                    <div key={s.key} className="da-bk-row">
                      <span>{s.key}</span>
                      <span>{s.trades} T</span>
                      <span>WR {pct(s.win_rate, 0)}</span>
                      <span className={s.total_pnl >= 0 ? 'da-pos' : 'da-neg'}>{fmt(s.total_pnl, 2)}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="da-breakdown-row">
                <div className="da-breakdown">
                  <div className="da-bk-title">Nach Stunde</div>
                  {(detail.by_hour || []).slice(0, 8).map(h => (
                    <div key={h.key} className="da-bk-row">
                      <span>{h.label}</span>
                      <span>{h.trades} T</span>
                      <span>WR {pct(h.win_rate, 0)}</span>
                      <span className={h.total_pnl >= 0 ? 'da-pos' : 'da-neg'}>{fmt(h.total_pnl, 2)}</span>
                    </div>
                  ))}
                </div>
                <div className="da-breakdown">
                  <div className="da-bk-title">Nach Wochentag</div>
                  {(detail.by_weekday || []).map(w => (
                    <div key={w.key} className="da-bk-row">
                      <span>{w.label}</span>
                      <span>{w.trades} T</span>
                      <span>WR {pct(w.win_rate, 0)}</span>
                      <span className={w.total_pnl >= 0 ? 'da-pos' : 'da-neg'}>{fmt(w.total_pnl, 2)}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="da-breakdown">
                <div className="da-bk-title">Exit-Grund</div>
                <div className="da-kv-inline">
                  {(detail.exit_reasons || []).map(er => (
                    <span key={er.reason}>{er.reason} <b>{er.count}</b></span>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
};

// ---------- Main component ----------
const DeepAnalytics = () => {
  const [overview, setOverview] = useState(null);
  const [health, setHealth] = useState(null);
  const [expanded, setExpanded] = useState({});          // { strategy_id: bool }
  const [details, setDetails] = useState({});            // { strategy_id: detail }
  const [loadingDetailIds, setLoadingDetailIds] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [ovRes, hlRes] = await Promise.all([
        fetch(`${API_URL}/api/analytics/overview`).then(r => r.json()),
        fetch(`${API_URL}/api/analytics/strategy-health`).then(r => r.json()),
      ]);
      setOverview(ovRes);
      setHealth(hlRes);
    } catch (e) {
      setError('Konnte Analyse-Daten nicht laden.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  const loadTradeDetail = useCallback(async (tradeId) => {
    try {
      const d = await fetch(`${API_URL}/api/analytics/trade/${tradeId}`).then(r => r.json());
      return d;
    } catch { return null; }
  }, []);

  const openStrategy = useCallback(async (sid) => {
    setExpanded(prev => ({ ...prev, [sid]: !prev[sid] }));
    if (!details[sid]) {
      setLoadingDetailIds(prev => ({ ...prev, [sid]: true }));
      try {
        const d = await fetch(`${API_URL}/api/analytics/strategy/${sid}`).then(r => r.json());
        setDetails(prev => ({ ...prev, [sid]: d }));
      } catch { /* noop */ }
      setLoadingDetailIds(prev => ({ ...prev, [sid]: false }));
    }
  }, [details]);

  const totals = overview?.totals || {};
  const rolling = overview?.rolling || {};
  const equity = overview?.equity_curve || [];

  const kiReview = useMemo(() => {
    if (!health || !overview) return null;
    const parts = [];
    const t = overview.totals || {};
    parts.push(`Insgesamt ${t.trades || 0} geschlossene Trades, Winrate ${pct(t.win_rate, 1)}, Profit Factor ${t.profit_factor_inf ? '∞' : fmt(t.profit_factor, 2)}, Expectancy ${fmt(t.expectancy, 3)} USDT/Trade.`);
    parts.push(`Peak-Equity ${money(t.peak_equity)} · Max Drawdown ${money(t.max_drawdown)}.`);
    const bad = (health.strategies || []).filter(s => s.status === 'red');
    const watch = (health.strategies || []).filter(s => s.status === 'yellow');
    const good = (health.strategies || []).filter(s => s.status === 'green');
    if (bad.length) {
      parts.push(`Nicht bewährt (Regeln überarbeiten): ${bad.map(b => `${b.strategy_name} (WR ${pct(b.all_time.win_rate, 0)}, PF ${b.all_time.profit_factor_inf ? '∞' : fmt(b.all_time.profit_factor, 2)})`).join('; ')}.`);
    }
    if (watch.length) {
      parts.push(`Beobachten: ${watch.map(b => b.strategy_name).join(', ')} – rolling Kennzahlen verschlechtern sich.`);
    }
    if (good.length) {
      parts.push(`Bewährt: ${good.map(b => b.strategy_name).join(', ')}.`);
    }
    const hints = (health.strategies || []).flatMap(s => (s.hints || []).map(h => `• ${s.strategy_name}: ${h}`));
    if (hints.length) parts.push('Konkrete Hinweise:\n' + hints.slice(0, 8).join('\n'));
    return parts.join('\n\n');
  }, [health, overview]);

  const copyReview = () => {
    if (!kiReview) return;
    navigator.clipboard.writeText(kiReview).then(() => toast.success('Review kopiert')).catch(() => {});
  };

  return (
    <div className="deep-analytics" data-testid="deep-analytics">
      <div className="da-header">
        <div>
          <h3><ListChecks size={16} weight="bold" /> ANALYSE DEEP</h3>
          <div className="da-sub">Trades · Strategien · Regel-Bewährung</div>
        </div>
        <button className="da-refresh" onClick={loadAll} data-testid="da-refresh" disabled={loading}>
          <ArrowsClockwise size={14} weight="bold" /> {loading ? 'Lade…' : 'Neu laden'}
        </button>
      </div>

      {error && <div className="da-error">{error}</div>}

      {/* KPI cards */}
      <div className="da-kpi-grid">
        <div className="da-kpi" data-testid="da-kpi-pf">
          <div className="da-kpi-label">Profit Factor</div>
          <div className="da-kpi-value">{totals.profit_factor_inf ? '∞' : fmt(totals.profit_factor, 2)}</div>
          <div className="da-kpi-sub">Gewinn / Verlust</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-expect">
          <div className="da-kpi-label">Expectancy</div>
          <div className={`da-kpi-value ${(totals.expectancy || 0) >= 0 ? 'da-pos' : 'da-neg'}`}>{fmt(totals.expectancy, 3)}</div>
          <div className="da-kpi-sub">USDT / Trade{totals.expectancy_r != null ? ` · ${fmt(totals.expectancy_r, 2)} R` : ''}</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-wr">
          <div className="da-kpi-label">Winrate</div>
          <div className="da-kpi-value">{pct(totals.win_rate, 1)}</div>
          <div className="da-kpi-sub">{totals.wins}W · {totals.losses}L{totals.breakevens ? ` · ${totals.breakevens} BE` : ''}</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-dd">
          <div className="da-kpi-label">Max Drawdown</div>
          <div className="da-kpi-value da-neg">{money(totals.max_drawdown)}</div>
          <div className="da-kpi-sub">Peak {money(totals.peak_equity)}</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-trades">
          <div className="da-kpi-label">Trades gesamt</div>
          <div className="da-kpi-value">{totals.trades || 0}</div>
          <div className="da-kpi-sub">{totals.open_trades || 0} offen</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-pnl">
          <div className="da-kpi-label">Netto PnL</div>
          <div className={`da-kpi-value ${(totals.total_pnl || 0) >= 0 ? 'da-pos' : 'da-neg'}`}>{money(totals.total_pnl)}</div>
          <div className="da-kpi-sub">Ø Gewinn {money(totals.avg_win)} · Ø Verlust {money(-1 * (totals.avg_loss || 0))}</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-streak">
          <div className="da-kpi-label">Streaks</div>
          <div className="da-kpi-value"><span className="da-pos">{totals.longest_win_streak || 0}W</span> · <span className="da-neg">{totals.longest_loss_streak || 0}L</span></div>
          <div className="da-kpi-sub">längste Serie</div>
        </div>
        <div className="da-kpi" data-testid="da-kpi-rolling">
          <div className="da-kpi-label">Rolling 20</div>
          <div className={`da-kpi-value ${(rolling.last_20?.expectancy || 0) >= 0 ? 'da-pos' : 'da-neg'}`}>{fmt(rolling.last_20?.expectancy, 3)}</div>
          <div className="da-kpi-sub">WR {pct(rolling.last_20?.win_rate, 0)} · PF {rolling.last_20?.profit_factor_inf ? '∞' : fmt(rolling.last_20?.profit_factor, 2)}</div>
        </div>
      </div>

      {/* Equity curve */}
      <div className="da-section">
        <div className="da-section-title"><TrendUp size={13} /> EQUITY-KURVE</div>
        <EquityChart data={equity} />
      </div>

      {/* Rolling comparison */}
      <div className="da-section">
        <div className="da-section-title"><Clock size={13} /> ROLLING PERFORMANCE</div>
        <div className="da-rolling-grid">
          {['last_10', 'last_20', 'last_50'].map(k => {
            const r = rolling[k] || {};
            const label = { last_10: '10', last_20: '20', last_50: '50' }[k];
            return (
              <div key={k} className="da-rolling">
                <div className="da-rolling-title">Letzte {label}</div>
                <div className="da-rolling-row"><span>Trades</span><b>{r.trades || 0}</b></div>
                <div className="da-rolling-row"><span>WR</span><b>{pct(r.win_rate, 0)}</b></div>
                <div className="da-rolling-row"><span>PF</span><b>{r.profit_factor_inf ? '∞' : fmt(r.profit_factor, 2)}</b></div>
                <div className="da-rolling-row"><span>Expectancy</span><b className={(r.expectancy || 0) >= 0 ? 'da-pos' : 'da-neg'}>{fmt(r.expectancy, 3)}</b></div>
                <div className="da-rolling-row"><span>PnL</span><b className={(r.total_pnl || 0) >= 0 ? 'da-pos' : 'da-neg'}>{fmt(r.total_pnl, 2)}</b></div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Strategy health */}
      <div className="da-section">
        <div className="da-section-title"><Lightning size={13} /> STRATEGIE-BEWERTUNG</div>
        {(!health?.strategies || health.strategies.length === 0) && (
          <div className="da-empty">Noch keine geschlossenen Trades pro Strategie.</div>
        )}
        <div className="da-strat-list">
          {(health?.strategies || []).map(row => (
            <StrategyCard
              key={row.strategy_id}
              row={row}
              expanded={!!expanded[row.strategy_id]}
              detail={details[row.strategy_id]}
              loadingDetail={!!loadingDetailIds[row.strategy_id]}
              onOpen={openStrategy}
            />
          ))}
        </div>
      </div>

      {/* KI review */}
      <div className="da-section">
        <div className="da-section-title"><ChartBar size={13} /> KI-REVIEW</div>
        <div className="da-review-box">
          <pre className="da-review">{kiReview || 'Noch nicht genug Daten für ein Review.'}</pre>
          <button className="da-review-copy" onClick={copyReview} data-testid="da-review-copy" disabled={!kiReview}>Review kopieren</button>
        </div>
      </div>

      {/* Trade breakdown */}
      <div className="da-section">
        <div className="da-section-title"><Target size={13} /> LETZTE TRADES (klick für Details)</div>
        <div className="da-trade-list">
          {(overview?.equity_curve || []).slice(-30).reverse().map(pt => (
            <MiniTradeLoader key={pt.trade_id} tradeId={pt.trade_id} symbol={pt.symbol} closedAt={pt.closed_at} pnl={pt.pnl} onLoadDetail={loadTradeDetail} />
          ))}
          {(!overview?.equity_curve || overview.equity_curve.length === 0) && (
            <div className="da-empty">Noch keine Trades.</div>
          )}
        </div>
      </div>
    </div>
  );
};

// Fetches minimal trade info on demand (avoids a second full-trades call)
const MiniTradeLoader = ({ tradeId, symbol, closedAt, pnl, onLoadDetail }) => {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const toggle = async () => {
    const nx = !open; setOpen(nx);
    if (nx && !detail) {
      const d = await onLoadDetail(tradeId);
      setDetail(d);
    }
  };
  const pnlCls = pnl > 0 ? 'da-pos' : pnl < 0 ? 'da-neg' : '';
  return (
    <div className="da-trade">
      <button className="da-trade-head" onClick={toggle} data-testid={`da-mini-toggle-${tradeId}`}>
        {open ? <CaretDown size={12} /> : <CaretRight size={12} />}
        <span className="da-symbol">{(symbol || '').replace('USDT', '')}</span>
        {detail && (
          <>
            <span className={`da-badge ${detail.side === 'LONG' ? 'da-long' : 'da-short'}`}>{detail.side}</span>
            <span className="da-strat">{detail.strategy_name || detail.strategy_id}</span>
            <span className="da-reason">{detail.exit_reason}</span>
          </>
        )}
        <span className={`da-pnl ${pnlCls}`}>{pnl >= 0 ? '+' : ''}{fmt(pnl, 4)}</span>
        <span className="da-time">{closedAt ? new Date(closedAt).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin' }) : '–'}</span>
      </button>
      {open && (
        <div className="da-trade-body">
          {!detail && <div className="da-loading">Lade Details…</div>}
          {detail && (
            <div className="da-kv-grid">
              <div><span>Haltedauer</span><b>{detail.hold_human || '–'}</b></div>
              <div><span>R-Multiple</span><b className={detail.r_multiple >= 0 ? 'da-pos' : 'da-neg'}>{detail.r_multiple ?? '–'} R</b></div>
              <div><span>CRV geplant</span><b>{detail.planned_crv ?? '–'}</b></div>
              <div><span>CRV real</span><b>{detail.realized_crv ?? '–'}</b></div>
              <div><span>Exit-Grund</span><b>{detail.exit_reason}</b></div>
              <div><span>Gebühren</span><b>{fmt(detail.fee_amount, 4)} USDT</b></div>
              <div><span>Gebühren % PnL</span><b>{detail.fee_pct_of_pnl != null ? pct(detail.fee_pct_of_pnl, 1) : '–'}</b></div>
              <div><span>PnL nach Gebühren</span><b className={detail.pnl_after_fees >= 0 ? 'da-pos' : 'da-neg'}>{fmt(detail.pnl_after_fees, 4)}</b></div>
              <div><span>Entry → Exit</span><b className="da-mono">{fmt(detail.entry, 4)} → {fmt(detail.exit_price, 4)}</b></div>
              <div><span>SL init/final</span><b className="da-mono">{fmt(detail.initial_sl, 4)} → {fmt(detail.sl, 4)}</b></div>
              <div><span>TP1 / Voll</span><b className="da-mono">{fmt(detail.tp1, 4)} / {fmt(detail.tpf, 4)}</b></div>
              <div><span>Hebel · Qty</span><b>{detail.leverage}x · {fmt(detail.qty, 4)}</b></div>
              <div><span>Notional</span><b>{fmt(detail.position_notional, 2)} USDT</b></div>
              <div><span>Zeit (Berlin)</span><b>{detail.entry_weekday_name || '–'} · {detail.entry_hour != null ? String(detail.entry_hour).padStart(2, '0') + ':00' : '–'}</b></div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default DeepAnalytics;
