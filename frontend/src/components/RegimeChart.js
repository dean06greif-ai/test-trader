import React, { useMemo, useState } from 'react';
import {
  ComposedChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceArea, ReferenceLine,
} from 'recharts';

import { regimeColor, regimeOpacity, REGIME_FALLBACK_COLORS } from '../lib/regimeColors';
import { fmtDateTime } from '../lib/time';

export const REGIME_COLORS = REGIME_FALLBACK_COLORS;

const fmtDate = (ts) => {
  const d = new Date(ts);
  return `${d.getDate()}.${d.getMonth() + 1}.${String(d.getFullYear()).slice(2)}`;
};

// Farben der einblendbaren EMA-Trendlinien (Reihenfolge = aufsteigende Periode)
const EMA_PALETTE = ['#64D2FF', '#BF5AF2', '#FF9F0A', '#FF6482', '#66D4CF', '#A2845E'];

/**
 * Kursverlauf mit farbig hinterlegten Regime-Abschnitten.
 * prices: [[ts, close], ...] · segments: [{regime, from_ts, to_ts}]
 * regimes: [{id,label}] · trainEndTs: Trennlinie Training/Holdout
 * liveSegments: kausale Live-Erkennung (ohne Lookahead). Gibt es einen
 *   Holdout, wird er als LIVE-PREDICTION gezeichnet: vor der Trennlinie die
 *   final korrigierten Phasen (mit Lookahead), danach die Live-Sicht in
 *   LEUCHTENDEREN Farben – die Vergangenheit wird dabei nie umgeschrieben,
 *   Fehlgriffe + Erkennungs-Lag bleiben sichtbar.
 * liveBand: optionales Band oben (Live-Sicht über den GESAMTEN Zeitraum)
 * emas: {"9": [[ts,val],...], ...} einblendbare EMA-Linien (Standard: aus)
 */

// Anzeige-Segmente begrenzen, OHNE das Bild zu verfälschen: pro Zeit-"Pixel"
// (Bucket) gewinnt das Regime mit dem größten Zeitanteil (Dominanz-Bucketing).
// Vorher wurden schmale Abschnitte einfach in den VORGÄNGER gemerged – bei
// langen Zeiträumen (z.B. 2000 Tage) fraß ein Segment dutzende Nachfolger
// und halbe Charts wurden fälschlich einfarbig rot/grün.
const mergeForDisplay = (segments, minWidth) => {
  if (!segments || segments.length === 0) return [];
  // Wenige Segmente: exakt zeichnen, nur benachbarte gleiche verschmelzen.
  if (!minWidth || segments.length <= 320) {
    const out = [];
    for (const s of segments) {
      const last = out[out.length - 1];
      if (last && s.regime === last.regime) {
        last.to_ts = Math.max(last.to_ts, s.to_ts);
      } else {
        out.push({ ...s });
      }
    }
    return out;
  }
  const t0 = segments[0].from_ts;
  const t1 = segments[segments.length - 1].to_ts;
  const nb = Math.max(1, Math.min(2000, Math.ceil((t1 - t0) / minWidth)));
  const bw = (t1 - t0) / nb;
  const acc = Array.from({ length: nb }, () => ({}));
  for (const s of segments) {
    let b0 = Math.floor((s.from_ts - t0) / bw);
    let b1 = Math.floor((s.to_ts - t0) / bw);
    b0 = Math.max(0, Math.min(nb - 1, b0));
    b1 = Math.max(0, Math.min(nb - 1, b1));
    for (let b = b0; b <= b1; b++) {
      const bs = t0 + b * bw;
      const ov = Math.min(s.to_ts, bs + bw) - Math.max(s.from_ts, bs);
      if (ov > 0) acc[b][s.regime] = (acc[b][s.regime] || 0) + ov;
    }
  }
  const out = [];
  for (let b = 0; b < nb; b++) {
    const entries = Object.entries(acc[b]);
    if (!entries.length) continue;
    entries.sort((x, y) => y[1] - x[1]);
    const reg = Number(entries[0][0]);
    const bs = t0 + b * bw;
    const be = Math.min(bs + bw, t1);
    const last = out[out.length - 1];
    if (last && last.regime === reg && bs - last.to_ts < bw / 2) {
      last.to_ts = be;
    } else {
      out.push({ regime: reg, from_ts: bs, to_ts: be });
    }
  }
  return out;
};

// Segmente auf ein Zeitfenster zuschneiden (für den Holdout-Split)
const clipSegments = (segments, fromTs, toTs) => {
  const out = [];
  for (const s of segments || []) {
    const a = fromTs != null ? Math.max(s.from_ts, fromTs) : s.from_ts;
    const b = toTs != null ? Math.min(s.to_ts, toTs) : s.to_ts;
    if (b > a) out.push({ ...s, from_ts: a, to_ts: b });
  }
  return out;
};

function RegimeChart({ title, prices, segments, idealSegments, liveSegments,
  regimes, model, trainEndTs, emas, liveBand, height = 190 }) {
  const [hidden, setHidden] = useState({});
  const [emaOn, setEmaOn] = useState({});
  const emaKeys = useMemo(
    () => Object.keys(emas || {}).sort((a, b) => parseFloat(a) - parseFloat(b)),
    [emas]);
  const data = useMemo(() => {
    const rows = (prices || []).map(p => ({ t: p[0], c: p[1] }));
    for (const k of emaKeys) {
      const map = new Map((emas[k] || []).map(p => [p[0], p[1]]));
      for (const r of rows) {
        const v = map.get(r.t);
        if (v !== undefined) r['e' + k] = v;
      }
    }
    return rows;
  }, [prices, emas, emaKeys]);
  const span = data.length ? data[data.length - 1].t - data[0].t : 0;
  const minW = span / 300;
  // Holdout-Split: davor die berechnete (final korrigierte) Sicht, danach die
  // kausale Live-Prediction – leuchtender, damit man sofort sieht: ab hier
  // ohne Lookahead.
  const splitLive = Boolean(trainEndTs && (liveSegments || []).length);
  const dispSegments = useMemo(
    () => mergeForDisplay(splitLive ? clipSegments(segments, null, trainEndTs) : segments, minW),
    [segments, splitLive, trainEndTs, minW]);
  const dispPred = useMemo(
    () => (splitLive ? mergeForDisplay(clipSegments(liveSegments, trainEndTs, null), minW) : []),
    [liveSegments, splitLive, trainEndTs, minW]);
  const dispBand = useMemo(
    () => (liveBand ? mergeForDisplay(liveSegments, minW) : []),
    [liveSegments, liveBand, minW]);
  const dispIdeal = useMemo(() => mergeForDisplay(idealSegments, minW), [idealSegments, minW]);
  if (!data.length) return null;
  const [min, max] = data.reduce((a, p) => [Math.min(a[0], p.c), Math.max(a[1], p.c)],
    [Infinity, -Infinity]);
  const pad = (max - min) * 0.04;
  const band = (max - min + 2 * pad) * 0.07;
  const labelOf = (rid) => (regimes || []).find(r => r.id === rid)?.label || `Regime ${rid + 1}`;
  const predOpacity = (rid) => Math.min(regimeOpacity(rid, regimes, model) * 2.4, 0.62);
  const segAt = (ts) => {
    if (splitLive && ts > trainEndTs) {
      return (liveSegments || []).find(s => ts >= s.from_ts && ts <= s.to_ts);
    }
    return (segments || []).find(s => ts >= s.from_ts && ts <= s.to_ts);
  };

  return (
    <div className="rl-chart" data-testid={`regime-chart-${title || 'chart'}`}>
      {title && <div className="rl-chart-title">{title}</div>}
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          {dispSegments.filter(s => !hidden[s.regime]).map((s, i) => (
            <ReferenceArea key={i} x1={s.from_ts} x2={s.to_ts}
              y1={min - pad} y2={max + pad}
              fill={regimeColor(s.regime, regimes, model)}
              fillOpacity={regimeOpacity(s.regime, regimes, model)} strokeOpacity={0} />
          ))}
          {dispPred.filter(s => !hidden[s.regime]).map((s, i) => (
            <ReferenceArea key={`pred-${i}`} x1={s.from_ts} x2={s.to_ts}
              y1={min - pad} y2={max + pad}
              fill={regimeColor(s.regime, regimes, model)}
              fillOpacity={predOpacity(s.regime)} strokeOpacity={0} />
          ))}
          {dispBand.map((s, i) => (
            <ReferenceArea key={`live-${i}`} x1={s.from_ts} x2={s.to_ts}
              y1={max + pad - band} y2={max + pad}
              fill={regimeColor(s.regime, regimes, model)}
              fillOpacity={0.85} strokeOpacity={0} />
          ))}
          {dispIdeal.map((s, i) => (
            <ReferenceArea key={`ideal-${i}`} x1={s.from_ts} x2={s.to_ts}
              y1={min - pad} y2={min - pad + band}
              fill={regimeColor(s.regime, regimes, model)}
              fillOpacity={0.8} strokeOpacity={0} />
          ))}
          {trainEndTs && (
            <ReferenceLine x={trainEndTs} stroke="#ffa502" strokeDasharray="4 4"
              label={{
                value: splitLive ? 'Live-Prediction →' : 'Holdout →',
                fill: '#ffa502', fontSize: 10, position: 'insideTopRight',
              }} />
          )}
          <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']}
            tickFormatter={fmtDate} tick={{ fontSize: 10, fill: '#8b90a0' }}
            stroke="#262a38" />
          <YAxis domain={[min - pad, max + pad]} tick={{ fontSize: 10, fill: '#8b90a0' }}
            stroke="#262a38" width={62}
            tickFormatter={(v) => (v >= 1000 ? v.toFixed(0) : v.toPrecision(4))} />
          <Tooltip
            contentStyle={{ background: '#12141d', border: '1px solid #262a38', fontSize: 11 }}
            labelFormatter={(ts) => {
              const seg = segAt(ts);
              const pred = splitLive && ts > trainEndTs;
              return `${fmtDateTime(ts)}${seg ? ` · ${labelOf(seg.regime)}` : ''}${pred ? ' · LIVE (ohne Lookahead)' : ''}`;
            }}
            formatter={(v, name) => [Number(v).toPrecision(6),
              name === 'c' ? 'Kurs' : `EMA ${String(name).slice(1)}`]} />
          {emaKeys.filter(k => emaOn[k]).map((k) => (
            <Line key={`ema-${k}`} dataKey={`e${k}`} dot={false}
              stroke={EMA_PALETTE[emaKeys.indexOf(k) % EMA_PALETTE.length]}
              strokeWidth={1.2} strokeDasharray="0" isAnimationActive={false}
              connectNulls />
          ))}
          <Line dataKey="c" dot={false} stroke="#c9cddb" strokeWidth={1.4} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      <div className="rl-legend">
        {(regimes || []).map(r => (
          <button key={r.id}
            className={`rl-legend-item ${hidden[r.id] ? 'off' : ''}`}
            onClick={() => setHidden(h => ({ ...h, [r.id]: !h[r.id] }))}
            data-testid={`regime-legend-${r.id}`}
            title="Klicken zum Ein-/Ausblenden der Markierung">
            <span className="rl-dot" style={{ background: regimeColor(r.id, regimes, model) }} />
            #{r.id + 1} {r.label}
          </button>
        ))}
        {emaKeys.map((k) => (
          <button key={`ema-${k}`}
            className={`rl-legend-item ${emaOn[k] ? '' : 'off'}`}
            onClick={() => setEmaOn(o => ({ ...o, [k]: !o[k] }))}
            data-testid={`regime-ema-toggle-${k}`}
            title={`EMA ${k} Tage ein-/ausblenden – z.B. um Kreuzungen mit Regime-Wechseln zu vergleichen`}>
            <span className="rl-dot"
              style={{ background: EMA_PALETTE[emaKeys.indexOf(k) % EMA_PALETTE.length] }} />
            EMA {k}
          </button>
        ))}
        {splitLive && (
          <span className="opt-small" style={{ alignSelf: 'center', opacity: 0.8 }}
            title="Rechts der orangen Linie zeigt der Hintergrund die kausale Live-Erkennung (Kerze für Kerze, ohne Zukunftswissen) in leuchtenderen Farben. Die Vergangenheit wird dabei nie umgeschrieben – Fehlgriffe und Erkennungs-Verzögerung bleiben sichtbar.">
            leuchtend = Live-Prediction (ohne Lookahead)
          </span>
        )}
      </div>
    </div>
  );
}

export default React.memo(RegimeChart);
