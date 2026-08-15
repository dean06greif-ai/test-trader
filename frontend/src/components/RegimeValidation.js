import React, { useState } from 'react';
import { CaretDown, CaretRight, CheckCircle, Warning } from '@phosphor-icons/react';
import { fmtDate } from '../lib/time';

const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

/**
 * Ergebnis der logischen Regime-Prüfung: Passt jedes Label zum tatsächlichen
 * Kursverlauf (Richtung, Volatilität)? Zeigt Kennzahlen und konkrete Verstöße.
 */
export default function RegimeValidation({ summary, perSymbol, testId = 'regime-validation' }) {
  const [open, setOpen] = useState(false);
  if (!summary && !perSymbol) return null;
  const s = summary || {};
  const ok = s.passed !== false;
  const rows = Object.entries(perSymbol || {});
  const violations = rows.flatMap(([sym, v]) =>
    ((v?.validation || {}).violations || []).map(x => ({ ...x, symbol: sym })));

  return (
    <div className={`rl-valid ${ok ? 'good' : 'bad'}`} data-testid={testId}>
      <div className="rl-valid-head" onClick={() => setOpen(!open)}>
        {open ? <CaretDown size={12} /> : <CaretRight size={12} />}
        {ok ? <CheckCircle size={13} weight="fill" /> : <Warning size={13} weight="fill" />}
        <b>Regime-Prüfung {ok ? 'bestanden' : 'mit Auffälligkeiten'}</b>
        <span className="opt-small">
          Unplausible Bars <b>{fmt(s.violation_bars_pct, 2)}%</b> ·
          Richtung korrekt <b>{s.direction_accuracy_pct === null || s.direction_accuracy_pct === undefined
            ? '–' : `${fmt(s.direction_accuracy_pct, 1)}%`}</b> ·
          Ø Abschnitt <b>{fmt(s.avg_segment_days, 1)}</b> Tage
        </span>
        <span style={{ flex: 1 }} />
        {violations.length > 0 && (
          <span className="opt-small">{violations.length} Hinweis(e)</span>
        )}
      </div>
      {open && (
        <div className="rl-valid-body">
          <div className="opt-small" style={{ marginBottom: 6 }}>
            Geprüft wird je Abschnitt: Richtung des Labels gegen die tatsächliche
            Kursbewegung (Abschnitt, Sichtfenster und langer Kontext) sowie die
            Volatilitätsstufe. Kurze Gegenbewegungen innerhalb eines Trends gelten
            nicht als Fehler.
          </div>
          {rows.map(([sym, v]) => {
            const val = v?.validation;
            const ideal = v?.ideal?.agreement;
            if (!val) return null;
            return (
              <div key={sym} className="opt-small" style={{ marginBottom: 3 }}>
                <b>{sym.replace('USDT', '')}</b>: {val.segments} Abschnitte ·
                {' '}unplausibel <b>{fmt(val.violation_bars_pct, 2)}%</b> ·
                {' '}Richtung <b>{val.direction_accuracy_pct === null ? '–' : `${fmt(val.direction_accuracy_pct, 1)}%`}</b>
                {ideal ? <> · Übereinstimmung mit Rückblick-Sicht <b>{fmt(ideal.direction_pct, 1)}%</b> (Richtung) / <b>{fmt(ideal.exact_pct, 1)}%</b> (exakt)</> : null}
              </div>
            );
          })}
          {violations.slice(0, 12).map((x, i) => (
            <div key={i} className="rl-valid-row" data-testid={`${testId}-violation-${i}`}>
              <b>{x.symbol.replace('USDT', '')}</b>
              <span>{fmtDate(x.from_ts)} → {fmtDate(x.to_ts)}</span>
              <span>{x.label}</span>
              <span className={x.net_pct >= 0 ? 'pos' : 'neg'}>{fmt(x.net_pct, 2)}%</span>
              <span className="opt-small">{(x.problems || []).join(' · ')}</span>
            </div>
          ))}
          {!violations.length && (
            <div className="opt-small pos">Keine unplausiblen Abschnitte gefunden.</div>
          )}
        </div>
      )}
    </div>
  );
}
