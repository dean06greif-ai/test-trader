import React, { useCallback, useEffect, useState } from 'react';
import { ArrowRight, SlidersHorizontal } from '@phosphor-icons/react';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmt = (v) => (v === null || v === undefined || v === ''
  ? '–' : typeof v === 'number' ? (Number.isInteger(v) ? v : Number(v.toFixed(4))) : String(v));

/**
 * Vorher/Nachher-Vergleich der Strategie-Parameter und Regel-Schwellen.
 *
 * „Vorher" = Ausgangswerte der Strategie-Definition (bei KI-Strategien die vom
 * Strategie-Labor erzeugten Werte), „Nachher" = aktuell aktive Werte nach
 * Parameter-Optimierung. Rein lesend (`GET /api/strategies/{id}/param-diff`).
 */
const StrategyParamDiff = ({ strategyId, testIdSuffix = '' }) => {
  const [diff, setDiff] = useState(null);
  const [error, setError] = useState(null);
  const [onlyChanged, setOnlyChanged] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/strategies/${strategyId}/param-diff`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Vergleich nicht verfügbar');
      setDiff(data); setError(null);
    } catch (e) { setError(e.message); }
  }, [strategyId]);

  useEffect(() => { load(); }, [load]);

  if (error) {
    return <div className="gov-card-rules" data-testid={`param-diff-error${testIdSuffix}`}>{error}</div>;
  }
  if (!diff) {
    return <div className="gov-card-rules" data-testid={`param-diff-loading${testIdSuffix}`}>Lade Vergleich…</div>;
  }

  const params = onlyChanged ? diff.params.filter(p => p.changed) : diff.params;
  const ruleRows = ['long', 'short'].flatMap(side =>
    ((diff.rules || {})[side] || []).map(r => ({ ...r, side })));
  const shownRules = onlyChanged ? ruleRows.filter(r => r.changed) : ruleRows;

  return (
    <div className="diff-box" data-testid={`param-diff${testIdSuffix}`}>
      <div className="diff-head">
        <SlidersHorizontal size={13} weight="bold" />
        <b>Optimierung: Vorher / Nachher</b>
        <span className="diff-meta">{diff.strategy_name}{diff.timeframe ? ` · ${diff.timeframe}` : ''}</span>
        <label className="diff-filter">
          <input type="checkbox" checked={onlyChanged}
            onChange={e => setOnlyChanged(e.target.checked)}
            data-testid={`param-diff-only-changed${testIdSuffix}`} />
          nur Änderungen
        </label>
      </div>

      {!diff.has_changes && (
        <div className="diff-empty" data-testid={`param-diff-empty${testIdSuffix}`}>
          Noch keine optimierten Werte übernommen – es gelten die Ausgangswerte der Strategie.
        </div>
      )}

      {shownRules.length > 0 && (
        <>
          <div className="diff-sub">Regel-Schwellen</div>
          {shownRules.map((r, i) => (
            <div className={`diff-row ${r.changed ? 'changed' : ''}`} key={`r${i}`}
              data-testid={`param-diff-rule-${r.side}-${r.index}${testIdSuffix}`}>
              <span className="diff-label">{r.side === 'long' ? 'LONG' : 'SHORT'} {r.index + 1}</span>
              <span className="diff-before">{r.before}</span>
              <ArrowRight size={11} weight="bold" />
              <span className="diff-after">{r.after}</span>
            </div>
          ))}
        </>
      )}

      {params.length > 0 && (
        <>
          <div className="diff-sub">Parameter</div>
          {params.map(p => (
            <div className={`diff-row ${p.changed ? 'changed' : ''}`} key={p.key}
              data-testid={`param-diff-param-${p.key}${testIdSuffix}`}>
              <span className="diff-label">{p.label}</span>
              <span className="diff-before">{fmt(p.before)}</span>
              <ArrowRight size={11} weight="bold" />
              <span className="diff-after">{fmt(p.after)}</span>
            </div>
          ))}
        </>
      )}

      {(diff.coins || []).length > 0 && (
        <>
          <div className="diff-sub">Coin-spezifische Werte</div>
          {diff.coins.map(c => (
            <div className="diff-coin" key={c.symbol} data-testid={`param-diff-coin-${c.symbol}${testIdSuffix}`}>
              <b>{c.symbol}</b>
              {c.params.map(p => (
                <span className={`diff-chip ${p.changed ? 'changed' : ''}`} key={p.key}>
                  {p.label}: {fmt(p.before)} → {fmt(p.after)}
                </span>
              ))}
            </div>
          ))}
        </>
      )}

      {(diff.rule_problems || []).length > 0 && (
        <div className="diff-warn" data-testid={`param-diff-problems${testIdSuffix}`}>
          Nicht auswertbare Regeln: {diff.rule_problems.join(' · ')}
        </div>
      )}
    </div>
  );
};

export default StrategyParamDiff;
