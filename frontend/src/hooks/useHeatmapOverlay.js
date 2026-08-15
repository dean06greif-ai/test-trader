import { useEffect, useRef, useState } from 'react';

const API_URL = process.env.REACT_APP_BACKEND_URL;

// Heat 0..1 -> Farbe: kühl (blau) über orange bis heiß (rot), Alpha mit Hitze
const heatColor = (h) => {
  const a = (0.10 + 0.20 * h).toFixed(3);
  if (h >= 0.8) return `rgba(255, 51, 102, ${a})`;
  if (h >= 0.55) return `rgba(255, 159, 67, ${a})`;
  return `rgba(77, 156, 255, ${a})`;
};

/**
 * Liquidations-Heatmap als farbige Preiszonen direkt über dem Haupt-Chart.
 *
 * Ressourcenschonend wie das Level-Overlay: läuft nur solange `enabled`,
 * ein REST-Abruf alle `refreshMs`, Zeichnen auf einem pointer-events-freien
 * Canvas (folgt Zoom/Scroll/Autoscale über Redraw-Timer + Range-Subscription).
 */
export default function useHeatmapOverlay(chartRef, seriesRef, canvasRef, symbol, enabled, {
  interval = '15m', bins = 48, minHeat = 0.35, refreshMs = 120000,
} = {}) {
  const dataRef = useRef(null);
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const clearCanvas = () => {
      const c = canvasRef.current;
      if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height);
    };
    if (!enabled) {
      dataRef.current = null;
      clearCanvas();
      setInfo(null);
      setError(null);
      return undefined;
    }
    let cancelled = false;

    const draw = () => {
      const c = canvasRef.current;
      const series = seriesRef.current;
      if (!c || !series || !c.parentElement) return;
      const w = c.parentElement.clientWidth;
      const h = c.parentElement.clientHeight;
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      const ctx = c.getContext('2d');
      ctx.clearRect(0, 0, w, h);
      const d = dataRef.current;
      if (!d || !(d.bins || []).length || !(d.high > d.low)) return;
      const half = (d.high - d.low) / d.bins.length / 2;
      d.bins.forEach(b => {
        if (b.heat < minHeat) return;
        let y1;
        let y2;
        try {
          y1 = series.priceToCoordinate(b.price + half);
          y2 = series.priceToCoordinate(b.price - half);
        } catch (_) { return; }
        if (y1 == null || y2 == null) return;
        const top = Math.min(y1, y2);
        const hh = Math.max(Math.abs(y2 - y1), 1);
        if (top > h || top + hh < 0) return;
        ctx.fillStyle = heatColor(b.heat);
        ctx.fillRect(0, top, w, hh);
      });
    };

    const load = async () => {
      try {
        const res = await fetch(
          `${API_URL}/api/liquidity/heatmap/${symbol}?interval=${interval}&bins=${bins}`);
        const d = await res.json();
        if (cancelled) return;
        if (!res.ok) throw new Error(d.detail || 'Heatmap nicht verfügbar');
        dataRef.current = d;
        setInfo({
          zones: (d.bins || []).filter(b => b.heat >= minHeat).length,
          price: d.price,
          source: d.clusters_source,
        });
        setError(null);
        draw();
      } catch (e) {
        if (!cancelled) {
          setError(e.message);
          dataRef.current = null;
          clearCanvas();
          setInfo(null);
        }
      }
    };

    load();
    const dataTimer = setInterval(load, refreshMs);
    // Redraw-Timer: folgt Zoom/Scroll/Autoscale (Canvas-Fill von ~40 Zonen ist trivial)
    const drawTimer = setInterval(draw, 600);
    let unsub = null;
    try {
      const ts = chartRef.current && chartRef.current.timeScale();
      if (ts) {
        const onRange = () => draw();
        ts.subscribeVisibleLogicalRangeChange(onRange);
        unsub = () => { try { ts.unsubscribeVisibleLogicalRangeChange(onRange); } catch (_) { /* noop */ } };
      }
    } catch (_) { /* Chart evtl. gerade neu aufgebaut */ }

    return () => {
      cancelled = true;
      clearInterval(dataTimer);
      clearInterval(drawTimer);
      if (unsub) unsub();
      clearCanvas();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, symbol, interval, bins, minHeat, refreshMs]);

  return { info, error };
}
