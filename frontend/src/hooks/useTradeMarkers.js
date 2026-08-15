import { useCallback, useEffect, useRef, useState } from 'react';
import { createSeriesMarkers } from 'lightweight-charts';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmt = (v) => (v == null ? '–' : Number(v).toLocaleString('de-DE', { maximumFractionDigits: 6 }));

/**
 * Trade-Overlay im Haupt-Chart:
 *  - offene Trades: Entry/SL/TP als Preislinien + Entry-Pfeil (verschwinden beim Schließen)
 *  - geschlossene Trades (optional per Toggle): Entry-Pfeil + Exit-Punkt (grün/rot nach PnL)
 *  - `tradeMapRef`: Bar-Zeit -> Trade-Infos für den Hover-Tooltip (zeigt Strategie)
 */
export default function useTradeMarkers(seriesRef, symbol, showClosed, barSec, barsLoaded) {
  const linesRef = useRef([]);
  const detailLinesRef = useRef([]);
  const openRef = useRef([]);
  const markersRef = useRef(null);
  const tradeMapRef = useRef({});
  const [counts, setCounts] = useState({ open: 0, closed: 0 });
  // Offene Trades des Coins für die Badge-Leiste im Chart (MainChart)
  const [openTrades, setOpenTrades] = useState([]);
  // Manueller Refresh (z.B. nach Trade-Eröffnung/-Schließung aus dem Chart)
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = useCallback(() => setReloadKey(k => k + 1), []);

  // Angepinnter Trade (Klick auf Badge/Entry-Pfeil): Entry + aktuelle SL/TP1/TP
  // fest im Chart (nur Preislinien – RAM-schonend, keine zusätzlichen Serien)
  const pinnedIdRef = useRef(null);
  const pinnedLinesRef = useRef([]);
  const [pinnedId, setPinnedId] = useState(null);

  const renderPinned = useCallback(() => {
    const series = seriesRef.current;
    pinnedLinesRef.current.forEach(l => {
      try { series && series.removePriceLine(l); } catch (_) { /* noop */ }
    });
    pinnedLinesRef.current = [];
    const id = pinnedIdRef.current;
    if (!id || !series) return;
    const hit = openRef.current.find(o => o.trade.id === id);
    if (!hit) { pinnedIdRef.current = null; setPinnedId(null); return; }
    const t = hit.trade;
    const add = (price, color, style, width, title) => {
      if (!price) return;
      try {
        pinnedLinesRef.current.push(series.createPriceLine({
          price, color, lineWidth: width, lineStyle: style,
          axisLabelVisible: true, title,
        }));
      } catch (_) { /* noop */ }
    };
    add(t.entry, t.side === 'LONG' ? '#00FF66' : '#FF3366', 0, 2,
        `⦿ ENTRY ${t.label || t.side}`);
    add(t.sl, '#FF3366', 2, 1, 'SL');
    if (!t.tp1_hit) add(t.tp1, '#00C77F', 2, 1, 'TP1');
    add(t.tpf, '#00FF66', 2, 1, 'TP');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const togglePin = useCallback((tradeId) => {
    pinnedIdRef.current = pinnedIdRef.current === tradeId ? null : tradeId;
    setPinnedId(pinnedIdRef.current);
    renderPinned();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pinAtTime = useCallback((time) => {
    const hit = openRef.current.find(o => o.time === time);
    if (hit) togglePin(hit.trade.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // SL/TP der Position nur beim Hover über den Entry-Punkt einblenden
  const hoverDetail = useCallback((time) => {
    const series = seriesRef.current;
    detailLinesRef.current.forEach(l => {
      try { series && series.removePriceLine(l); } catch (_) { /* noop */ }
    });
    detailLinesRef.current = [];
    if (time == null || !series) return;
    openRef.current
      .filter(o => o.time === time && o.trade.id !== pinnedIdRef.current)
      .forEach(({ trade: t }) => {
      const add = (price, color, style, title) => {
        if (!price) return;
        try {
          detailLinesRef.current.push(series.createPriceLine({
            price, color, lineWidth: 1, lineStyle: style,
            axisLabelVisible: true, title,
          }));
        } catch (_) { /* noop */ }
      };
      add(t.sl, '#FF3366', 2, 'SL');
      if (t.qty_remaining !== 0 && !t.tp1_hit) add(t.tp1, '#00C77F', 2, 'TP1');
      add(t.tpf, '#00FF66', 2, 'TP');
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;

    const clearLines = () => {
      const series = seriesRef.current;
      linesRef.current.forEach(l => {
        try { series && series.removePriceLine(l); } catch (_) { /* noop */ }
      });
      linesRef.current = [];
    };
    const clearMarkers = () => {
      try { markersRef.current && markersRef.current.setMarkers([]); } catch (_) { /* noop */ }
    };

    const toBar = (iso) => {
      const t = Math.floor(new Date(iso).getTime() / 1000);
      return Math.floor(t / barSec) * barSec;
    };

    const apply = (trades) => {
      const series = seriesRef.current;
      if (!series) return;
      clearLines();
      const mine = trades.filter(t => t.symbol === symbol);
      const open = mine.filter(t => t.status === 'open');
      const closed = mine.filter(t => t.status === 'closed');

      // Nummerierung pro Richtung ("Long 1", "Short 2", ...) in zeitlicher
      // Reihenfolge – macht Entry/Exit im Chart eindeutig zuordenbar.
      const numbered = {};
      const counters = { LONG: 0, SHORT: 0 };
      [...mine].sort((a, b) => new Date(a.opened_at) - new Date(b.opened_at)).forEach(t => {
        counters[t.side] = (counters[t.side] || 0) + 1;
        numbered[t.id] = `${t.side === 'LONG' ? 'Long' : 'Short'} ${counters[t.side]}`;
      });

      // sichtbare Zeitspanne der geladenen Kerzen (Marker außerhalb weglassen)
      let first = 0;
      let last = Infinity;
      try {
        const data = series.data();
        if (data.length) { first = data[0].time; last = data[data.length - 1].time + barSec; }
      } catch (_) { /* noop */ }

      // Keine permanenten Entry-Preislinien mehr am rechten Rand – Trades sind
      // oben links als Badges sichtbar, Details per Klick (Pin) bzw. Hover.
      openRef.current = open.map(t => ({ time: toBar(t.opened_at),
                                         trade: { ...t, label: numbered[t.id] } }));
      // angepinnte Linien mit den frischen Werten (aktueller SL/TP) neu zeichnen
      renderPinned();
      setOpenTrades(open.map(t => ({ ...t, barTime: toBar(t.opened_at),
                                     label: numbered[t.id] || t.side })));

      const markers = [];
      const map = {};
      const remember = (time, info) => { (map[time] = map[time] || []).push(info); };

      open.forEach(t => {
        const time = toBar(t.opened_at);
        if (time < first || time > last) return;
        const name = numbered[t.id] || t.side;
        markers.push({
          time,
          position: t.side === 'LONG' ? 'belowBar' : 'aboveBar',
          shape: t.side === 'LONG' ? 'arrowUp' : 'arrowDown',
          size: 2,
          color: t.side === 'LONG' ? '#00FF66' : '#FF3366',
          text: `${name} offen`,
        });
        remember(time, {
          label: `${name} offen · ${t.strategy_name || t.strategy_id || '?'}`,
          detail: `Entry ${fmt(t.entry)} · SL ${fmt(t.sl)} · TP ${fmt(t.tpf)} · ${t.mode || ''}`,
          hasDetail: true,
        });
      });

      if (showClosed) {
        closed.forEach(t => {
          const strat = t.strategy_name || t.strategy_id || '?';
          const name = numbered[t.id] || t.side;
          const win = (t.realized_pnl || 0) >= 0;
          const tIn = toBar(t.opened_at);
          if (tIn >= first && tIn <= last) {
            markers.push({
              time: tIn,
              position: t.side === 'LONG' ? 'belowBar' : 'aboveBar',
              shape: 'circle',
              size: 1,
              color: '#5C6680',
              text: `${name} Entry`,
            });
            remember(tIn, {
              label: `${name} Entry (geschlossen) · ${strat}`,
              detail: `Entry ${fmt(t.entry)} · PnL ${fmt(t.realized_pnl)} $`,
            });
          }
          if (t.closed_at) {
            const tOut = toBar(t.closed_at);
            if (tOut >= first && tOut <= last) {
              markers.push({
                time: tOut,
                position: 'inBar',
                shape: win ? 'circle' : 'square',
                size: 1,
                color: win ? '#0f9d58' : '#c73652',
                text: `${name} Closed`,
              });
              remember(tOut, {
                label: `${name} Exit · ${strat}`,
                detail: `Exit ${fmt(t.exit_price)} · PnL ${fmt(t.realized_pnl)} $ (${t.result || '–'})`,
              });
            }
          }
        });
      }

      markers.sort((a, b) => a.time - b.time);
      try {
        if (!markersRef.current) markersRef.current = createSeriesMarkers(series, markers);
        else markersRef.current.setMarkers(markers);
      } catch (_) { /* Chart evtl. gerade neu aufgebaut */ }
      tradeMapRef.current = map;
      setCounts({ open: open.length, closed: closed.length });
    };

    const load = async () => {
      try {
        const res = await fetch(`${API_URL}/api/autotrade/trades?limit=200`);
        const d = await res.json();
        if (!cancelled) apply(d.trades || []);
      } catch (_) { /* Netz-Race beim Laden ignorieren */ }
    };

    load();
    const iv = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(iv);
      hoverDetail(null);
      // eslint-disable-next-line react-hooks/exhaustive-deps
      const series = seriesRef.current;
      pinnedLinesRef.current.forEach(l => {
        try { series && series.removePriceLine(l); } catch (_) { /* noop */ }
      });
      pinnedLinesRef.current = [];
      openRef.current = [];
      clearLines();
      clearMarkers();
      tradeMapRef.current = {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, showClosed, barSec, barsLoaded, reloadKey]);

  return { tradeMapRef, counts, hoverDetail, openTrades, refresh,
           togglePin, pinAtTime, pinnedId };
}
