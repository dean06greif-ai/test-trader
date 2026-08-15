import React, { useState } from 'react';
import { FloppyDisk, Trophy } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;
const fmt = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

const cfgPills = (cfg) => {
  const entries = Object.entries(cfg || {});
  if (!entries.length) return <span className="opt-param-pill">Baseline (aktuelle Einstellungen)</span>;
  return entries.map(([k, v]) => <span key={k} className="opt-param-pill">{k}: <b>{String(v)}</b></span>);
};

const MetricCells = ({ m }) => (
  <>
    <td>{m?.trades ?? '–'}</td>
    <td className={(m?.win_rate || 0) >= 50 ? 'pos' : 'neg'}>{fmt(m?.win_rate, 1)}%</td>
    <td className={`mono ${(m?.pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{fmt(m?.pnl)}</td>
    <td className="mono neg">{fmt(m?.max_drawdown)}</td>
  </>
);

export default function DynamicResult({ result, onSaved }) {
  const dy = result?.dynamic;
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  if (!dy) {
    return (
      <div className="dyn-verdict warn" data-testid="dyn-missing">
        <b>Ergebnis unvollständig</b>
        <div>
          Dieser Dynamik-Lauf enthält keine Regime-Daten. Das passiert, wenn der Lauf von einer
          veralteten lokalen Worker-Version berechnet wurde (sie kennt den Dynamik-Modus noch nicht).
          Bitte das Worker-Paket neu herunterladen (Ausführung → Lokal → ⚙ Verwalten → Download),
          den Worker neu starten und den Lauf wiederholen – oder Cloud-Ausführung wählen.
        </div>
      </div>
    );
  }
  const model = dy.model || {};
  const cmp = dy.comparison || {};
  const verdict = dy.verdict || {};

  const save = async () => {
    setSaving(true);
    try {
      const res = await fetch(`${API_URL}/api/dynamic/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          name: name || `Dynamisch: ${result.strategy_name || result.strategy_id}`,
          strategy_id: result.strategy_id,
          symbols: result.symbols,
          timeframe: result.timeframe,
          model: dy.model,
          configs: dy.configs,
          fallback_config: dy.static_benchmark?.config || {},
          sub_strategies: dy.sub_strategies || {},
          settings: dy.settings,
          verdict: dy.verdict,
        }),
      });
      const d = await res.json();
      if (!res.ok) { toast.error(d.detail || 'Speichern fehlgeschlagen'); return; }
      setSaved(true);
      toast.success('Dynamische Strategie gespeichert – Verwaltung unter "Dynamische Strategien"');
      onSaved && onSaved();
    } catch { toast.error('Verbindungsfehler'); }
    finally { setSaving(false); }
  };

  return (
    <div data-testid="dyn-result">
      <div className="opt-section-title">
        <Trophy size={15} weight="fill" style={{ color: '#FFD700' }} />
        DYNAMISCHE STRATEGIE · {result.strategy_name} · {result.timeframe} · {result.days} Tage
      </div>

      <div className={`dyn-verdict ${verdict.dynamic_better ? 'ok' : 'warn'}`} data-testid="dyn-verdict">
        <b>{verdict.dynamic_better ? '✓ Dynamisch empfohlen' : '✗ Statisch bevorzugen'}</b>
        <div>{verdict.recommendation}</div>
        <ul>{(verdict.reasons || []).map((r, i) => <li key={i}>{r}</li>)}</ul>
      </div>

      <div className="opt-label" style={{ marginTop: 10 }}>
        ERKANNTE MARKTREGIME ({(model.regimes || []).length} · automatisch bestimmt, max. {dy.settings?.max_regimes} ·
        Cluster-Qualität {fmt(model.silhouette, 2)}) – Erkennung ohne Lookahead, Umschalten nur bei
        Sicherheit ≥ {fmt(dy.settings?.confidence_min, 0)}% und Mindesthaltedauer {dy.settings?.min_hold_days} Tage
      </div>
      <div className="opt-table-wrap">
        <table className="opt-table" data-testid="dyn-regime-table">
          <thead><tr><th>Regime</th><th>Anteil</th><th>Trades</th><th>WR</th><th>PnL</th><th>Max DD</th><th>Basis-PnL</th>{dy.per_regime_strategies && <th>Eigene Regeln</th>}<th>Konfiguration</th></tr></thead>
          <tbody>
            {(dy.regimes || []).map((r) => (
              <tr key={r.regime}>
                <td className="opt-small">#{r.regime + 1} {r.label}{r.insufficient && <span className="neg"> · zu wenig Trades → Fallback</span>}</td>
                <td>{fmt(r.share_pct, 0)}%</td>
                <td>{r.metrics?.trades ?? '–'}</td>
                <td className={(r.metrics?.win_rate || 0) >= 50 ? 'pos' : 'neg'}>{fmt(r.metrics?.win_rate, 0)}%</td>
                <td className={`mono ${(r.metrics?.pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{fmt(r.metrics?.pnl)}</td>
                <td className="mono neg">{fmt(r.metrics?.max_drawdown)}</td>
                <td className={`mono ${(r.baseline_metrics?.pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{fmt(r.baseline_metrics?.pnl)}</td>
                {dy.per_regime_strategies && (
                  <td data-testid={`dyn-substrat-${r.regime}`}>
                    <div className="opt-params-list" style={{ margin: 0 }}>
                      {(r.own_strategy?.rules || []).length
                        ? r.own_strategy.rules.map((x, i) => <span key={i} className="opt-param-pill">{x}</span>)
                        : <span className="opt-small">{r.own_strategy?.note || 'Basis-Regeln'}</span>}
                    </div>
                    {r.validation_passed !== null && r.validation_passed !== undefined && (
                      <div className="opt-small">
                        Walk-Forward dieser Phase:{' '}
                        <b className={r.validation_passed ? 'pos' : 'neg'}>
                          {r.validation_passed ? 'bestanden' : 'nicht bestanden'}
                        </b>
                        {r.validation && <> · {r.validation.trades} Trades · PnL {fmt(r.validation.pnl)}</>}
                      </div>
                    )}
                  </td>
                )}
                <td><div className="opt-params-list" style={{ margin: 0 }}>
                  {cfgPills(r.insufficient ? dy.static_benchmark?.config : r.config)}
                  {r.rule_variant && (
                    <span className="opt-param-pill" style={{ borderColor: 'rgba(124,255,178,0.5)' }}
                      title={`Zusätzliche Regel verbessert dieses Regime um ${fmt(r.rule_variant.improvement_pct, 0)}% (${r.rule_variant.metrics?.trades} Trades, PnL ${fmt(r.rule_variant.metrics?.pnl)}). Gilt für Backtest/Analyse – Live nutzt Basis-Regeln + Trade-Parameter.`}
                      data-testid={`dyn-variant-${r.regime}`}>
                      +Regel: <b>{r.rule_variant.rule_label}</b> (+{fmt(r.rule_variant.improvement_pct, 0)}%)
                    </span>
                  )}
                </div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="opt-label" style={{ marginTop: 10 }}>
        VERGLEICH: DYNAMISCH vs. STATISCHE BENCHMARK (gleiches Suchbudget · Test = unbekannte Daten,
        letzte {fmt(100 - (dy.settings?.train_pct || 75), 0)}% des Zeitraums · {dy.test_switches ?? 0} Regimewechsel im Test)
      </div>
      <div className="opt-table-wrap">
        <table className="opt-table" data-testid="dyn-compare-table">
          <thead><tr><th></th><th>Trades</th><th>WR</th><th>PnL</th><th>Max DD</th><th>Trades</th><th>WR</th><th>PnL</th><th>Max DD</th></tr></thead>
          <tbody>
            <tr><td colSpan="1"></td><td colSpan="4" className="opt-small">TRAINING</td><td colSpan="4" className="opt-small">TEST (unbekannt)</td></tr>
            <tr>
              <td className="opt-small"><b>Dynamisch</b></td>
              <MetricCells m={cmp.dynamic?.train} /><MetricCells m={cmp.dynamic?.test} />
            </tr>
            <tr>
              <td className="opt-small">Statisch (Benchmark)</td>
              <MetricCells m={cmp.static?.train} /><MetricCells m={cmp.static?.test} />
            </tr>
          </tbody>
        </table>
      </div>
      <div className="opt-params-list" style={{ marginTop: 6 }}>
        <span className="opt-small" style={{ alignSelf: 'center' }}>STATISCHE BENCHMARK-KONFIG:</span>
        {cfgPills(dy.static_benchmark?.config)}
      </div>
      <div className="opt-small" style={{ margin: '6px 0' }}>
        Hinweis: {dy.settings?.switch_policy}. Regime mit weniger als {dy.settings?.min_trades_per_regime} Trades
        nutzen automatisch die statische Fallback-Konfiguration.
        {dy.rule_variants?._note && <> {dy.rule_variants._note}.</>}
        {dy.per_regime_strategies && (
          <> Pro Marktphase wurde eine <b>eigenständige Sub-Strategie</b> gesucht
          (max. {dy.settings?.max_rules_per_regime} Regeln) – jede hat eigene Regeln UND eigene
          Trade-Parameter. Jede Sub-Strategie muss zusätzlich einen <b>Walk-Forward innerhalb ihrer
          eigenen Marktphase</b> bestehen (Training/Validierung getrennt) – sonst bleibt für diese
          Phase die Basis-Strategie aktiv. Beim Regimewechsel wird live die passende Sub-Strategie aktiv.</>
        )}
      </div>

      <div className="opt-save-row">
        <input type="text" placeholder="Name der dynamischen Strategie"
          value={name} onChange={e => setName(e.target.value)} data-testid="dyn-save-name" />
        <button className="opt-apply" onClick={save} disabled={saving || saved} data-testid="dyn-save">
          <FloppyDisk size={15} weight="bold" />
          {saved ? 'Gespeichert ✓' : 'Als dynamische Strategie speichern'}
        </button>
      </div>
      {!verdict.dynamic_better && (
        <div className="opt-small" style={{ color: '#FFB74D' }}>
          Achtung: Die dynamische Variante war im Test NICHT nachweislich besser als die statische Benchmark –
          Speichern ist möglich, aber die statische Strategie ist aktuell die bessere Wahl.
        </div>
      )}
    </div>
  );
}
