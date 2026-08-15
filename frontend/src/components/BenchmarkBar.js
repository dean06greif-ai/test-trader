import React from 'react';
import { Timer, Cpu, Database, DownloadSimple, Desktop, Cloud } from '@phosphor-icons/react';
import './BenchmarkBar.css';

const fmtSec = (s) => {
  if (s == null) return '–';
  if (s >= 90) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
};
const fmtNum = (n) => (n >= 1000000 ? `${(n / 1000000).toFixed(1)} Mio` : (n || 0).toLocaleString('de-DE'));

/** Laufzeit-Statistik eines Backtest-/Optimizer-Laufs (result.benchmark). */
export default function BenchmarkBar({ b, testid = 'benchmark-bar' }) {
  if (!b) return null;
  const local = b.execution === 'local';
  const mc = (b.workers || 1) > 1;
  return (
    <div className="bench-bar" data-testid={testid}>
      <span className={`bench-item ${local ? 'good' : ''}`}
        title={local
          ? `Berechnet auf dem lokalen PC${b.worker_name ? ` (${b.worker_name})` : ''} – nicht in der Cloud`
          : 'In der Cloud berechnet. Für mehr Tempo: lokalen Worker starten'}
        data-testid={`${testid}-execution`}>
        {local ? <Desktop size={13} weight="bold" /> : <Cloud size={13} weight="bold" />}
        {local ? `Lokal${b.worker_name ? ` · ${b.worker_name}` : ''}` : 'Cloud'}
      </span>
      <span className="bench-item" title="Gesamtlaufzeit · davon Daten laden / Simulation">
        <Timer size={13} weight="bold" />
        {fmtSec(b.total_seconds)}
        <span className="bench-sub">Daten {fmtSec(b.data_seconds)} · Sim {fmtSec(b.sim_seconds)}</span>
      </span>
      <span className={`bench-item ${mc ? 'good' : ''}`}
        title={mc
          ? `Multi-Core: ${b.workers} Prozesse. Reine Rechenzeit ${fmtSec(b.cpu_seconds)} auf ${fmtSec(b.sim_seconds)} Wanduhrzeit verteilt`
          : 'Sequenziell (1 Kern). Multi-Core: Lokale Ausführung + CPU-Kerne im ⚙-Panel'}
        data-testid={`${testid}-cores`}>
        <Cpu size={13} weight="bold" />
        {mc ? `${b.workers} Kerne · Speedup ${(b.parallel_speedup || 1).toFixed(1)}×` : '1 Kern'}
      </span>
      {(b.cached_candles > 0) && (
        <span className="bench-item good"
          title={`${fmtNum(b.cached_candles)} von ${fmtNum(b.raw_candles)} Kerzen kamen aus dem ${local ? 'lokalen ' : ''}Cache statt aus dem Netz – geschätzte Ersparnis ~${fmtSec(b.est_cache_saved_seconds)}`}
          data-testid={`${testid}-cache`}>
          <Database size={13} weight="bold" />
          Cache {b.cache_ratio}% (~{fmtSec(b.est_cache_saved_seconds)} gespart)
        </span>
      )}
      {(b.downloaded_candles > 0) && (
        <span className="bench-item" title="Frisch von Binance geladene 1m-Kerzen"
          data-testid={`${testid}-downloaded`}>
          <DownloadSimple size={13} weight="bold" />
          {fmtNum(b.downloaded_candles)} Kerzen geladen
        </span>
      )}
      <span className="bench-item" title={b.evaluations != null
        ? 'Anzahl bewerteter Kombinationen und simulierte Kerzen'
        : 'Simulierte (Strategie × Coin)-Paare und Kerzen'}>
        {b.evaluations != null
          ? `${fmtNum(b.evaluations)} Evaluierungen`
          : `${b.pairs || 0} Paare`} · {fmtNum(b.sim_candles)} Kerzen
        {b.dyn_segments > 0 && (
          <span className="bench-sub">{fmtNum(b.dyn_segments)} Regime-Abschnitte simuliert</span>
        )}
      </span>
    </div>
  );
}
