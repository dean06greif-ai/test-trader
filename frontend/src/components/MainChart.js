import React, { useEffect, useRef, useState } from 'react';
import { createChart, CandlestickSeries, LineSeries } from 'lightweight-charts';
import useLiquidityOverlay from '../hooks/useLiquidityOverlay';
import useHeatmapOverlay from '../hooks/useHeatmapOverlay';
import useTradeMarkers from '../hooks/useTradeMarkers';
import RegimeBadge from './RegimeBadge';
import './MainChart.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

// Lade-Bereiche für den Chart: LIVE = 1m-Kerzen mit Live-Ticks,
// 1W/1M = lokal geladene Historie (aggregiertes Timeframe, keine Live-Updates)
// Lade-Bereiche: Der Chart ist IMMER live – 1W/1M/1Y laden zusätzlich
// Vergangenheit dazu (aggregiertes Timeframe); erneuter Klick = zurück zu 1m
const RANGES = {
  live: { barSec: 60, subtitle: '1MIN' },
  '1w': { label: '1W', days: 7, barSec: 900, subtitle: '15MIN · 7 TAGE' },
  '1m': { label: '1M', days: 30, barSec: 3600, subtitle: '1H · 30 TAGE' },
  '1y': { label: '1Y', days: 365, barSec: 86400, subtitle: '1D · 1 JAHR' },
};

// Kurz-Erklärungen für die Level-Legende (Hover)
const LEVEL_EXPLAIN = {
  swing_high: 'Swing High: markanter Wendepunkt nach oben – darüber liegen Stop-Losses (Liquiditätspool)',
  swing_low: 'Swing Low: markanter Wendepunkt nach unten – darunter liegen Stop-Losses (Liquiditätspool)',
  eqh: 'Equal Highs: mehrfach getestetes gleiches Hoch – beliebtes Sweep-Ziel über dem Level',
  eql: 'Equal Lows: mehrfach getestetes gleiches Tief – beliebtes Sweep-Ziel unter dem Level',
  fvg: 'Imbalance (Fair Value Gap): Kurslücke aus einem Impuls – wird oft wieder aufgefüllt',
  ob_bull: 'Order Block (Bull): letzte rote Kerze vor einem Impuls nach oben – institutionelle Long-Einstiegszone beim Retest',
  ob_bear: 'Order Block (Bear): letzte grüne Kerze vor einem Impuls nach unten – institutionelle Short-Einstiegszone beim Retest',
  poc: 'Point of Control: Preis mit dem meisten gehandelten Volumen – wirkt wie ein Magnet',
  vah: 'Value Area High: Oberkante der 70%-Volumenzone – oft Widerstand',
  val: 'Value Area Low: Unterkante der 70%-Volumenzone – oft Unterstützung',
  hvn: 'High Volume Node: viel gehandeltes Preisniveau – bremst Bewegungen ab',
  lvn: 'Low Volume Node: kaum gehandeltes Niveau – Preis läuft hier schnell durch',
  round: 'Runde Zahl: psychologisches Level mit vielen Orders',
  day_high: 'Tages-Hoch: darüber liegen Stops und Breakout-Orders',
  day_low: 'Tages-Tief: darunter liegen Stops und Breakout-Orders',
};

// Preis-Genauigkeit je Instrument: Forex (1.1392) und Cent-Coins brauchen mehr
// Dezimalstellen als BTC, sonst kollabieren die Kerzen auf der Preisachse.
const priceFormatFor = (price) => {
  const p = Math.abs(price || 0);
  const precision = p >= 100 ? 2 : p >= 1 ? 4 : p >= 0.01 ? 5 : 8;
  return { type: 'price', precision, minMove: Math.pow(10, -precision) };
};

// Kompakte Preis-Anzeige für die Offene-Trades-Badges
const fmtPrice = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '–';
  const digits = Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 4 : 6;
  return n.toLocaleString('de-DE', { maximumFractionDigits: digits });
};

// EMA helper (client-side overlay)
const ema = (values, period) => {
  // Warmup-EMA ab der ersten Kerze (statt null bis zur Periode): die Linie
  // (auch EMA 200) ist damit über den KOMPLETTEN angezeigten Zeitraum sichtbar
  // und konvergiert nach ~1 Periode gegen die klassische EMA.
  if (!values.length) return [];
  const k = 2 / (period + 1);
  const out = new Array(values.length);
  let prev = values[0];
  out[0] = prev;
  for (let i = 1; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
};

const MainChart = ({ symbol, candleData, signal, onClearSignal }) => {
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const ema9Ref = useRef(null);
  const ema50Ref = useRef(null);
  const ema200Ref = useRef(null);
  const formingRef = useRef(null);
  const emaStateRef = useRef(null);
  const [emaOn, setEmaOn] = useState({ 9: true, 50: true, 200: true });
  const lastTimeRef = useRef(0);
  const resizeObserverRef = useRef(null);
  const resizeRafRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [bars, setBars] = useState(0);
  // Liquiditäts-Overlay: absichtlich NICHT persistiert -> nach jedem Reload/Login
  // wieder aus, damit im Normalbetrieb keine zusätzlichen Abrufe entstehen.
  const [liqOn, setLiqOn] = useState(false);
  const { levels: liqLevels, error: liqError } = useLiquidityOverlay(
    candleSeriesRef, symbol, liqOn);
  // Liquidations-Heatmap als farbige Zonen direkt im Chart (Canvas-Overlay)
  const heatCanvasRef = useRef(null);
  const [heatOn, setHeatOn] = useState(false);
  const { info: heatInfo, error: heatError } = useHeatmapOverlay(
    chartRef, candleSeriesRef, heatCanvasRef, symbol, heatOn);
  // Lade-Bereich (LIVE / 1 Woche / 1 Monat) + Trade-Overlay
  const [range, setRange] = useState('live');
  const [rangeOpen, setRangeOpen] = useState(false);
  const rangeDdRef = useRef(null);
  const [showClosed, setShowClosed] = useState(false);
  const [tradeTip, setTradeTip] = useState(null);
  const { tradeMapRef, counts: tradeCounts, hoverDetail, openTrades, refresh: refreshTrades,
          togglePin, pinAtTime, pinnedId } = useTradeMarkers(
    candleSeriesRef, symbol, showClosed, RANGES[range].barSec, `${range}:${bars}`);

  // Signal-Overlay: Entry/SL/TP-Linien + Regel-Panel beim Klick auf ein Signal
  const signalLinesRef = useRef([]);
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series) return undefined;
    const clear = () => {
      signalLinesRef.current.forEach(l => { try { series.removePriceLine(l); } catch (_) { /* noop */ } });
      signalLinesRef.current = [];
    };
    clear();
    if (!signal || signal.symbol !== symbol) return clear;
    const mk = (price, color, title) => {
      const p = Number(price);
      if (!p) return;
      try {
        signalLinesRef.current.push(series.createPriceLine({
          price: p, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title,
        }));
      } catch (_) { /* noop */ }
    };
    mk(signal.entry_price, '#F5C518', 'ENTRY');
    mk(signal.stop_loss, '#FF3366', 'SL');
    mk(signal.take_profit_1, '#00FF66', 'TP1');
    mk(signal.take_profit_full, '#00CC55', 'TP');
    return clear;
  }, [signal, symbol]);

  // Trades werden nur noch unter "Trades → Offene Trades" geschlossen,
  // nicht mehr direkt im Chart (Badge zeigt nur noch Infos).

  // Zeit-Dropdown bei Klick außerhalb schließen
  useEffect(() => {
    if (!rangeOpen) return undefined;
    const onDown = (e) => {
      if (rangeDdRef.current && !rangeDdRef.current.contains(e.target)) setRangeOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [rangeOpen]);

  // EMA-Linien per Klick auf die Legende ein-/ausschalten
  const emaRefs = { 9: ema9Ref, 50: ema50Ref, 200: ema200Ref };
  const toggleEma = (period) => {
    setEmaOn(prev => {
      const next = { ...prev, [period]: !prev[period] };
      try { emaRefs[period].current?.applyOptions({ visible: next[period] }); } catch (_) { /* noop */ }
      return next;
    });
  };

  // Create chart once
  useEffect(() => {
    if (!chartContainerRef.current) return;
    // European / German time on the chart axis + crosshair tooltip.
    // lightweight-charts renders UTC by default; we override via formatters
    // that use Europe/Berlin so times match German local time incl. DST.
    const berlinTime = (ts, opts) => new Intl.DateTimeFormat('de-DE', {
      timeZone: 'Europe/Berlin', hour12: false, ...opts,
    }).format(new Date(ts * 1000));
    const container = chartContainerRef.current;
    const initialWidth = Math.max(container.clientWidth || 0, 1);
    const initialHeight = Math.max(container.clientHeight || 0, 1);
    // Mobil-Fix: vertikales Wischen über dem Chart scrollt die SEITE statt
    // die Preisskala zu ziehen (Seite war sonst kaum scrollbar). Horizontales
    // Wischen + Pinch-Zoom bedienen weiterhin den Chart. Desktop unverändert.
    const isTouch = window.matchMedia('(pointer: coarse)').matches;
    const chart = createChart(container, {
      layout: { background: { color: '#121212' }, textColor: '#A1A4B0' },
      grid: { vertLines: { color: '#1E2028' }, horzLines: { color: '#1E2028' } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: '#2A2D3A',
        tickMarkFormatter: (time) => berlinTime(time, {
          hour: '2-digit', minute: '2-digit',
        }),
      },
      rightPriceScale: { borderColor: '#2A2D3A' },
      crosshair: { mode: 1 },
      localization: {
        locale: 'de-DE',
        timeFormatter: (time) => berlinTime(time, {
          day: '2-digit', month: '2-digit', year: 'numeric',
          hour: '2-digit', minute: '2-digit',
        }),
      },
      // WICHTIG: kein autoSize:true - das erzeugt in bestimmten Flex-Layouts
      // eine Feedback-Schleife (Chart -> Parent -> ResizeObserver -> Chart),
      // wodurch das Chart-Fenster immer kleiner wird. Wir messen selbst.
      autoSize: false,
      width: initialWidth,
      height: initialHeight,
      handleScroll: isTouch
        ? { vertTouchDrag: false, horzTouchDrag: true, mouseWheel: true, pressedMouseMove: true }
        : true,
      handleScale: isTouch
        ? { pinch: true, mouseWheel: true, axisPressedMouseMove: true, axisDoubleClickReset: true }
        : true,
    });
    chartRef.current = chart;
    candleSeriesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: '#00FF66', downColor: '#FF3366', borderUpColor: '#00FF66',
      borderDownColor: '#FF3366', wickUpColor: '#00FF66', wickDownColor: '#FF3366',
    });
    // EMA-Punkte am Fadenkreuz (Feature aus Version 1.8 übernommen): beim
    // Bewegen des Crosshairs erscheint auf jeder EMA-Linie ein farbiger Punkt.
    ema9Ref.current = chart.addSeries(LineSeries, { color: '#FFD700', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: true, crosshairMarkerRadius: 4 });
    ema50Ref.current = chart.addSeries(LineSeries, { color: '#00A8FF', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: true, crosshairMarkerRadius: 4 });
    ema200Ref.current = chart.addSeries(LineSeries, { color: '#FF5E7A', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: true, crosshairMarkerRadius: 4 });

    // Manuelles Resize per ResizeObserver + rAF-Throttle, mit
    // "letzter angewendeter Größe"-Guard - so triggern wir keine Endlos-Loop.
    let lastAppliedW = initialWidth;
    let lastAppliedH = initialHeight;
    const applySize = () => {
      resizeRafRef.current = null;
      const el = chartContainerRef.current;
      const c = chartRef.current;
      if (!el || !c) return;
      const w = Math.max(Math.floor(el.clientWidth), 1);
      const h = Math.max(Math.floor(el.clientHeight), 1);
      if (w === lastAppliedW && h === lastAppliedH) return; // guard
      lastAppliedW = w;
      lastAppliedH = h;
      try { c.resize(w, h, false); } catch (_) { /* noop */ }
    };
    const scheduleResize = () => {
      if (resizeRafRef.current != null) return;
      resizeRafRef.current = window.requestAnimationFrame(applySize);
    };

    // ResizeObserver auf dem Container (der ist absolute innerhalb .chart-wrap,
    // dadurch beeinflusst seine Größe den Parent nicht -> keine Loop).
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserverRef.current = new ResizeObserver(scheduleResize);
      resizeObserverRef.current.observe(container);
    }
    window.addEventListener('resize', scheduleResize);

    return () => {
      window.removeEventListener('resize', scheduleResize);
      if (resizeRafRef.current != null) {
        window.cancelAnimationFrame(resizeRafRef.current);
        resizeRafRef.current = null;
      }
      if (resizeObserverRef.current) {
        try { resizeObserverRef.current.disconnect(); } catch (_) { /* noop */ }
        resizeObserverRef.current = null;
      }
      try { chart.remove(); } catch (e) { /* noop */ }
      chartRef.current = null;
      candleSeriesRef.current = null;
    };
  }, []);

  // Load historical candles when symbol or range changes
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true); setError(null);
      lastTimeRef.current = 0;
      try {
        const url = range === 'live'
          ? `${API_URL}/api/klines/${symbol}?limit=200`
          : `${API_URL}/api/klines/${symbol}/history?days=${RANGES[range].days}`;
        const res = await fetch(url);
        const data = await res.json();
        const candles = (data.candles || [])
          .map(c => ({ time: Math.floor(c.timestamp / 1000), open: c.open, high: c.high, low: c.low, close: c.close }))
          .filter(c => c.time && Number.isFinite(c.open) && Number.isFinite(c.close));
        // dedupe + sort ascending (lightweight-charts requirement)
        const seen = new Set();
        const clean = [];
        candles.sort((a, b) => a.time - b.time).forEach(c => {
          if (!seen.has(c.time)) { seen.add(c.time); clean.push(c); }
        });
        if (cancelled || !candleSeriesRef.current) return;
        if (clean.length) {
          candleSeriesRef.current.applyOptions({
            priceFormat: priceFormatFor(clean[clean.length - 1].close),
          });
        }
        candleSeriesRef.current.setData(clean);
        // Preisachse zurück auf Auto-Skalierung: sonst bleibt nach manuellem
        // Scrollen (z.B. BTC bei 66k) die Skala beim Coin-Wechsel hängen.
        try {
          chartRef.current && chartRef.current.priceScale('right').applyOptions({ autoScale: true });
        } catch (_) { /* noop */ }
        const closes = clean.map(c => c.close);
        const e9 = ema(closes, 9), e50 = ema(closes, 50), e200 = ema(closes, 200);
        ema9Ref.current.setData(clean.map((c, i) => e9[i] != null ? { time: c.time, value: e9[i] } : null).filter(Boolean));
        ema50Ref.current.setData(clean.map((c, i) => e50[i] != null ? { time: c.time, value: e50[i] } : null).filter(Boolean));
        ema200Ref.current.setData(clean.map((c, i) => e200[i] != null ? { time: c.time, value: e200[i] } : null).filter(Boolean));
        // Zustand für Live-Fortschreibung: EMA-Stand der letzten fertigen Kerze
        const n = clean.length;
        const prevOf = (arr) => (n > 1 && arr[n - 2] != null ? arr[n - 2] : (n ? arr[n - 1] : null));
        emaStateRef.current = {
          9: { prev: prevOf(e9), k: 2 / 10 },
          50: { prev: prevOf(e50), k: 2 / 51 },
          200: { prev: prevOf(e200), k: 2 / 201 },
        };
        formingRef.current = n ? { ...clean[n - 1] } : null;
        lastTimeRef.current = n ? clean[n - 1].time : 0;
        chartRef.current && chartRef.current.timeScale().fitContent();
        setBars(clean.length);
        setLoading(false);
      } catch (e) {
        if (!cancelled) { setError('Chart konnte nicht geladen werden'); setLoading(false); }
      }
    };
    load();
    return () => { cancelled = true; };
  }, [symbol, range]);

  // Live-Updates in ALLEN Ansichten: 1m-Ticks werden in das aktuelle
  // Timeframe-Bucket gemerged (1W→15m, 1M→1h, 1Y→1d); EMAs laufen live mit
  useEffect(() => {
    if (!candleData || !candleSeriesRef.current) return;
    const barSec = RANGES[range].barSec;
    const t = Math.floor(candleData.timestamp / 1000);
    if (!t || !Number.isFinite(candleData.close)) return;
    const bucket = Math.floor(t / barSec) * barSec;
    if (bucket < lastTimeRef.current) return; // never update older data -> prevents crash
    const f = formingRef.current;
    let bar;
    if (f && f.time === bucket) {
      bar = {
        time: bucket, open: f.open,
        high: Math.max(f.high, candleData.high ?? candleData.close),
        low: Math.min(f.low, candleData.low ?? candleData.close),
        close: candleData.close,
      };
    } else {
      // neue Kerze beginnt -> EMA-Stand der fertigen Kerze festschreiben
      const st = emaStateRef.current;
      if (st && f) {
        Object.values(st).forEach(s => {
          if (s.prev != null) s.prev = f.close * s.k + s.prev * (1 - s.k);
        });
      }
      bar = {
        time: bucket,
        open: candleData.open ?? candleData.close,
        high: candleData.high ?? candleData.close,
        low: candleData.low ?? candleData.close,
        close: candleData.close,
      };
    }
    try {
      candleSeriesRef.current.update(bar);
      formingRef.current = bar;
      lastTimeRef.current = bucket;
      const st = emaStateRef.current;
      if (st) {
        const refs = { 9: ema9Ref, 50: ema50Ref, 200: ema200Ref };
        Object.entries(st).forEach(([p, s]) => {
          if (s.prev == null) return;
          const val = bar.close * s.k + s.prev * (1 - s.k);
          try { refs[p].current?.update({ time: bucket, value: val }); } catch (_) { /* noop */ }
        });
      }
    } catch (e) {
      // swallow chart errors so the whole UI never crashes
      console.warn('chart update skipped', e.message);
    }
  }, [candleData, range]);

  // Hover-Tooltip: zeigt Strategie/Details des Trades unter dem Crosshair
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return undefined;
    const handler = (param) => {
      if (!param || !param.time || !param.point) {
        setTradeTip(null);
        hoverDetail(null);
        return;
      }
      const infos = tradeMapRef.current[param.time];
      if (infos && infos.length) {
        setTradeTip({ x: param.point.x, y: param.point.y, infos });
      } else setTradeTip(null);
      // SL/TP-Linien nur zeigen, solange der Entry-Punkt gehovert wird
      hoverDetail(infos && infos.some(i => i.hasDetail) ? param.time : null);
    };
    chart.subscribeCrosshairMove(handler);
    return () => { try { chart.unsubscribeCrosshairMove(handler); } catch (_) { /* noop */ } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Klick auf einen Entry-Pfeil im Chart: Trade anpinnen (Entry/SL/TP1/TP fest sichtbar)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return undefined;
    const handler = (param) => {
      if (!param || !param.time) return;
      const infos = tradeMapRef.current[param.time];
      if (infos && infos.some(i => i.hasDetail)) pinAtTime(param.time);
    };
    chart.subscribeClick(handler);
    return () => { try { chart.unsubscribeClick(handler); } catch (_) { /* noop */ } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="main-chart" data-testid="main-chart">
      <div className="chart-header">
        <div className="chart-title">
          <span className="mono">{symbol}</span>
          <span className="chart-subtitle">{RANGES[range].subtitle} · LIVE</span>
          <RegimeBadge symbol={symbol} />
          <span className="chart-range-dd" data-testid="chart-range-group" ref={rangeDdRef}>
            <button
              className={`chart-liq-toggle range ${range !== 'live' ? 'on' : ''}`}
              onClick={() => setRangeOpen(v => !v)}
              title="Zusätzlichen Zeitraum laden"
              data-testid="chart-range-dropdown-btn"
            >
              Zeit{range !== 'live' ? ` · ${RANGES[range].label}` : ''} ▾
            </button>
            {rangeOpen && (
              <div className="chart-range-menu" data-testid="chart-range-menu">
                {[['1w', '1 Woche'], ['1m', '1 Monat'], ['1y', '1 Jahr']].map(([key, label]) => (
                  <button
                    key={key}
                    className={`chart-range-item ${range === key ? 'active' : ''}`}
                    onClick={() => { setRange(prev => (prev === key ? 'live' : key)); setRangeOpen(false); }}
                    data-testid={`chart-range-${key}`}
                  >
                    <span>{label}</span>
                    <small>{RANGES[key].subtitle}</small>
                  </button>
                ))}
                {range !== 'live' && (
                  <button
                    className="chart-range-item reset"
                    onClick={() => { setRange('live'); setRangeOpen(false); }}
                    data-testid="chart-range-reset"
                  >
                    <span>✕ Zeitraum entfernen</span>
                    <small>zurück zur 1m-Ansicht</small>
                  </button>
                )}
              </div>
            )}
          </span>
        </div>
        <div className="chart-indicators">
          <button
            className={`indicator-label ${emaOn[9] ? '' : 'off'}`}
            onClick={() => toggleEma(9)}
            title="EMA 9 – exponentieller Durchschnitt der letzten 9 Kerzen: schnelle Momentum-/Trigger-Linie für Einstiege. Klick = ein-/ausblenden"
            data-testid="ema9-toggle"
          ><div className="indicator-dot" style={{ background: '#FFD700' }}></div><span>EMA 9</span></button>
          <button
            className={`indicator-label ${emaOn[50] ? '' : 'off'}`}
            onClick={() => toggleEma(50)}
            title="EMA 50 – mittelfristiger Trend: Preis darüber = Aufwärtstrend, darunter = Abwärtstrend. Klick = ein-/ausblenden"
            data-testid="ema50-toggle"
          ><div className="indicator-dot" style={{ background: '#00A8FF' }}></div><span>EMA 50</span></button>
          <button
            className={`indicator-label ${emaOn[200] ? '' : 'off'}`}
            onClick={() => toggleEma(200)}
            title="EMA 200 – langfristige Trend-Linie und viel beachtete Unterstützung/Widerstand (sichtbar ab 200 geladenen Kerzen, am besten in 1W/1M). Klick = ein-/ausblenden"
            data-testid="ema200-toggle"
          ><div className="indicator-dot" style={{ background: '#FF5E7A' }}></div><span>EMA 200</span></button>
          <button
            className={`chart-liq-toggle ${liqOn ? 'on' : ''}`}
            onClick={() => setLiqOn(v => !v)}
            title={liqOn
              ? 'Liquiditäts-Level ausblenden'
              : 'Liquiditäts-Level einblenden (lädt bei Bedarf, standardmäßig aus)'}
            data-testid="chart-liq-toggle"
          >
            LIQ {liqOn ? `· ${liqLevels.length}` : ''}
          </button>
          <button
            className={`chart-liq-toggle heat ${heatOn ? 'on' : ''}`}
            onClick={() => setHeatOn(v => !v)}
            title={heatOn
              ? 'Liquidations-Zonen ausblenden'
              : 'Liquidations-Heatmap als farbige Zonen im Chart einblenden (bevorzugt ECHTE gemessene Liquidationen der letzten 4h; ohne genügend Daten Schätzung aus Hebel-Mathematik + OI + Volumen)'}
            data-testid="chart-heat-toggle"
          >
            HEAT {heatOn && heatInfo ? `· ${heatInfo.zones}` : ''}
          </button>
          <button
            className={`chart-liq-toggle trades ${showClosed ? 'on' : ''}`}
            onClick={() => setShowClosed(v => !v)}
            title={showClosed
              ? 'Nur offene Trades im Chart anzeigen'
              : 'Alle Trades anzeigen (offene + geschlossene: Entry-Pfeil + Exit-Punkt, Hover = Strategie). Ohne Aktivierung sind nur offene Trades sichtbar.'}
            data-testid="chart-trades-toggle"
          >
            ALLE TRADES {showClosed
              ? `· ${(tradeCounts.open || 0) + (tradeCounts.closed || 0)}`
              : ''}
          </button>
        </div>
      </div>
      {heatOn && (heatError || heatInfo) && (
        <div className="chart-liq-legend" data-testid="chart-heat-legend">
          {heatError
            ? <span className="chart-liq-err">{heatError}</span>
            : (
              <>
                <span className="chart-liq-chip heat-low" title="Blaue Zonen: wenig geschätzte Liquidations-Liquidität – Preis läuft hier meist einfach durch">blau = wenig</span>
                <span className="chart-liq-chip heat-mid" title="Orange Zonen: mittlere Liquiditäts-Dichte – erste Magnet-Wirkung auf den Preis">orange = mittel</span>
                <span className="chart-liq-chip heat-high" title="Rote Zonen: dichte Liquidations-Cluster – wirken wie Magnete, Sweeps dorthin sind oft Umkehrpunkte">rot = dichte Liq.-Cluster</span>
                <span className="chart-liq-chip" title="Basis der Zonen: bevorzugt ECHTE gemessene Liquidationen (Force-Orders der Börsen, letzte 4h). Liegen zu wenige gemessene Daten vor, wird auf eine Schätzung aus typischen Hebel-Stufen (10x-100x), Open Interest und Volumen zurückgegriffen. Basis: 15m-Kerzen">{heatInfo?.source === 'measured' ? 'echte Liquidationen (4h)' : 'Schätzung (Hebel + OI + Volumen)'} · 15m</span>
              </>
            )}
        </div>
      )}
      {liqOn && (liqError || liqLevels.length > 0) && (
        <div className="chart-liq-legend" data-testid="chart-liq-legend">
          {liqError ? <span className="chart-liq-err">{liqError}</span>
            : liqLevels.map((l, i) => (
              <span
                key={i}
                className={`chart-liq-chip ${l.side}`}
                title={`${LEVEL_EXPLAIN[l.type] || 'Liquiditäts-Level'}${l.untested ? ' – unberührt: seit Entstehung nicht wieder angelaufen (bevorzugtes Ziel)' : ''} · Stärke ${l.strength}/100`}
              >
                {l.price} · {l.type}{l.untested ? ' (unberührt)' : ''} · {l.strength}
              </span>
            ))}
        </div>
      )}
      <div className="chart-wrap">
        {loading && <div className="chart-overlay" data-testid="chart-loading">Lade {symbol}...</div>}
        {error && <div className="chart-overlay chart-error" data-testid="chart-error">{error}</div>}
        <div ref={chartContainerRef} className="chart-container" />
        {signal && signal.symbol === symbol && (
          <div className={`chart-signal-panel ${signal.type === 'LONG' ? 'long' : 'short'}`} data-testid="chart-signal-panel">
            <div className="csp-head">
              <span className={`csp-badge ${signal.type === 'LONG' ? 'long' : 'short'}`}>
                {signal.signal_class === 'PRE_SIGNAL' ? 'PRE-' : ''}{signal.type}
              </span>
              <span className="csp-title">{signal.strategy_name || signal.strategy_id}</span>
              <span className="csp-time">
                {new Date(signal.timestamp).toLocaleTimeString('de-DE', { timeZone: 'Europe/Berlin', hour: '2-digit', minute: '2-digit' })}
              </span>
              <button className="csp-close" onClick={onClearSignal} title="Signal-Anzeige schließen" data-testid="chart-signal-close">✕</button>
            </div>
            {(signal.rules_snapshot || []).length > 0 ? (
              <div className="csp-rules">
                {(signal.rules_snapshot || []).map((r) => (
                  <div key={r.id} className={`csp-rule ${r.met ? 'met' : ''}`} data-testid={`chart-signal-rule-${r.id}`}>
                    <span className="csp-check">{r.met ? '✓' : '○'}</span>
                    <span className="csp-label">{r.label}</span>
                    <span
                      className={`csp-tf ${r.timeframe ? 'override' : ''}`}
                      title={r.timeframe
                        ? `Diese Regel wird auf dem ${r.timeframe}-Timeframe geprüft (Multi-Timeframe-Filter)`
                        : `Regel läuft auf dem Strategie-Timeframe (${signal.strategy_timeframe || '1m'})`}
                    >
                      {r.timeframe || signal.strategy_timeframe || '1m'}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="csp-empty">Keine Regel-Details gespeichert (älteres Signal)</div>
            )}
          </div>
        )}
        {openTrades.length > 0 && (
          <div className="chart-open-trades" data-testid="chart-open-trades">
            {openTrades.slice(0, 4).map((t) => {
              const px = Number(candleData?.close) || Number(t.entry) || 0;
              const sign = t.side === 'LONG' ? 1 : -1;
              const lev = Number(t.leverage || 1);
              // EINE Quelle für alle Anzeigen: der Server-berechnete uPnL % auf
              // Margin (für Live-Trades der ECHTE Bitunix-uPnL) – identisch mit
              // dem Trade-Verlauf. Lokale Rechnung nur noch als Fallback.
              const qtyRem = Number(t.qty_remaining ?? t.qty) || 0;
              const margin = Number(t.margin_used) > 0
                ? Number(t.margin_used)
                : (t.entry && qtyRem ? (Number(t.entry) * qtyRem) / Math.max(lev, 0.01) : 0);
              const gross = t.entry && qtyRem ? (px - t.entry) * sign * qtyRem : 0;
              const pnlPct = t.computed?.upnl_pct_margin != null
                ? Number(t.computed.upnl_pct_margin)
                : (margin > 0
                  ? (gross / margin) * 100
                  : (t.entry ? ((px - t.entry) / t.entry) * sign * lev * 100 : 0));
              return (
                <div
                  key={t.id}
                  className={`chart-open-badge ${t.side === 'LONG' ? 'long' : 'short'} ${pinnedId === t.id ? 'pinned' : ''}`}
                  onClick={() => togglePin(t.id)}
                  onMouseEnter={() => hoverDetail(t.barTime)}
                  onMouseLeave={() => hoverDetail(null)}
                  title={`${t.strategy_name || t.strategy_id || ''} · Entry ${fmtPrice(t.entry)} · SL ${fmtPrice(t.sl)} · TP ${fmtPrice(t.tpf)} – Klick: Entry/SL/TP1/TP im Chart anpinnen (nochmal Klick = lösen)`}
                  data-testid={`chart-open-badge-${t.id}`}
                >
                  <span className="cob-side">{t.side === 'LONG' ? '▲' : '▼'} {t.label || t.side}</span>
                  {t.horizon === 'swing' && <span className="cob-swing">SWING</span>}
                  <span className="cob-meta">{lev}x · {(t.mode || '').toUpperCase()}</span>
                  <span className="cob-entry">@ {fmtPrice(t.entry)}</span>
                  <span className={`cob-pnl ${pnlPct >= 0 ? 'pos' : 'neg'}`}>
                    {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                  </span>
                </div>
              );
            })}
            {openTrades.length > 4 && (
              <div className="chart-open-badge more">+{openTrades.length - 4} weitere</div>
            )}
          </div>
        )}
        <canvas ref={heatCanvasRef} className="chart-heat-canvas" data-testid="chart-heat-canvas" />
        {tradeTip && (
          <div
            className="chart-trade-tip"
            data-testid="chart-trade-tooltip"
            style={{
              left: Math.min(tradeTip.x + 14, Math.max((chartContainerRef.current?.clientWidth || 400) - 250, 0)),
              top: Math.max(tradeTip.y - 10, 4),
            }}
          >
            {tradeTip.infos.slice(0, 4).map((i, k) => (
              <div key={k} className="chart-trade-tip-row">
                <b>{i.label}</b>
                <span>{i.detail}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default MainChart;
