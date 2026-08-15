import React, { useCallback, useEffect, useState } from 'react';
import { Drop, X, ArrowsClockwise } from '@phosphor-icons/react';
import SafeOverlay from './SafeOverlay';
import { fmtTimeSec } from '../lib/time';
import './LiquidityPanel.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const INTERVALS = ['5m', '15m', '1h', '4h'];
const TYPE_LABEL = {
  swing_high: 'Swing-Hoch', swing_low: 'Swing-Tief', eqh: 'Equal Highs',
  eql: 'Equal Lows', fvg: 'Imbalance (FVG)', poc: 'POC', vah: 'VAH', val: 'VAL',
  ob_bull: 'Order Block (Bull)', ob_bear: 'Order Block (Bear)',
  hvn: 'Volumen-Knoten', lvn: 'Volumen-Vakuum', round: 'Runde Marke',
  day_high: 'Tageshoch', day_low: 'Tagestief',
};

const heatColor = (heat) => {
  // Klassische Heatmap-Bande: kühl (kaum Liquidität) -> heiß (dichte Cluster).
  // Wurzel-Skala macht schwache Zonen sichtbar, ohne Spitzen zu verwischen.
  const h = Math.sqrt(Math.max(0, Math.min(1, heat)));
  const hue = 212 - 212 * h;          // 212 = blau, 0 = rot
  return `hsl(${hue}, ${40 + 55 * h}%, ${16 + 34 * h}%)`;
};

const fmtUsd = (v) => (v >= 1e9 ? `${(v / 1e9).toFixed(2)} Mrd`
  : v >= 1e6 ? `${(v / 1e6).toFixed(1)} Mio` : (v || 0).toLocaleString('de-DE'));

/**
 * Liquidations-Heatmap (Eigenbau, keyless) + eigene „Liquidity Levels"
 * (X-Ray-Pro-Äquivalent). Daten: /api/liquidity/heatmap + /api/liquidity/levels.
 */
const LiquidityPanel = ({ symbol = 'BTCUSDT', onClose }) => {
  const [sym, setSym] = useState(symbol);
  const [interval, setIntervalTf] = useState('15m');
  const [heat, setHeat] = useState(null);
  const [levels, setLevels] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [updated, setUpdated] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const [h, l] = await Promise.all([
        fetch(`${API_URL}/api/liquidity/heatmap/${sym}?interval=${interval}&bins=32`).then(r => r.json()),
        fetch(`${API_URL}/api/liquidity/levels/${sym}?interval=${interval}`).then(r => r.json()),
      ]);
      if (h.detail || l.detail) throw new Error(h.detail || l.detail);
      setHeat(h); setLevels(l); setUpdated(new Date().toISOString());
    } catch (e) {
      setErr(e.message || 'Laden fehlgeschlagen');
    } finally { setLoading(false); }
  }, [sym, interval]);

  useEffect(() => { load(); }, [load]);

  const bins = [...(heat?.bins || [])].reverse();
  const price = heat?.price || levels?.price;
  const rl = heat?.recent_liquidations_5m || {};

  return (
    <SafeOverlay className="liq-overlay" onClose={onClose} testId="liquidity-overlay" closeOnOutside={false}>
      <div className="liq-modal" data-testid="liquidity-panel">
        <div className="liq-head">
          <Drop size={18} weight="fill" style={{ color: '#4d9cff' }} />
          <h3>LIQUIDITÄT · HEATMAP &amp; LEVELS</h3>
          <button className="liq-close" onClick={onClose} data-testid="liquidity-close-btn"><X size={15} weight="bold" /></button>
        </div>
        <p className="liq-sub">
          Liquidations-Cluster, Orderbook-Wände und Liquiditäts-Level – Eigenbau aus
          freien Börsendaten (Binance/OKX/Bybit) und Kerzen. Keine Fremd-Keys nötig.
          Der KI Trader nutzt genau diese Daten in seinen Entscheidungen.
        </p>

        <details className="liq-help" data-testid="liquidity-help">
          <summary>❓ Was bedeutet hier was? (Erklärung aller Werte &amp; Einstellungen)</summary>
          <div className="liq-help-body">
            <p><b>Einstellungen oben:</b></p>
            <ul>
              <li><b>Asset-Auswahl</b> – für welchen Coin die Liquiditätsdaten berechnet werden.</li>
              <li><b>Intervall (5m–1d)</b> – Kerzengröße der Analyse: kleine Intervalle zeigen kurzfristige Cluster (Scalping), große Intervalle die relevanten Swing-Zonen.</li>
              <li><b>Aktualisieren</b> – lädt Heatmap, Levels und Orderbook neu (kein Auto-Refresh, um die Börsen-APIs zu schonen).</li>
            </ul>
            <p><b>Kopfzeile:</b></p>
            <ul>
              <li><b>Preis</b> – aktueller Marktpreis (Basis aller Distanz-Angaben).</li>
              <li><b>Open Interest</b> – Gesamtwert aller offenen Futures-Positionen (von Binance). Steigendes OI + steigender Preis = neue Longs; steigendes OI + fallender Preis = neue Shorts.</li>
              <li><b>Liquidationen 5min</b> – tatsächlich liquidierte Long-/Short-Positionen der letzten 5 Minuten (Live-Feed der Börsen).</li>
              <li><b>Kaskade aktiv</b> – Warnung: ungewöhnlich viele Liquidationen in kurzer Zeit, Preis kann in eine Richtung „durchrutschen“.</li>
            </ul>
            <p><b>Liquidations-Heatmap:</b></p>
            <ul>
              <li>Jede Zeile ist eine Preiszone. Der <b>Heat-Wert (0–100)</b> zeigt, wie viel Liquidations-Liquidität dort liegt. <b>Bevorzugte Basis sind ECHTE gemessene Liquidationen</b> (Force-Orders der Börsen der letzten 4 Stunden, zu Preiszonen verdichtet). Liegen zu wenige gemessene Daten vor, greift eine Schätzung aus typischen Hebel-Stufen (10x/25x/50x/100x), gewichtet mit Volumen und Open Interest.</li>
              <li><b>Farbskala:</b> kühl (blau) = wenig Liquidität, heiß (gelb/rot) = dichte Cluster. Preis wird von heißen Zonen oft „magnetisch“ angezogen.</li>
              <li><b>Tags</b> markieren, welche Hebel-Stufe dort liquidiert würde (z. B. „50x Longs“).</li>
            </ul>
            <p><b>Liquidity Levels:</b></p>
            <ul>
              <li><b>Equal Highs/Lows</b> – mehrfach getestete gleiche Hochs/Tiefs: darüber/darunter liegen Stop-Losses (Liquiditätspools).</li>
              <li><b>Swing High/Low</b> – markante Wendepunkte; <b>„unberührt“</b> = seitdem nicht wieder angelaufen (bevorzugtes Ziel).</li>
              <li><b>Order Block (Bull/Bear)</b> – Smart-Money-Concept: die letzte Gegen-Kerze vor einem impulsiven Move. Unberührte Order Blocks sind institutionelle Einstiegs-Zonen für den Retest; komplett durchhandelte Blöcke werden automatisch entfernt. Der KI Trader sieht diese Zonen in seinem Liquiditäts-Kontext.</li>
              <li><b>↑/↓ %</b> – Distanz zum aktuellen Preis; <b>Strength</b> – wie oft/deutlich das Level bestätigt wurde.</li>
              <li><b>POC / VAH / VAL</b> – Volume Profile: Preis mit dem meisten gehandelten Volumen (POC) sowie Ober-/Unterkante der 70%-Value-Area. Wirken als Magnet bzw. Unterstützung/Widerstand.</li>
            </ul>
            <p><b>Orderbook-Wände:</b> echte, aktuell im Orderbuch liegende große Kauf-(Bid) bzw. Verkaufs-(Ask)-Blöcke mit USD-Größe – können als kurzfristige Barriere wirken, aber auch jederzeit gezogen werden (Spoofing möglich).</p>
            <p className="liq-help-note">⚠ <b>Vertrauenswürdigkeit:</b> Preis, Open Interest, 5-Minuten-Liquidationen, Orderbook-Wände und Volume Profile sind <b>echte Live-Börsendaten</b> (Binance/OKX/Bybit). Die <b>Heatmap</b> basiert – sobald genug Daten gesammelt sind – auf <b>echten gemessenen Liquidationen</b> (Force-Orders der letzten 4h). Nur wenn dafür zu wenige Daten vorliegen, rekonstruiert sie die wahrscheinlichen Zonen aus Hebel-Mathematik + OI + Volumen (dann als „Schätzung“ gekennzeichnet). Der KI Trader bekommt die reine Formel-Schätzung NICHT mehr als Trade-Begründung.</p>
          </div>
        </details>

        <div className="liq-controls">
          <select value={sym} onChange={e => setSym(e.target.value)} data-testid="liquidity-symbol-select">
            {['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT',
              'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'POLUSDT'].map(s => (
              <option key={s} value={s}>{s.replace('USDT', '')}</option>
            ))}
          </select>
          <select value={interval} onChange={e => setIntervalTf(e.target.value)} data-testid="liquidity-interval-select">
            {INTERVALS.map(i => <option key={i} value={i}>{i}</option>)}
          </select>
          <button onClick={load} disabled={loading} data-testid="liquidity-reload-btn">
            <ArrowsClockwise size={12} weight="bold" /> {loading ? 'Lädt…' : 'Aktualisieren'}
          </button>
          {updated && <span className="liq-pill">Stand <b>{fmtTimeSec(updated)}</b></span>}
        </div>

        {err && <div className="liq-err" data-testid="liquidity-error">{err}</div>}

        <div className="liq-stats" data-testid="liquidity-stats">
          <span className="liq-pill">Preis <b>{price ?? '–'}</b></span>
          <span className="liq-pill">Open Interest <b>{heat?.oi_usd ? `${fmtUsd(heat.oi_usd)} USD` : '–'}</b> ({heat?.oi_trend || '–'}{(heat?.oi_venues || []).length ? ` · Quellen: ${heat.oi_venues.join('+')}` : ''})</span>
          <span className="liq-pill">Liquidationen 5min: Longs <b>{fmtUsd(rl.long_usd || 0)}</b> · Shorts <b>{fmtUsd(rl.short_usd || 0)}</b></span>
          {rl.cascade && <span className="liq-pill warn">Kaskade aktiv</span>}
        </div>

        <div className="liq-grid">
          <div className="liq-card">
            <div className="liq-card-title">LIQUIDATIONS-HEATMAP {heat?.clusters_source === 'measured' ? '(echte Force-Orders, 4h)' : '(Schätzung)'}</div>
            {bins.length === 0 && <div className="liq-empty">Keine Daten.</div>}
            {bins.map((b, i) => {
              const isPrice = price && Math.abs(b.price - price) <= (heat.high - heat.low) / bins.length / 2;
              return (
                <div key={i} className={`liq-row ${isPrice ? 'is-price' : ''}`} data-testid={`liquidity-heat-row-${i}`}>
                  <span className="liq-price">{b.price}</span>
                  <span className="liq-bar-wrap">
                    <span className="liq-bar" style={{ width: '100%', background: heatColor(b.heat) }} />
                    <span className="liq-heat-val">{Math.round(b.heat * 100)}</span>
                  </span>
                  <span className="liq-tags" title={(b.tags || []).join(' · ')}>{(b.tags || []).join(' · ')}</span>
                </div>
              );
            })}
          </div>

          <div className="liq-card">
            <div className="liq-card-title">LIQUIDITY LEVELS (X-Ray-Äquivalent)</div>
            <div className="liq-levels">
              {(levels?.levels || []).length === 0 && <div className="liq-empty">Keine Level gefunden.</div>}
              {(levels?.levels || []).map((l, i) => (
                <div key={i} className={`liq-level ${l.side}`} data-testid={`liquidity-level-${i}`}>
                  <span className="liq-price">{l.price}</span>
                  <span className="liq-type">{TYPE_LABEL[l.type] || l.type}{l.untested ? ' · unberührt' : ''}</span>
                  <span className="liq-type">{l.side === 'above' ? '↑' : '↓'} {l.dist_pct}%</span>
                  <span className="liq-strength">{l.strength}</span>
                </div>
              ))}
            </div>
            {levels?.volume_profile?.poc && (
              <div className="liq-stats" style={{ marginTop: 12 }}>
                <span className="liq-pill">POC <b>{levels.volume_profile.poc}</b></span>
                <span className="liq-pill">VAH <b>{levels.volume_profile.vah}</b></span>
                <span className="liq-pill">VAL <b>{levels.volume_profile.val}</b></span>
              </div>
            )}
            <div className="liq-card-title" style={{ marginTop: 14 }}>ORDERBOOK-WÄNDE</div>
            <div className="liq-levels">
              {[...(heat?.orderbook_walls?.asks || []).map(w => ({ ...w, side: 'above' })),
                ...(heat?.orderbook_walls?.bids || []).map(w => ({ ...w, side: 'below' }))]
                .map((w, i) => (
                  <div key={i} className={`liq-level ${w.side}`} data-testid={`liquidity-wall-${i}`}>
                    <span className="liq-price">{w.price}</span>
                    <span className="liq-type">{w.side === 'above' ? 'Ask-Wand' : 'Bid-Wand'}</span>
                    <span className="liq-type">{w.dist_pct}%</span>
                    <span className="liq-strength">{fmtUsd(w.usd)}</span>
                  </div>
                ))}
              {!(heat?.orderbook_walls?.asks || []).length && !(heat?.orderbook_walls?.bids || []).length
                && <div className="liq-empty">Keine Wände erkannt.</div>}
            </div>
          </div>
        </div>
      </div>
    </SafeOverlay>
  );
};

export default LiquidityPanel;
