import { useEffect, useRef, useState } from 'react';

const API_URL = process.env.REACT_APP_BACKEND_URL;

// Farben je Level-Typ (siehe backend/services/liquidity_levels.py)
const LEVEL_STYLE = {
  poc: { color: '#FFD700', title: 'POC' },
  vah: { color: '#C9A227', title: 'VAH' },
  val: { color: '#C9A227', title: 'VAL' },
  eqh: { color: '#FF6B8A', title: 'Equal Highs' },
  eql: { color: '#4FD1A0', title: 'Equal Lows' },
  swing_high: { color: '#FF8F8F', title: 'Swing-Hoch' },
  swing_low: { color: '#7EE0A8', title: 'Swing-Tief' },
  fvg: { color: '#8AB4FF', title: 'Imbalance' },
  ob_bull: { color: '#3ED598', title: 'Order Block (Bull)' },
  ob_bear: { color: '#FF5E7A', title: 'Order Block (Bear)' },
  hvn: { color: '#B08CFF', title: 'Volumen-Knoten' },
  lvn: { color: '#5A6B80', title: 'Volumen-Vakuum' },
  day_high: { color: '#FF9F43', title: 'Tageshoch' },
  day_low: { color: '#FF9F43', title: 'Tagestief' },
  round: { color: '#7C8CA3', title: 'Runde Marke' },
};

/**
 * Liquiditäts-Level als Preislinien im Haupt-Chart.
 *
 * Bewusst ressourcenschonend:
 *   - läuft NUR solange `enabled` true ist (Standard: aus, nicht persistiert),
 *   - ein einzelner REST-Abruf (kein WebSocket, keine Live-Streams),
 *   - Refresh nur alle `refreshMs` (Default 90 s), Backend cached Kerzen 30 s,
 *   - alle Linien werden beim Ausschalten/Symbolwechsel sauber entfernt.
 */
export default function useLiquidityOverlay(seriesRef, symbol, enabled, {
  interval = '15m', maxLevels = 8, refreshMs = 90000,
} = {}) {
  const linesRef = useRef([]);
  const [levels, setLevels] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    const clear = () => {
      const series = seriesRef.current;
      linesRef.current.forEach(l => {
        try { series && series.removePriceLine(l); } catch (_) { /* noop */ }
      });
      linesRef.current = [];
    };

    if (!enabled) { clear(); setLevels([]); setError(null); return undefined; }

    let cancelled = false;
    const draw = (items) => {
      clear();
      const series = seriesRef.current;
      if (!series) return;
      items.forEach(l => {
        const style = LEVEL_STYLE[l.type] || { color: '#8ea3bd', title: l.type };
        try {
          linesRef.current.push(series.createPriceLine({
            price: l.price,
            color: style.color,
            lineWidth: l.strength >= 80 ? 2 : 1,
            lineStyle: l.untested ? 0 : 2,           // 0 = solid, 2 = dashed
            axisLabelVisible: true,
            title: `${style.title} ${l.strength}`,
          }));
        } catch (_) { /* Chart wurde evtl. gerade neu aufgebaut */ }
      });
    };

    const load = async () => {
      try {
        const res = await fetch(
          `${API_URL}/api/liquidity/levels/${symbol}?interval=${interval}`);
        const data = await res.json();
        if (cancelled) return;
        if (!res.ok) throw new Error(data.detail || 'Liquiditäts-Level nicht verfügbar');
        const items = (data.levels || []).slice(0, maxLevels);
        setLevels(items);
        setError(null);
        draw(items);
      } catch (e) {
        if (!cancelled) { setError(e.message); setLevels([]); }
      }
    };

    load();
    const timer = setInterval(load, refreshMs);
    return () => { cancelled = true; clearInterval(timer); clear(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, symbol, interval, maxLevels, refreshMs]);

  return { levels, error };
}
