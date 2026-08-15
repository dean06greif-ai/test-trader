import React, { useState } from 'react';
import { Brain, CaretDown, CaretRight } from '@phosphor-icons/react';

const API_URL = process.env.REACT_APP_BACKEND_URL;

export default function LearningPanel() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !data) {
      fetch(`${API_URL}/api/learning/summary`).then(r => r.json())
        .then(setData).catch(() => setData({ entries: 0, regimes: [] }));
    }
  };

  return (
    <div className="dyn-panel" data-testid="learning-panel">
      <button className={`opt-chip opt-history-btn ${open ? 'on' : ''}`} onClick={toggle}
        data-testid="learning-toggle"
        title="Lern-Gedächtnis: sammelt aus jedem Lauf, welche Indikatoren/Strategien in welcher Marktphase funktioniert haben – und sortiert damit z.B. die Regel-Varianten im Dynamik-Modus">
        {open ? <CaretDown size={13} /> : <CaretRight size={13} />} <Brain size={13} /> Lern-Gedächtnis
      </button>
      {open && (
        <div className="opt-history" data-testid="learning-body">
          {!data && <div className="opt-small">Lade...</div>}
          {data && data.entries === 0 && (
            <div className="opt-small">
              Noch keine Lern-Daten – jeder abgeschlossene Optimizer-Lauf (v.a. mit Regime-Analyse
              oder im Dynamik-Modus) füllt das Gedächtnis automatisch.
            </div>
          )}
          {data && data.regimes.map(g => (
            <div key={g.label} className="dyn-card">
              <div className="dyn-card-head">
                <b>{g.label}</b>
                <span className="opt-small">{g.runs} Einträge · {g.profitable_pct}% profitabel</span>
              </div>
              {g.top_indicators.length > 0 && (
                <div className="opt-small">
                  Beste Indikatoren: {g.top_indicators.map(i => `${i.indicator} (${i.pnl >= 0 ? '+' : ''}${i.pnl})`).join(' · ')}
                </div>
              )}
              {g.top_strategies.length > 0 && (
                <div className="opt-small">
                  Beste Strategien: {g.top_strategies.map(sx => `${sx.name} (${sx.pnl >= 0 ? '+' : ''}${sx.pnl})`).join(' · ')}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
