import React, { useState, useEffect, useCallback } from 'react';
import { TrendUp, TrendDown, Target, Clock, ChartBar, Lightning, CheckCircle, XCircle, Trash, Warning, CaretDown, Plus } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders, isAdmin } from '../auth';
import NewTradeModal from './NewTradeModal';
import TradeAIDetails, { SETUP_EXPLAIN } from './TradeAIDetails';
import './PerformanceAnalytics.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmtTime = (iso) => {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
      second: '2-digit', timeZone: 'Europe/Berlin',
    });
  } catch { return '—'; }
};

const fmtDur = (s) => {
  if (s == null) return '—';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
};

const fmtPct = (p) => (p == null ? '' : `${p > 0 ? '+' : ''}${p}%`);

// Kompakte Uhrzeit für die Trade-Liste: "14.06. 13:42"
const fmtTimeShort = (iso) => {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
      timeZone: 'Europe/Berlin',
    }).replace(',', '');
  } catch { return ''; }
};

// One price level row in the ladder (Entry / SL / TP1 / Full-TP / Exit)
const LevelRow = ({ label, value, pct, cls, hit }) => {
  if (value == null || value === 0) return null;
  return (
    <div className={`lvl-row ${cls || ''}`}>
      <span className="lvl-label">{label}{hit ? ' ✓' : ''}</span>
      <span className="lvl-value mono">{value}</span>
      {pct != null && <span className="lvl-pct mono">{fmtPct(pct)}</span>}
    </div>
  );
};

// Offener Trade: nur noch der Schließen-Button (das frühere "Trade steuern"-
// Interface wurde auf Nutzerwunsch entfernt – SL/TP managt die KI bzw. Bitunix).
const OpenTradeActions = ({ t, onChanged }) => {
  const [busy, setBusy] = useState(false);

  const closeNow = async () => {
    if (!window.confirm(`${t.side} ${t.symbol} wirklich komplett schließen?`)) return;
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/autotrade/close/${t.id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Aktion fehlgeschlagen');
      toast.success(`Trade geschlossen · PnL ${data.result?.realized_pnl ?? '–'} USDT`);
      onChanged && onChanged();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="ota" data-testid={`open-trade-actions-${t.id}`}>
      <button className="ota-btn ota-close" onClick={closeNow} disabled={busy}
        data-testid={`ota-close-${t.id}`}>
        {busy ? 'Schließt…' : 'Trade schließen'}
      </button>
    </div>
  );
};

const TradeDetailCard = ({ t, stratName, getCoinName, onChanged, onShowChart }) => {
  const [open, setOpen] = useState(false);
  const c = t.computed || {};
  const isLive = t.mode === 'live';
  const closed = t.status === 'closed';
  const resultMeta = t.result === 'win'
  ? { label: 'G', cls: 'res-win' }
  : t.result === 'loss'
    ? { label: 'V', cls: 'res-loss' }
    : t.result === 'breakeven'
      ? { label: 'BEP', cls: 'res-be' }
      : { label: 'OFFEN', cls: 'res-open' };
  const pnl = (!closed && c.live_pnl != null) ? c.live_pnl : (t.realized_pnl || 0);
  // Eine PnL-%-Quelle für ALLE Anzeigen (Trade-Liste, Chart-Badge, Bitunix):
  // offen = unrealisierter PnL in % der Margin (exakt wie Bitunix),
  // geschlossen = realisierter PnL in % der Margin.
  const pnlPct = closed
    ? (c.pnl_pct_margin ?? c.pnl_pct)
    : (c.upnl_pct_margin ?? c.pnl_pct_margin ?? c.pnl_pct);

  return (
    <div className={`tdc ${open ? 'tdc-open' : ''}`} data-testid={`trade-card-${t.id}`}>
      <button className="tdc-head" onClick={() => setOpen(o => !o)} data-testid={`trade-card-toggle-${t.id}`}>
  <span className="tdc-main">
    <span className={`mode-tag ${isLive ? 'mode-live' : 'mode-paper'}`} data-testid={`trade-mode-${t.id}`}>
      {isLive ? 'LIVE' : 'PAPER'}
    </span>
    <span className={`badge ${t.side === 'LONG' ? 'badge-long' : 'badge-short'}`}>{t.side}</span>
    <span className={`tdc-result ${resultMeta.cls}`}>{resultMeta.label}</span>
    <span className="mono text-secondary tdc-coin">{getCoinName(t.symbol)}</span>
    {t.horizon === 'swing' && <sup className="tdc-sup tag-swing" title={`Swing-Trade${t.runner ? ' · Runner' : ''}`} data-testid={`trade-swing-badge-${t.id}`}>S{t.runner ? '·R' : ''}</sup>}
    {t.data_collection && <sup className="tdc-sup tag-daten" title="Datensammel-Modus: reiner Paper-Trade zum ML-Datensammeln (zählt nicht zur Live-Performance)" data-testid={`trade-collection-badge-${t.id}`}>D</sup>}
    <span className="tdc-pnl-group">
      <span className={`mono tdc-pnl ${pnl >= 0 ? 'text-long' : 'text-short'}`}>{pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}</span>
      <CaretDown size={13} className={`tdc-caret ${open ? 'rot' : ''}`} />
    </span>
  </span>
  <span className="tdc-sub">
    {pnlPct != null && (
      <span className={`mono tdc-pnl-pct ${pnlPct >= 0 ? 'text-long' : 'text-short'}`} data-testid={`trade-pnl-pct-${t.id}`}>
        ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)
      </span>
    )}
  </span>
  <span className="tdc-strat-line">
    <span className="tdc-strat-name" title={stratName(t)}>{stratName(t)}</span>
    <span className="tdc-times mono" title={closed ? 'Eröffnet → Geschlossen (Berlin-Zeit)' : 'Eröffnet (Berlin-Zeit)'} data-testid={`trade-times-${t.id}`}>
      {fmtTimeShort(t.opened_at)}{closed ? ` → ${fmtTimeShort(t.closed_at)}` : ''}
    </span>
  </span>
</button>

      {open && (
        <div className="tdc-body" data-testid={`trade-card-body-${t.id}`}>
          {onShowChart && (
            <button className="tdc-chart-btn" onClick={() => onShowChart(t.symbol)}
              title={`Live-Chart von ${getCoinName(t.symbol)} im Hauptfenster öffnen, um den Trade genauer anzusehen`}
              data-testid={`trade-open-chart-${t.id}`}>
              <ChartBar size={13} weight="bold" /> Live-Chart {getCoinName(t.symbol)} öffnen
            </button>
          )}
          <div className="tdc-ladder">
            <LevelRow label={`TP Full (${c.rr_tpf || '?'}R)`} value={t.tpf} pct={c.tpf_distance_pct} cls="lvl-tp" />
            <LevelRow label={`TP1 (${c.rr_tp1 || '?'}R)`} value={t.tp1} pct={c.tp1_distance_pct} cls="lvl-tp1" hit={t.tp1_hit} />
            <LevelRow label="Entry" value={t.entry} pct={0} cls="lvl-entry" />
            {!closed && c.current_price != null && (
              <LevelRow label="Aktueller Kurs" value={c.current_price} pct={c.price_distance_pct} cls="lvl-current" />
            )}
            {closed && <LevelRow label="Exit" value={t.exit_price} pct={c.exit_distance_pct} cls="lvl-exit" />}
            <LevelRow label={`SL${c.sl_moved ? ' (aktuell)' : ''}`} value={t.sl} pct={c.sl_distance_pct} cls="lvl-sl" />
            {c.sl_moved ? <LevelRow label="SL initial" value={t.initial_sl} pct={c.initial_sl_distance_pct} cls="lvl-sl-init" /> : null}
          </div>

          <div className="tdc-meta">
            <div className="tdc-meta-item" title="Zeitpunkt, zu dem der Trade eröffnet wurde."><span>Eröffnet</span><b className="mono">{fmtTime(t.opened_at)}</b></div>
            {closed && <div className="tdc-meta-item" title="Zeitpunkt, zu dem der Trade komplett geschlossen wurde."><span>Geschlossen</span><b className="mono">{fmtTime(t.closed_at)}</b></div>}
            {!closed && c.current_price != null && (
              <div className="tdc-meta-item" title="Letzter Live-Preis des Coins – Basis für den unrealisierten PnL." data-testid={`trade-current-price-${t.id}`}><span>Aktueller Kurs</span><b className="mono">{c.current_price}</b></div>
            )}
            {!closed && c.unrealized_pnl != null && (
              <div className="tdc-meta-item" title="Gewinn/Verlust in $, wenn du JETZT zum aktuellen Kurs schließen würdest – OHNE Gebühren. (Kurs − Entry) × Menge, bei Short umgekehrt."><span>Unrealisierter PnL</span><b className={`mono ${c.unrealized_pnl >= 0 ? 'text-long' : 'text-short'}`} data-testid={`trade-unrealized-pnl-${t.id}`}>{c.unrealized_pnl >= 0 ? '+' : ''}{c.unrealized_pnl.toFixed(2)} $</b></div>
            )}
            {!closed && c.live_pnl != null && (
              <div className="tdc-meta-item" title="Wie Unrealisierter PnL, aber abzüglich aller schon angefallenen Gebühren (Entry-Fee, ggf. TP1-Teilverkauf) – der ehrliche „was bleibt wirklich übrig“-Wert."><span>Live PnL (inkl. Gebühren)</span><b className={`mono ${c.live_pnl >= 0 ? 'text-long' : 'text-short'}`} data-testid={`trade-live-pnl-${t.id}`}>{c.live_pnl >= 0 ? '+' : ''}{c.live_pnl.toFixed(2)} $</b></div>
            )}
            <div className="tdc-meta-item" title="Wie lange der Trade offen ist bzw. war."><span>Dauer</span><b className="mono">{fmtDur(c.duration_seconds)}</b></div>
            <div className="tdc-meta-item" title="PnL gemessen in „R“ = geplantes Risiko (Abstand Entry → initialer SL × Menge). +1R = einen Risiko-Einsatz gewonnen, −1R = SL voll getroffen. Macht Trades unterschiedlicher Größe vergleichbar – die KI wird u.a. daran gemessen."><span>R-Vielfaches</span><b className={`mono ${(c.r_multiple || 0) >= 0 ? 'text-long' : 'text-short'}`}>{c.r_multiple != null ? `${c.r_multiple}R` : '—'}</b></div>
            {!closed && c.upnl_pct_margin != null && (
              <div className="tdc-meta-item" title="Unrealisierter PnL in % der eingesetzten Margin – exakt die Zahl, die Bitunix in der Position anzeigt. Der Hebel wirkt hier voll: 1% Kursbewegung × Hebel 10 = 10%."><span>uPnL % auf Margin (wie Bitunix)</span><b className={`mono ${(c.upnl_pct_margin || 0) >= 0 ? 'text-long' : 'text-short'}`} data-testid={`trade-upnl-pct-margin-${t.id}`}>{fmtPct(c.upnl_pct_margin)}</b></div>
            )}
            <div className="tdc-meta-item" title="PnL (inkl. Gebühren) in % der eingesetzten Margin – dein tatsächlicher Return auf das gebundene Kapital."><span>PnL % auf Margin</span><b className={`mono ${(c.pnl_pct_margin || 0) >= 0 ? 'text-long' : 'text-short'}`} data-testid={`trade-pnl-pct-margin-${t.id}`}>{c.pnl_pct_margin != null ? fmtPct(c.pnl_pct_margin) : '—'}</b></div>
            <div className="tdc-meta-item" title="PnL in % des gesamten Positionswerts (Margin × Hebel) – entspricht der reinen Kursbewegung, ohne Hebel-Effekt."><span>PnL % Positionsgröße</span><b className={`mono ${(c.pnl_pct || 0) >= 0 ? 'text-long' : 'text-short'}`} data-testid={`trade-meta-pnl-pct-${t.id}`}>{c.pnl_pct != null ? fmtPct(c.pnl_pct) : '—'}</b></div>
            <div className="tdc-meta-item" title="PnL in % des Strategie-Kapitals – zeigt, wie stark dieser eine Trade das Gesamtkapital bewegt hat."><span>PnL % Kapital</span><b className={`mono ${(c.pnl_pct_capital || 0) >= 0 ? 'text-long' : 'text-short'}`}>{c.pnl_pct_capital != null ? fmtPct(c.pnl_pct_capital) : '—'}</b></div>
            <div className="tdc-meta-item" title="Geplantes Risiko in $: Abstand Entry → initialer SL × Menge. Das verlierst du (plus Gebühren), wenn der SL voll trifft."><span>Risk</span><b className="mono">{c.risk_usd ? `${c.risk_usd} $` : '—'}</b></div>
            {(() => {
              const risk = (t.qty && t.entry && (t.initial_sl || t.sl))
                ? Math.abs(t.entry - (t.initial_sl || t.sl)) * t.qty : 0;
              const fees = closed ? (t.fees_paid || 0) : (t.fees_paid || 0) * 2;
              if (!risk || !fees) return null;
              const ratio = fees / risk * 100;
              const color = ratio >= 50 ? 'var(--short, #f6465d)' : ratio >= 25 ? '#f0b90b' : 'var(--long, #0ecb81)';
              return (
                <div className="tdc-meta-item" title={`Roundtrip-Gebühren${closed ? '' : ' (geschätzt: 2× Entry-Fee)'} im Verhältnis zum geplanten Risiko (Entry→initialer SL). Über 50% heißt: Fees fressen den Großteil des geplanten Risikos – der Stop war zu eng oder das Notional zu groß.`}>
                  <span>Fees vs. Risiko</span>
                  <b className="mono" style={{ color }} data-testid={`trade-fees-vs-risk-${t.id}`}>
                    ≈{ratio.toFixed(0)}% ({fees.toFixed(2)} $)
                  </b>
                </div>
              );
            })()}
            <div className="tdc-meta-item" title="Gewählter Hebel: Positionswert = Kapital × Hebel. Höherer Hebel = gleiche Kursbewegung wirkt stärker auf die Margin – und die Gebühren steigen mit, weil sie auf den vollen Positionswert berechnet werden."><span>Hebel</span><b className="mono">{t.leverage ? `${t.leverage}x` : '—'}</b></div>
            <div className="tdc-meta-item" title="Eingesetzte Margin in $ – das für diesen Trade gebundene Kapital."><span>Kapital</span><b className="mono">{t.max_capital ? `${t.max_capital} $` : '—'}</b></div>
            <div className="tdc-meta-item" title="Gehandelte Stückzahl des Coins: Positionswert (Kapital × Hebel) ÷ Entry-Preis."><span>Menge</span><b className="mono">{t.qty ?? '—'}</b></div>
            <div className="tdc-meta-item" title="Ob das erste Teilziel (TP1) erreicht wurde: dort wird ein Teil der Position verkauft und der SL Richtung Einstand gezogen – Gewinn sichern, Rest Richtung TP Full laufen lassen."><span>TP1 getroffen</span><b className="mono">{t.tp1_hit ? 'Ja' : 'Nein'}</b></div>
          </div>

          {(t.ai_reasoning || t.setup || t.decision_id || t.strategy_id === 'ai_trader') && (
            <div className="tdc-ai" data-testid={`trade-ai-context-${t.id}`}>
              <div className="tdc-tl-title">KI-BEGRÜNDUNG</div>
              <div className="tdc-ai-tags">
                {t.setup && <span className="badge badge-setup" title={SETUP_EXPLAIN[t.setup] || 'Gehandeltes Setup aus dem Strategie-Playbook der KI'} data-testid={`trade-ai-setup-${t.id}`}>{String(t.setup).replace(/_/g, ' ')}</span>}
                {t.ai_confidence != null && <span className="badge" title="Wie sicher sich die KI bei diesem Entry war (0-100%). Nur Entscheidungen über der eingestellten Mindest-Konfidenz werden überhaupt gehandelt.">Konfidenz {t.ai_confidence}%</span>}
                {t.ai_news_impact && t.ai_news_impact !== 'neutral' && <span className="badge" title="Einschätzung der Nachrichtenlage zum Entry-Zeitpunkt: bullish/positive = News sprachen für steigende Kurse, bearish/negative = für fallende. Die konkreten Schlagzeilen stehen in der vollen Begründung (Details).">News: {t.ai_news_impact}</span>}
              </div>
              {t.ai_reasoning && <div className="tdc-ai-text" data-testid={`trade-ai-reasoning-${t.id}`}>{t.ai_reasoning}</div>}
              <TradeAIDetails trade={t} onChanged={onChanged} />
            </div>
          )}
          {(t.events || []).length > 0 && (
            <div className="tdc-timeline" data-testid={`trade-timeline-${t.id}`}>
              <div className="tdc-tl-title">VERLAUF</div>
              {t.events.map((ev, i) => (
                <div key={i} className="tdc-tl-item"><span className="tdc-tl-dot" />{ev}</div>
              ))}
            </div>
          )}

          {!closed && isAdmin() && (
            <OpenTradeActions t={t} onChanged={onChanged} />
          )}
        </div>
      )}
    </div>
  );
};

const CLEAR_RANGES = [
  { key: 'hour', label: 'Letzte Stunde' },
  { key: '24h', label: 'Letzte 24 Stunden' },
  { key: '7d', label: 'Letzte 7 Tage' },
  { key: '4w', label: 'Letzte 4 Wochen' },
  { key: 'all', label: 'Gesamter Zeitraum (alles)' },
];

const PerformanceAnalytics = ({ performance, strategies = [], enabledIds = [], signals, selectedCoin, selectedStrategy, strategyOverrides = {}, strategyCoinConfigs = {}, isAdmin, onNeedAdmin, onCleared, onShowChart }) => {
  const [view, setView] = useState('overview');
  const [timeAnalytics, setTimeAnalytics] = useState(null);
  const [trades, setTrades] = useState([]);
  const [tradeOnlyCoin, setTradeOnlyCoin] = useState(false);
  const [showNewTrade, setShowNewTrade] = useState(false);
  const [balance, setBalance] = useState(null);
  const [showClear, setShowClear] = useState(false);
  const [auditLog, setAuditLog] = useState([]);
  const [clearRange, setClearRange] = useState('24h');
  const [clearScope, setClearScope] = useState('all');
  const [clearPreview, setClearPreview] = useState(null);
  const [clearing, setClearing] = useState(false);
  const [pnlFilter, setPnlFilter] = useState('all');
  // Zeit-Analyse: Strategie-Filter ('' = Coin gesamt) + Ansicht (Uhrzeiten/Wochentage/Kombi)
  const [timeStrategy, setTimeStrategy] = useState('');
  const [timeView, setTimeView] = useState('hours');

  const getCoinName = (s) => s?.replace('USDT', '') || '';
  const stratName = (t) => t?.strategy_name || strategies.find(s => s.id === t?.strategy_id)?.name || t?.strategy_id || '—';

  // Resolve the auto-trade mode of the SELECTED strategy for the SELECTED coin
  // (per-strategy-per-coin config wins, falls back to strategy-level override).
  const resolveStrategyMode = (strategyId, coin) => {
    if (!strategyId) return 'off';
    const perCoin = strategyCoinConfigs?.[strategyId]?.[coin];
    if (perCoin && perCoin.mode) return perCoin.mode; // 'live' | 'paper' | 'off'
    const override = strategyOverrides?.[strategyId];
    if (!override || !override.enabled || override.mode === 'off') return 'off';
    return override.mode || 'off';
  };

  const activeStrategyName = strategies.find(s => s.id === selectedStrategy)?.name || '—';
  const activeMode = resolveStrategyMode(selectedStrategy, selectedCoin);
  const bannerMeta = {
    live:  { cls: 'mode-live',  label: 'ECHTGELD · LIVE',     pill: 'LIVE',  head: 'AKTIV' },
    paper: { cls: 'mode-paper', label: 'SIMULATION · PAPER',  pill: 'PAPER', head: 'AKTIV' },
    off:   { cls: 'mode-off',   label: 'DEAKTIVIERT · AUS',   pill: 'AUS',   head: 'INAKTIV' },
  };
  const banner = bannerMeta[activeMode] || bannerMeta.off;

  const stratSignals = signals.filter(s => !selectedStrategy || s.strategy_id === selectedStrategy);
  const totalSignals = stratSignals.length;
  const longSignals = stratSignals.filter(s => s.type === 'LONG').length;
  const shortSignals = stratSignals.filter(s => s.type === 'SHORT').length;
  const wins = stratSignals.filter(s => s.result === 'win').length;
  const losses = stratSignals.filter(s => s.result === 'loss').length;
  const decided = wins + losses;
  const winRate = decided ? Math.round(wins / decided * 100) : 0;

  // Trades heute (opened_at seit Mitternacht Europe/Berlin, optional nach aktiver Strategie gefiltert)
  const startOfTodayMs = (() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  })();
  const tradesToday = trades.filter(t => {
    const raw = t.opened_at || t.created_at || t.entry_time || t.timestamp;
    if (!raw) return false;
    const ts = typeof raw === 'number' ? raw : new Date(raw).getTime();
    if (!Number.isFinite(ts) || ts < startOfTodayMs) return false;
    if (selectedStrategy && t.strategy_id && t.strategy_id !== selectedStrategy) return false;
    return true;
  }).length;

  const totalWins = performance.reduce((a, p) => a + (p.wins || 0), 0);
  const totalLosses = performance.reduce((a, p) => a + (p.losses || 0), 0);
  const globalDecided = totalWins + totalLosses;
  const globalWinRate = globalDecided ? Math.round(totalWins / globalDecided * 100) : 0;

  // TOP COINS: dauerhaft beste Win-Rates NUR für die aktive Strategie
  // (aus performance.by_strategy – kumulierte, dauerhafte Daten je Coin+Strategie)
  const topPerformers = performance.map(p => {
    const st = (p.by_strategy || {})[selectedStrategy];
    if (!st || !(st.total > 0)) return null;
    const wins = st.wins || 0;
    const losses = st.losses || 0;
    const decided = wins + losses;
    return {
      symbol: p.symbol,
      total_signals: st.total,
      wins, losses, decided,
      win_rate: decided ? (wins / decided) * 100 : 0,
    };
  }).filter(p => p && p.decided > 0)
    .sort((a, b) => (b.win_rate - a.win_rate) || (b.decided - a.decided))
    .slice(0, 5);

  const loadTrades = useCallback(() => {
    fetch(`${API_URL}/api/autotrade/trades?limit=200`).then(r => r.json()).then(d => setTrades(d.trades || [])).catch(() => {});
    fetch(`${API_URL}/api/autotrade/balance`).then(r => r.json()).then(setBalance).catch(() => {});
  }, []);

  useEffect(() => {
    if (view === 'time-based' && selectedCoin) {
      const q = timeStrategy ? `?strategy_id=${encodeURIComponent(timeStrategy)}` : '';
      fetch(`${API_URL}/api/analytics/time-based/${selectedCoin}${q}`).then(r => r.json()).then(setTimeAnalytics).catch(() => {});
    }
    if (view === 'trades') { loadTrades(); const iv = setInterval(loadTrades, 15000); return () => clearInterval(iv); }
    // Trades werden auch in der Übersicht benötigt (für "Trades heute"-Zähler)
    if (view === 'overview') { loadTrades(); const iv = setInterval(loadTrades, 15000); return () => clearInterval(iv); }
  }, [view, selectedCoin, timeStrategy, loadTrades]);

  // Globaler Live/Paper-Filter (obere Auswahl) für den GESAMTEN Analyse-Bereich
  const filterFn = (t) => pnlFilter === 'all' || (pnlFilter === 'live' ? t.mode === 'live' : t.mode !== 'live');
  const openTrades = trades.filter(t => t.status === 'open' && filterFn(t));
  const closedTrades = trades.filter(t => t.status === 'closed' && filterFn(t));

  // Listen-Filter: optional nur der aktuell ausgewählte Coin (nur Anzeige)
  const listFilterFn = (t) => !tradeOnlyCoin || t.symbol === selectedCoin;
  const openList = openTrades.filter(listFilterFn);
  const closedList = closedTrades.filter(listFilterFn);

  // Coin-specific slices (for currently selected coin)
  const coinClosedTrades = closedTrades.filter(t => t.symbol === selectedCoin);
  const coinOpenTrades = openTrades.filter(t => t.symbol === selectedCoin);

  const pnlTotal = pnlFilter === 'all'
    ? (balance?.realized_pnl || 0)
    : closedTrades.reduce((a, t) => a + (t.realized_pnl || 0), 0);
  const coinPnl = coinClosedTrades.reduce((a, t) => a + (t.realized_pnl || 0), 0);

  // Performance je Strategie für den GEWÄHLTEN COIN.
  // Wir starten mit ALLEN aktivierten Strategien (damit KI Trader garantiert
  // erscheint, auch wenn er noch keine Trades hat) und mergen dann die
  // echten Trade-Daten ein. Strategien ohne Trades werden ausgeblendet,
  // AUSSER dem KI Trader (der bleibt immer sichtbar).
  const activeStrategies = strategies.filter(s => enabledIds.includes(s.id));
  const activeStratList = activeStrategies.length ? activeStrategies : strategies;

  const stratRowsMap = {};
  // Seed mit aktivierten Strategien (inkl. ai_trader)
  activeStratList.forEach(s => {
    stratRowsMap[s.id] = {
      id: s.id,
      name: s.name || s.id,
      wins: 0, losses: 0, total: 0, openCount: 0, pnl: 0,
      alwaysShow: s.id === 'ai_trader',
    };
  });
  [...closedTrades, ...openTrades].forEach(t => {
    if (t.symbol !== selectedCoin) return;
    const sid = t.strategy_id || 'unknown';
    if (!stratRowsMap[sid]) {
      stratRowsMap[sid] = {
        id: sid,
        name: stratName(t),
        wins: 0, losses: 0, total: 0, openCount: 0, pnl: 0,
        alwaysShow: sid === 'ai_trader',
      };
    }
    const e = stratRowsMap[sid];
    if (t.status === 'open') {
      e.openCount += 1;
    } else {
      e.total += 1;
      if (t.result === 'win') e.wins += 1;
      else if (t.result === 'loss') e.losses += 1;
      e.pnl += t.realized_pnl || 0;
    }
  });
  const stratRows = Object.values(stratRowsMap)
    // Strategien ohne Trades ausblenden – KI Trader bleibt immer sichtbar
    .filter(e => e.alwaysShow || e.total > 0 || e.openCount > 0)
    .map(e => {
      const decided = e.wins + e.losses;
      return { ...e, wr: decided ? Math.round((e.wins / decided) * 100) : 0 };
    })
    .sort((a, b) => (b.total + b.openCount) - (a.total + a.openCount));

  const openClear = () => {
    if (!isAdmin) { onNeedAdmin && onNeedAdmin(); return; }
    if ((clearScope === 'coin_strategy' || clearScope === 'strategy') && !selectedStrategy) setClearScope('all');
    setShowClear(true);
  };

  // Bestätigungs-Vorschau: wie viele Einträge sind betroffen? (rein lesend)
  const clearPayload = () => {
    const payload = { range: clearRange, scope: clearScope };
    if (clearScope === 'coin' || clearScope === 'coin_strategy') payload.symbol = selectedCoin;
    if (clearScope === 'coin_strategy' || clearScope === 'strategy') payload.strategy_id = selectedStrategy;
    return payload;
  };

  useEffect(() => {
    if (!showClear) return;
    let alive = true;
    setClearPreview(null);
    (async () => {
      try {
        const res = await fetch(`${API_URL}/api/analytics/clear/preview`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(clearPayload()),
        });
        const data = await res.json();
        if (alive && res.ok) setClearPreview(data);
      } catch { /* Vorschau ist optional */ }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showClear, clearRange, clearScope, selectedCoin, selectedStrategy]);

  // Audit-Log: wer hat wann was gelöscht (nur Admin, rein lesend)
  useEffect(() => {
    if (!showClear) return;
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`${API_URL}/api/audit-log?limit=8`, { headers: authHeaders() });
        if (alive && res.ok) setAuditLog((await res.json()).entries || []);
      } catch { /* optional */ }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showClear]);

  const runClear = async () => {
    setClearing(true);
    try {
      const payload = clearPayload();
      const res = await fetch(`${API_URL}/api/analytics/clear`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const data = await res.json();
        const total = Object.values(data.deleted || {}).reduce((a, b) => a + b, 0);
        toast.success(`Analyse-Daten gelöscht (${total} Einträge · ${scopeLabel})`);
        setShowClear(false);
        onCleared && onCleared();
      } else if (res.status === 401) {
        toast.error('Admin-Login erforderlich');
        onNeedAdmin && onNeedAdmin();
      } else {
        toast.error('Fehler beim Löschen');
      }
    } catch {
      toast.error('Verbindungsfehler');
    } finally {
      setClearing(false);
    }
  };

  const rangeLabel = CLEAR_RANGES.find(r => r.key === clearRange)?.label || '';

  const clearScopeOptions = [
    { key: 'all', label: 'Alle Coins & Strategien', disabled: false },
    { key: 'coin', label: `Nur ${getCoinName(selectedCoin)}`, disabled: !selectedCoin },
    {
      key: 'coin_strategy',
      label: `Nur "${activeStrategyName}" bei ${getCoinName(selectedCoin)}`,
      disabled: !selectedStrategy || !selectedCoin,
      hint: !selectedStrategy ? 'Keine Strategie ausgewählt' : null,
    },
    {
      key: 'strategy',
      label: `Strategie "${activeStrategyName}" – ALLE Coins`,
      disabled: !selectedStrategy,
      hint: !selectedStrategy ? 'Keine Strategie ausgewählt' : null,
    },
  ];
  const scopeLabel = clearScope === 'coin'
    ? `nur ${getCoinName(selectedCoin)}`
    : clearScope === 'coin_strategy'
      ? `nur "${activeStrategyName}" bei ${getCoinName(selectedCoin)}`
      : clearScope === 'strategy'
        ? `"${activeStrategyName}" über alle Coins`
        : 'alle Coins & Strategien';

  return (
    <div className="performance-analytics" data-testid="performance-analytics">
      <div className="analytics-header">
        <div><h3>ANALYSE</h3><div className="analytics-subtitle">Live Statistics</div></div>
        <button className="clear-data-btn" onClick={openClear} data-testid="clear-analytics-btn" title="Analyse-Daten löschen">
          <Trash size={15} weight="bold" />
        </button>
      </div>

      <div className="view-switcher">
        <button className={`view-btn ${view === 'overview' ? 'active' : ''}`} onClick={() => setView('overview')} data-testid="view-overview"><ChartBar size={14} />Übersicht</button>
        <button className={`view-btn ${view === 'trades' ? 'active' : ''}`} onClick={() => setView('trades')} data-testid="view-trades"><Lightning size={14} />Trades</button>
        <button className={`view-btn ${view === 'time-based' ? 'active' : ''}`} onClick={() => setView('time-based')} data-testid="view-time-based"><Clock size={14} />Zeit</button>
      </div>

      {view === 'overview' && (
        <>
          <div className="analytics-section">
            <div className="section-title">HEUTE (aktive Strategie)</div>
            <div className="stats-grid">
              <div className="stat-card"><div className="stat-icon"><Target size={20} className="text-warning" /></div><div className="stat-content"><div className="stat-value mono">{totalSignals}</div><div className="stat-label">Signale</div></div></div>
              <div className="stat-card"><div className="stat-icon"><TrendUp size={20} className="text-long" /></div><div className="stat-content"><div className="stat-value mono text-long">{longSignals}</div><div className="stat-label">Long</div></div></div>
              <div className="stat-card"><div className="stat-icon"><TrendDown size={20} className="text-short" /></div><div className="stat-content"><div className="stat-value mono text-short">{shortSignals}</div><div className="stat-label">Short</div></div></div>
              <div className="stat-card" data-testid="stat-trades-today"><div className="stat-icon"><Lightning size={20} className="text-warning" /></div><div className="stat-content"><div className="stat-value mono">{tradesToday}</div><div className="stat-label">Trades</div></div></div>
              <div className="stat-card"><div className="stat-icon"><CheckCircle size={20} className="text-long" /></div><div className="stat-content"><div className="stat-value mono">{winRate}%</div><div className="stat-label">Win-Rate ({decided})</div></div></div>
            </div>
          </div>

          <div className="analytics-section">
            <div className="section-title">TOP COINS · {activeStrategyName} (dauerhaft)</div>
            <div className="top-coins-list">
              {topPerformers.length === 0 && <div className="no-data">Noch keine dauerhaften Daten für diese Strategie</div>}
              {topPerformers.map((coin, i) => (
                <div key={coin.symbol} className="top-coin-item" data-testid={`top-coin-${coin.symbol}`}>
                  <div className="coin-rank">{i + 1}</div>
                  <div className="coin-info"><div className="coin-name mono">{getCoinName(coin.symbol)}</div>
                    <div className="coin-signals"><span className="text-long mono">{coin.wins}W</span><span className="text-muted">/</span><span className="text-short mono">{coin.losses}L</span></div></div>
                  <div className="coin-crv"><div className="crv-label">WR</div><div className="crv-value mono" style={{ color: (coin.win_rate || 0) >= 50 ? '#00FF66' : '#FF3366' }}>{(coin.win_rate || 0).toFixed(0)}%</div></div>
                </div>
              ))}
            </div>
          </div>

          <div className="analytics-section">
            <div className="section-title">GESAMT-ANALYSE (dauerhaft)</div>
            <div className="global-stats">
              <div className="global-stat"><span className="text-long mono">{totalWins}</span><span className="text-muted">Wins</span></div>
              <div className="global-stat"><span className="text-short mono">{totalLosses}</span><span className="text-muted">Losses</span></div>
              <div className="global-stat"><span className="mono" style={{ color: globalWinRate >= 50 ? '#00FF66' : '#FF3366' }}>{globalWinRate}%</span><span className="text-muted">Win-Rate</span></div>
            </div>
          </div>
        </>
      )}

      {view === 'trades' && (
        <>
          <div className={`mode-banner ${banner.cls}`} data-testid="active-mode-banner">
            <div className="mode-banner-dot" />
            <div className="mode-banner-text">
              <span className="mode-banner-label">{banner.head} · {activeStrategyName} · {getCoinName(selectedCoin)}</span>
              <span className="mode-banner-value">{banner.label}</span>
            </div>
            <span className={`mode-pill ${banner.cls}`}>{banner.pill}</span>
          </div>

          <div className="pnl-filter" data-testid="pnl-filter">
            <button className={`pnl-filter-btn ${pnlFilter === 'all' ? 'active' : ''}`} onClick={() => setPnlFilter('all')} data-testid="pnl-filter-all">Alle</button>
            <button className={`pnl-filter-btn live ${pnlFilter === 'live' ? 'active' : ''}`} onClick={() => setPnlFilter('live')} data-testid="pnl-filter-live">Live</button>
            <button className={`pnl-filter-btn paper ${pnlFilter === 'paper' ? 'active' : ''}`} onClick={() => setPnlFilter('paper')} data-testid="pnl-filter-paper">Paper</button>
          </div>

          <div className="analytics-section">
            <div className="stats-grid">
              <div className="stat-card" data-testid="pnl-total-card">
                <div className="stat-content">
                  <div className="stat-value mono" style={{ color: pnlTotal >= 0 ? '#00FF66' : '#FF3366' }}>
                    {pnlTotal.toFixed(2)}
                  </div>
                  <div className="stat-label">PnL Gesamt (USDT)</div>
                </div>
              </div>
              <div className="stat-card"><div className="stat-content"><div className="stat-value mono">{openTrades.length}</div><div className="stat-label">Offen</div></div></div>
              <div className="stat-card"><div className="stat-content"><div className="stat-value mono">{closedTrades.length}</div><div className="stat-label">Geschlossen</div></div></div>
            </div>
            <div className="stats-grid" style={{ marginTop: 8 }}>
              <div className="stat-card stat-card-coin" data-testid="pnl-coin-card">
                <div className="stat-content">
                  <div className="stat-value mono" style={{ color: coinPnl >= 0 ? '#00FF66' : '#FF3366' }}>
                    {coinPnl.toFixed(2)}
                  </div>
                  <div className="stat-label">PnL {getCoinName(selectedCoin)} (USDT)</div>
                </div>
              </div>
              <div className="stat-card"><div className="stat-content"><div className="stat-value mono">{coinOpenTrades.length}</div><div className="stat-label">Offen · {getCoinName(selectedCoin)}</div></div></div>
              <div className="stat-card"><div className="stat-content"><div className="stat-value mono">{coinClosedTrades.length}</div><div className="stat-label">Geschl. · {getCoinName(selectedCoin)}</div></div></div>
            </div>
          </div>

          <div className="analytics-section">
            <div className="section-title">
              PERFORMANCE JE STRATEGIE · {getCoinName(selectedCoin)}{pnlFilter !== 'all' ? ` · ${pnlFilter === 'live' ? 'LIVE' : 'PAPER'}` : ''}
            </div>
            {stratRows.length === 0 && <div className="no-data">Noch keine Trades auf {getCoinName(selectedCoin)}</div>}
            {stratRows.map(s => {
              const empty = s.total === 0 && s.openCount === 0;
              return (
                <div key={s.id} className={`strat-perf-row ${empty ? 'strat-perf-empty' : ''}`} data-testid={`strat-perf-${s.id}`}>
                  <div className="strat-perf-name" title={s.name}>{s.name}</div>
                  <div className="strat-perf-stats">
                    {s.openCount > 0 && <span className="mono text-warning" title="offene Trades">{s.openCount}○</span>}
                    <span className="text-long mono">{s.wins}W</span>
                    <span className="text-short mono">{s.losses}L</span>
                    <span className="mono" style={{ color: (s.wins + s.losses) === 0 ? '#5C6070' : (s.wr >= 50 ? '#00FF66' : '#FF3366') }}>
                      {(s.wins + s.losses) === 0 ? '—' : `${s.wr}%`}
                    </span>
                    <span className={`mono ${s.pnl === 0 ? 'text-muted' : (s.pnl >= 0 ? 'text-long' : 'text-short')}`}>
                      {s.pnl.toFixed(2)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="analytics-section">
            <div className="section-title">OFFENE TRADES <span className="sec-count">{openList.length}{openList.length !== openTrades.length ? `/${openTrades.length}` : ''}</span>
              <button className={`trade-filter-coin ${tradeOnlyCoin ? 'active' : ''}`}
                onClick={() => setTradeOnlyCoin(v => !v)}
                title={`Nur Trades des aktuell ausgewählten Coins (${getCoinName(selectedCoin)}) anzeigen – gilt für offene UND geschlossene Trades`}
                data-testid="trade-filter-coin-toggle">
                {tradeOnlyCoin ? '☑' : '☐'} Nur {getCoinName(selectedCoin)}
              </button>
              <button className="new-trade-plus" title="Neuen Trade eröffnen (Live/Paper)"
                onClick={() => (isAdmin ? setShowNewTrade(true) : (onNeedAdmin && onNeedAdmin()))}
                data-testid="open-new-trade-btn">
                <Plus size={13} weight="bold" />
              </button>
            </div>
            {showNewTrade && (
              <NewTradeModal defaultSymbol={selectedCoin}
                onClose={() => setShowNewTrade(false)} onOpened={loadTrades} />
            )}
            {openList.length === 0 && <div className="no-data">Keine offenen Trades{tradeOnlyCoin ? ' (Coin-Filter aktiv)' : ''}</div>}
            {openList.map(t => (
              <TradeDetailCard key={t.id} t={t} stratName={stratName} getCoinName={getCoinName}
                onChanged={loadTrades} onShowChart={onShowChart} />
            ))}
          </div>

          <div className="analytics-section">
            <div className="section-title">GESCHLOSSENE TRADES <span className="sec-count">{closedList.length}{closedList.length !== closedTrades.length ? `/${closedTrades.length}` : ''}</span></div>
            {closedList.length === 0 && <div className="no-data">Keine{tradeOnlyCoin ? ' (Coin-Filter aktiv)' : ''}</div>}
            {closedList.slice(0, 30).map(t => (
              <TradeDetailCard key={t.id} t={t} stratName={stratName} getCoinName={getCoinName} onShowChart={onShowChart} />
            ))}
          </div>
        </>
      )}

      {view === 'time-based' && (
        <div className="analytics-section">
          <div className="section-title">ZEIT-ANALYSE: {getCoinName(selectedCoin)}</div>

          <div className="time-controls" data-testid="time-controls">
            <select
              className="time-strategy-select"
              value={timeStrategy}
              onChange={(e) => setTimeStrategy(e.target.value)}
              data-testid="time-strategy-select"
            >
              <option value="">Gesamt (alle Strategien)</option>
              {activeStratList.map(s => (
                <option key={s.id} value={s.id}>{s.name || s.id}</option>
              ))}
            </select>
            <div className="time-view-tabs">
              <button className={`time-view-tab ${timeView === 'hours' ? 'active' : ''}`} onClick={() => setTimeView('hours')} data-testid="time-view-hours">Uhrzeiten</button>
              <button className={`time-view-tab ${timeView === 'weekdays' ? 'active' : ''}`} onClick={() => setTimeView('weekdays')} data-testid="time-view-weekdays">Wochentage</button>
              <button className={`time-view-tab ${timeView === 'combo' ? 'active' : ''}`} onClick={() => setTimeView('combo')} data-testid="time-view-combo">Kombi</button>
            </div>
          </div>

          {(() => {
            const rows = timeView === 'hours'
              ? (timeAnalytics?.by_hour || [])
              : timeView === 'weekdays'
                ? (timeAnalytics?.by_weekday || [])
                : (timeAnalytics?.by_combo || []);
            if (!rows.length) {
              return <div className="no-data">Noch keine Zeit-Daten{timeStrategy ? ' für diese Strategie' : ''}. Sobald Signale kommen, siehst du hier die Auswertung.</div>;
            }
            // Sortier-Priorität: (1) Zeilen mit echten Trades nach PnL absteigend
            // (bester PnL zuerst), (2) danach nur-Signal-Zeilen nach WR.
            const sorted = [...rows].sort((a, b) => {
              const aT = (a.trades || 0) > 0 ? 1 : 0;
              const bT = (b.trades || 0) > 0 ? 1 : 0;
              if (aT !== bT) return bT - aT;
              if (aT && bT) {
                if ((b.pnl || 0) !== (a.pnl || 0)) return (b.pnl || 0) - (a.pnl || 0);
              }
              const aDec = a.decided > 0 ? 1 : 0;
              const bDec = b.decided > 0 ? 1 : 0;
              if (aDec !== bDec) return bDec - aDec;
              return (b.win_rate - a.win_rate) || (b.total_signals - a.total_signals);
            });
            const label = (r) => timeView === 'hours'
              ? `${String(r.hour).padStart(2, '0')}:00`
              : timeView === 'weekdays'
                ? r.weekday
                : `${r.weekday} · ${String(r.hour).padStart(2, '0')}:00`;

            // PnL-Gesamt-Summe für die Zusammenfassung
            const totalPnl = sorted.reduce((s, r) => s + (r.pnl || 0), 0);
            const totalTrades = sorted.reduce((s, r) => s + (r.trades || 0), 0);
            const bucketsWithTrades = sorted.filter(r => (r.trades || 0) > 0).length;

            return (
              <div className="time-section">
                <div className="time-subtitle text-long">
                  {timeStrategy ? (activeStratList.find(s => s.id === timeStrategy)?.name || timeStrategy) : 'COIN GESAMT'} · BESTE ZUERST
                </div>
                {totalTrades > 0 && (
                  <div className="time-pnl-summary" data-testid="time-pnl-summary">
                    <span className="text-muted">Ø PnL-Beitrag:</span>
                    <span className={`mono ${totalPnl >= 0 ? 'text-long' : 'text-short'}`} data-testid="time-pnl-total">
                      {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USDT
                    </span>
                    <span className="text-muted">· {totalTrades} Trades in {bucketsWithTrades} Buckets</span>
                  </div>
                )}
                {sorted.map((r, i) => {
                  const hasTrades = (r.trades || 0) > 0;
                  const pnl = r.pnl || 0;
                  return (
                    <div key={i} className={`time-item ${hasTrades ? 'time-item-has-trades' : ''}`} data-testid={`time-row-${i}`}>
                      <div className="time-info"><span className="mono">{label(r)}</span></div>
                      <div className="time-stats">
                        {hasTrades ? (
                          <>
                            <span className={`mono time-pnl-value ${pnl >= 0 ? 'text-long' : 'text-short'}`} data-testid={`time-pnl-${i}`}>
                              {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
                            </span>
                            <span className="text-muted">·</span>
                            <span className="mono time-trades-badge" title={`Ø ${(r.avg_pnl || 0).toFixed(2)} USDT · Best ${(r.best_trade || 0).toFixed(2)} · Worst ${(r.worst_trade || 0).toFixed(2)}`}>
                              {r.trades}T
                            </span>
                            <span className={`mono ${(r.trade_win_rate || 0) >= 50 ? 'text-long' : 'text-short'}`}>
                              {(r.trade_win_rate || 0).toFixed(0)}%
                            </span>
                          </>
                        ) : (
                          <>
                            <span className={`mono ${r.decided > 0 ? (r.win_rate >= 50 ? 'text-long' : 'text-short') : 'text-muted'}`}>
                              {r.decided > 0 ? `${r.win_rate.toFixed(0)}% WR` : '— WR'}
                            </span>
                            <span className="text-muted">·</span>
                            <span className="mono text-long">{r.wins}W</span>
                            <span className="mono text-short">{r.losses}L</span>
                            <span className="text-muted">·</span>
                            <span className="mono">{r.total_signals}x</span>
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })()}
        </div>
      )}

      {showClear && (
        <div className="clear-overlay" onClick={() => !clearing && setShowClear(false)}>
          <div className="clear-modal" onClick={e => e.stopPropagation()} data-testid="clear-analytics-modal">
            <div className="clear-modal-header">
              <Trash size={18} weight="bold" />
              <h4>Analyse-Daten löschen</h4>
            </div>
            <p className="clear-modal-sub">Wähle, was gelöscht werden soll – und für welchen Zeitraum (wie beim Browser-Verlauf).</p>

            <div className="clear-section-label">WAS LÖSCHEN?</div>
            <div className="clear-ranges clear-scopes">
              {clearScopeOptions.map(o => (
                <label
                  key={o.key}
                  className={`clear-range ${clearScope === o.key ? 'active' : ''} ${o.key === 'all' ? 'danger' : ''} ${o.disabled ? 'disabled' : ''}`}
                  title={o.disabled && o.hint ? o.hint : undefined}
                  data-testid={`clear-scope-${o.key}`}
                >
                  <input type="radio" name="clear-scope" value={o.key} checked={clearScope === o.key} disabled={o.disabled} onChange={() => setClearScope(o.key)} />
                  <span>{o.label}{o.disabled && o.hint ? <em className="clear-scope-hint"> · {o.hint}</em> : null}</span>
                </label>
              ))}
            </div>

            <div className="clear-section-label">ZEITRAUM</div>
            <div className="clear-ranges">
              {CLEAR_RANGES.map(r => (
                <label key={r.key} className={`clear-range ${clearRange === r.key ? 'active' : ''} ${r.key === 'all' && clearScope === 'all' ? 'danger' : ''}`} data-testid={`clear-range-${r.key}`}>
                  <input type="radio" name="clear-range" value={r.key} checked={clearRange === r.key} onChange={() => setClearRange(r.key)} />
                  <span>{r.label}</span>
                </label>
              ))}
            </div>

            <div className="clear-summary" data-testid="clear-summary">
              Es wird gelöscht: <b>{rangeLabel}</b> · <b>{scopeLabel}</b>
            </div>
            <div className="clear-summary" data-testid="clear-preview">
              {clearPreview === null
                ? 'Betroffene Einträge werden geprüft …'
                : (
                  <>
                    Betroffen: <b>{clearPreview.signals}</b> Analysen/Signale
                    {' · '}<b>{clearPreview.trades}</b> Trades
                    {clearScope === 'strategy' && (clearPreview.symbols || []).length > 0
                      && <> · über <b>{clearPreview.symbols.length}</b> Coins ({clearPreview.symbols.map(getCoinName).join(', ')})</>}
                  </>
                )}
            </div>
            <div className="clear-warn"><Warning size={14} weight="bold" /> Gelöschte Signale &amp; Statistiken können nicht wiederhergestellt werden. Jede Löschung wird protokolliert (Audit-Log).</div>
            {auditLog.length > 0 && (
              <div className="clear-summary" data-testid="clear-audit-log" style={{ maxHeight: 110, overflowY: 'auto' }}>
                <b>Letzte Löschungen (Audit-Log):</b>
                {auditLog.map((a, i) => (
                  <div key={i} style={{ opacity: 0.85, fontSize: 11, marginTop: 3 }} data-testid={`audit-entry-${i}`}>
                    {String(a.ts || '').slice(0, 16).replace('T', ' ')} · {a.user || 'Admin'}{a.ip ? ` (${a.ip})` : ''} · {a.action}
                    {a.details?.deleted ? ` · ${Object.values(a.details.deleted).reduce((x, y) => x + y, 0)} Einträge` : ''}
                    {a.details?.range ? ` · ${a.details.range}/${a.details.scope}` : ''}
                    {a.details?.strategy_id && !a.details?.range ? ` · ${a.details.strategy_id}` : ''}
                  </div>
                ))}
              </div>
            )}
            <div className="clear-actions">
              <button className="clear-cancel" onClick={() => setShowClear(false)} disabled={clearing} data-testid="clear-cancel-btn">Abbrechen</button>
              <button className="clear-confirm" onClick={runClear} disabled={clearing} data-testid="clear-confirm-btn">
                {clearing ? 'Lösche...' : `Löschen (${scopeLabel})`}
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
};

export default PerformanceAnalytics;
