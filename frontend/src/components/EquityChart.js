import React, { useState, useEffect, useMemo, useRef } from 'react';
import { DownloadSimple } from '@phosphor-icons/react';
import {
  ComposedChart, Line, Area, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, ReferenceDot, CartesianGrid,
} from 'recharts';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const COIN_COLORS = ['#FFD60A', '#FF9F0A', '#BF5AF2', '#64D2FF', '#30D158',
  '#FF6482', '#AC8E68', '#5E5CE6', '#FFB340', '#66D4CF'];

// Performance-Cap: nur so viele Punkte rendern, sonst laggt Recharts stark
const MAX_POINTS = 800;

const fmtTime = (iso) => {
  try {
    const d = new Date(iso);
    return `${d.getDate()}.${d.getMonth() + 1}. ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
  } catch { return iso; }
};

/**
 * Equity-Chart.
 * Entweder `jobId` (lädt vom Backtest-Endpoint) ODER `points` (vorbereitete Punkte, z.B. Optimizer).
 * `csvHref` optional für Export-Link.
 * `title` optional für Header.
 */
export default function EquityChart({ jobId, points: pointsProp, csvHref, title }) {
  const [pointsFetched, setPointsFetched] = useState(null);
  const [err, setErr] = useState(null);
  const [showDD, setShowDD] = useState(true);
  const [showLS, setShowLS] = useState(false);
  const [showLiq, setShowLiq] = useState(false); // Liquidations-Marker: Standard AUS (Performance)
  const [selectedCoins, setSelectedCoins] = useState(null); // null = noch nicht initialisiert
  const [stratFilter, setStratFilter] = useState(null); // null = noch nicht initialisiert
  const stratInitRef = useRef(false);
  const coinInitRef = useRef(false);

  const points = pointsProp ?? pointsFetched;

  useEffect(() => {
    if (pointsProp !== undefined) return; // externer Modus: keine Fetches
    if (!jobId) return;
    setPointsFetched(null); setErr(null);
    stratInitRef.current = false;
    coinInitRef.current = false;
    fetch(`${API_URL}/api/backtest/equity/${jobId}`)
      .then(r => { if (!r.ok) throw new Error('no data'); return r.json(); })
      .then(d => setPointsFetched(d.points || []))
      .catch(() => setErr('Keine Equity-Daten für diesen Backtest verfügbar'));
  }, [jobId, pointsProp]);

  // Strategien im Datensatz
  const strategies = useMemo(() => {
    const m = {};
    (points || []).forEach(p => {
      if (p.strategy_id) m[p.strategy_id] = p.strategy_name || p.strategy_id;
    });
    return m;
  }, [points]);

  // Standard-Strategie setzen: erste Strategie wählen sobald Daten da sind
  useEffect(() => {
    if (!points || stratInitRef.current) return;
    const ids = Object.keys(strategies);
    if (ids.length > 1) {
      setStratFilter(ids[0]);
    } else {
      setStratFilter('');
    }
    stratInitRef.current = true;
  }, [points, strategies]);

  // Bei Strategie-Wechsel Top-3-Vorauswahl neu berechnen (sonst zeigt sie alte Coins)
  useEffect(() => {
    coinInitRef.current = false;
  }, [stratFilter]);

  // PnL pro Coin (basierend auf aktuellem Strat-Filter) für Top-3-Vorauswahl
  const coinPnl = useMemo(() => {
    const acc = {};
    (points || []).forEach(p => {
      if (stratFilter && p.strategy_id !== stratFilter) return;
      acc[p.symbol] = (acc[p.symbol] || 0) + (p.pnl || 0);
    });
    return acc;
  }, [points, stratFilter]);

  const allCoins = useMemo(() => Object.keys(coinPnl).sort(), [coinPnl]);

  // Coins Top-3 vorauswählen (sobald Daten & Strategie steht)
  useEffect(() => {
    if (!points || stratFilter === null) return;
    if (coinInitRef.current) return;
    const ranked = Object.entries(coinPnl)
      .sort(([, a], [, b]) => Math.abs(b) - Math.abs(a))
      .slice(0, 3).map(([c]) => c);
    setSelectedCoins(ranked);
    coinInitRef.current = true;
  }, [points, stratFilter, coinPnl]);

  const toggleCoin = (c) =>
    setSelectedCoins(prev => (prev || []).includes(c)
      ? prev.filter(x => x !== c) : [...(prev || []), c]);

  const setAllCoins = (on) => setSelectedCoins(on ? [...allCoins] : []);

  // Datensatz für die Chart – gefiltert nach Strategie, optional downsampled
  const { data, liqs } = useMemo(() => {
    const pts = (points || []).filter(p => !stratFilter || p.strategy_id === stratFilter);
    let eq = 0, peak = 0, lo = 0, sh = 0;
    const coinEq = {};
    (selectedCoins || []).forEach(c => { coinEq[c] = 0; });
    const rows = [];
    const liqPts = [];
    pts.forEach((p, i) => {
      eq += p.pnl; peak = Math.max(peak, eq);
      if (p.side === 'LONG') lo += p.pnl; else sh += p.pnl;
      if (coinEq[p.symbol] !== undefined) coinEq[p.symbol] += p.pnl;
      const row = {
        i, t: p.t, equity: +eq.toFixed(4), peak: +peak.toFixed(4),
        dd: +(eq - peak).toFixed(4),
        long: +lo.toFixed(4), short: +sh.toFixed(4),
        side: p.side, pnl: p.pnl, symbol: p.symbol, liquidated: p.liquidated,
      };
      (selectedCoins || []).forEach(c => { row[`c_${c}`] = +coinEq[c].toFixed(4); });
      rows.push(row);
      if (p.liquidated) liqPts.push(row);
    });

    // Downsampling: bei sehr vielen Trades nur jeden n-ten Punkt rendern.
    // Liquidations-Punkte werden aus dem UN-downgesampelten Datensatz gezogen, damit sie
    // korrekt auf der Zeitachse liegen – wir markieren sie am nächstliegenden Index.
    if (rows.length <= MAX_POINTS) return { data: rows, liqs: liqPts };
    const stride = Math.ceil(rows.length / MAX_POINTS);
    const downs = rows.filter((_, i) => i % stride === 0 || i === rows.length - 1);
    // Liquidationen auf den nächsten downsampled-Index abbilden
    const remap = new Map(downs.map((r, idx) => [r.i, idx]));
    const mappedLiqs = liqPts.map(l => {
      // finde nächstgelegenen downgesampelten Index
      let bestIdx = 0, bestDiff = Infinity;
      for (const [origI, dsIdx] of remap.entries()) {
        const d = Math.abs(origI - l.i);
        if (d < bestDiff) { bestDiff = d; bestIdx = dsIdx; }
      }
      return { ...l, i: bestIdx };
    });
    // Downsampled rows neu indexieren
    const reIdx = downs.map((r, idx) => ({ ...r, i: idx }));
    return { data: reIdx, liqs: mappedLiqs };
  }, [points, stratFilter, selectedCoins]);

  if (err) return <div className="bt-hint" data-testid="equity-chart-empty">{err}</div>;
  if (!points) return <div className="bt-hint">Equity-Kurve lädt...</div>;
  if (!data.length) return <div className="bt-hint" data-testid="equity-chart-empty">Keine geschlossenen Trades – keine Equity-Kurve.</div>;

  const chip = (on, set, label, testid) => (
    <button className={`bt-chip ${on ? 'on' : ''}`} style={{ fontSize: 11, padding: '3px 9px' }}
      onClick={() => set(v => !v)} data-testid={testid}>{label}</button>
  );

  const stratIds = Object.keys(strategies);
  const totalLiq = (points || []).filter(p => (!stratFilter || p.strategy_id === stratFilter) && p.liquidated).length;

  return (
    <div data-testid="equity-chart">
      <div className="bt-section-title" style={{ marginTop: 14 }}>
        {title || 'EQUITY-KURVE (kumulierter PnL pro Trade)'}
        {csvHref !== null && (
          <span className="btc-export">
            <a href={csvHref || `${API_URL}/api/backtest/export/${jobId}?kind=equity`}
              className="btc-export-btn" data-testid="equity-export-csv">
              <DownloadSimple size={13} weight="bold" /> equity.csv
            </a>
          </span>
        )}
      </div>

      <div className="bt-chips" style={{ marginBottom: 6, flexWrap: 'wrap' }}>
        {chip(showDD, setShowDD, 'Drawdown', 'equity-toggle-dd')}
        {chip(showLS, setShowLS, 'Long/Short getrennt', 'equity-toggle-ls')}
        {chip(showLiq, setShowLiq, `Liquidations-Marker${totalLiq ? ` (${totalLiq})` : ''}`, 'equity-toggle-liq')}
        {stratIds.length > 1 && (
          <select value={stratFilter || ''} onChange={e => setStratFilter(e.target.value)}
            data-testid="equity-strategy-filter"
            style={{ background: '#0A0A0A', border: '1px solid #2A2D3A', borderRadius: 8, color: '#fff', fontSize: 11, padding: '3px 8px' }}>
            <option value="">Alle Strategien (kann laggen)</option>
            {stratIds.map(id => <option key={id} value={id}>{strategies[id]}</option>)}
          </select>
        )}
        {data.length >= MAX_POINTS && (
          <span style={{ color: '#8A8FA3', fontSize: 10, alignSelf: 'center' }}
            data-testid="equity-downsampled">
            (Downsampling aktiv · {data.length} sichtbar)
          </span>
        )}
      </div>

      {allCoins.length > 0 && (
        <div className="bt-chips" style={{ marginBottom: 8, flexWrap: 'wrap' }}
          data-testid="equity-coin-chips">
          <span style={{ fontSize: 11, color: '#8A8FA3', alignSelf: 'center', marginRight: 4 }}>
            Coin-Linien:
          </span>
          {allCoins.map((c, idx) => {
            const on = (selectedCoins || []).includes(c);
            const pnl = coinPnl[c] || 0;
            return (
              <button key={c} className={`bt-chip ${on ? 'on' : ''}`}
                style={{
                  fontSize: 11, padding: '3px 9px',
                  borderColor: on ? COIN_COLORS[idx % COIN_COLORS.length] : undefined,
                  color: on ? COIN_COLORS[idx % COIN_COLORS.length] : undefined,
                }}
                onClick={() => toggleCoin(c)}
                data-testid={`equity-coin-${c}`}>
                {c.replace('USDT', '')}
                <span style={{ opacity: 0.55, marginLeft: 4 }}>
                  {pnl >= 0 ? '+' : ''}{pnl.toFixed(1)}
                </span>
              </button>
            );
          })}
          <button className="bt-chip" style={{ fontSize: 11, padding: '3px 9px' }}
            onClick={() => setAllCoins(true)} data-testid="equity-coins-all">alle an</button>
          <button className="bt-chip" style={{ fontSize: 11, padding: '3px 9px' }}
            onClick={() => setAllCoins(false)} data-testid="equity-coins-none">alle aus</button>
        </div>
      )}

      <div style={{ width: '100%', height: 320, background: '#0A0C12', border: '1px solid #1E2230', borderRadius: 10, padding: '10px 4px 0 0' }}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 5, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#1A1E2C" strokeDasharray="3 3" />
            <XAxis dataKey="i" tick={{ fill: '#5C6070', fontSize: 10 }}
              tickFormatter={(i) => data[i] ? fmtTime(data[i].t) : i} minTickGap={60} />
            <YAxis tick={{ fill: '#5C6070', fontSize: 10 }} width={55}
              tickFormatter={(v) => v.toFixed(1)} />
            <Tooltip
              contentStyle={{ background: '#12141C', border: '1px solid #2A2D3A', borderRadius: 8, fontSize: 11 }}
              labelFormatter={(i) => data[i]
                ? `${fmtTime(data[i].t)} · ${data[i].symbol?.replace('USDT', '')} ${data[i].side} · Trade-PnL ${data[i].pnl?.toFixed(3)}${data[i].liquidated ? ' · LIQUIDIERT ⚠' : ''}`
                : ''}
              formatter={(v, name) => [Number(v).toFixed(3), name]} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {showDD && (
              <Area type="monotone" dataKey="dd" name="Drawdown" fill="#FF453A"
                fillOpacity={0.18} stroke="#FF453A" strokeOpacity={0.4} strokeWidth={1}
                isAnimationActive={false} />
            )}
            <Line type="monotone" dataKey="peak" name="Peak" stroke="#3A3F55"
              dot={false} strokeWidth={1} strokeDasharray="4 3" isAnimationActive={false} />
            <Line type="monotone" dataKey="equity" name="Equity" stroke="#00A8FF"
              dot={false} strokeWidth={2} isAnimationActive={false} />
            {showLS && (
              <Line type="monotone" dataKey="long" name="Nur Longs" stroke="#30D158"
                dot={false} strokeWidth={1.5} isAnimationActive={false} />
            )}
            {showLS && (
              <Line type="monotone" dataKey="short" name="Nur Shorts" stroke="#FF6482"
                dot={false} strokeWidth={1.5} isAnimationActive={false} />
            )}
            {(selectedCoins || []).map((c) => {
              const idx = allCoins.indexOf(c);
              return (
                <Line key={c} type="monotone" dataKey={`c_${c}`} name={c.replace('USDT', '')}
                  stroke={COIN_COLORS[idx % COIN_COLORS.length]} dot={false}
                  strokeWidth={1} isAnimationActive={false} />
              );
            })}
            {showLiq && liqs.map((p, idx) => (
              <ReferenceDot key={idx} x={p.i} y={p.equity} r={4}
                fill="#FF453A" stroke="#fff" strokeWidth={1} />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="bt-hint" style={{ marginTop: 6 }}>
        Erkenne: konstantes Wachstum vs. Glückstreffer, wann Drawdowns entstehen, ob Longs oder
        Shorts den Gewinn tragen und welche Coins die Strategie tragen.
        {totalLiq > 0 && !showLiq &&
          ` · ${totalLiq} Liquidation${totalLiq > 1 ? 'en' : ''} vorhanden – „Liquidations-Marker" aktivieren, um sie zu sehen.`}
      </div>
    </div>
  );
}
