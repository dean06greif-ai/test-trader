import React, { useState } from 'react';
import { MagnifyingGlass, Brain, X } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;

// Ausführliche Setup-Erklärungen (Hover auf dem Setup-Badge)
export const SETUP_EXPLAIN = {
  trend_follow: 'Trend folgen: Einstieg in Richtung des bestehenden Trends (EMA-Struktur zeigt klar nach oben/unten). Ziel: die Bewegung läuft weiter. SL hinter der letzten Struktur, TP in Trendrichtung.',
  breakout: 'Ausbruch: Der Preis verlässt eine Range/Konsolidierung mit Schwung. Einstieg in Ausbruchsrichtung, SL zurück hinter das Ausbruchslevel (dann wäre der Ausbruch gescheitert), TP in der freien Kurszone dahinter.',
  squeeze_breakout: 'Squeeze-Ausbruch: Nach extrem enger, ruhiger Seitwärtsphase (niedrige Volatilität) wird auf den explosiven Ausbruch gesetzt – enge Ranges entladen sich oft heftig.',
  mean_reversion: 'Rückkehr zum Mittelwert: Nach einer Übertreibung (z.B. RSI-Extrem, weit weg vom EMA) wird auf die Gegenbewegung zurück zum Durchschnitt gesetzt. SL hinter dem Extrem, TP am Mittelwert.',
  range_fade: 'Range-Fade: An der Ober-/Unterkante einer Seitwärtsrange GEGEN den Rand handeln (oben short, unten long). Ziel ist die andere Rangeseite, SL knapp außerhalb der Range.',
  liquidity_sweep: 'Liquidity Sweep: Der Preis stößt kurz über ein markantes Hoch/Tief (dort liegen viele Stop-Orders = Liquidität), holt diese Stops ab und dreht. Einstieg in die Umkehrbewegung.',
  momentum_news: 'News-Momentum: Eine Nachricht löst eine starke Bewegung aus. Einstieg in die Impulsrichtung, solange das Momentum trägt – enger Zeithorizont, News-Kontext steht in der Begründung.',
  pullback: 'Pullback: Rücksetzer in einem intakten Trend. Einstieg beim Abpraller an EMA oder Struktur-Level in Trendrichtung – günstigerer Einstieg als dem Trend hinterherzulaufen.',
  swing_trend: 'Swing-Trend: Übergeordneter, langsamer Trade mit weiten Zielen, niedrigem Hebel und Halten über Stunden/Tage – läuft parallel zu kurzfristigen Scalps.',
  hedge: 'Hedge: Bewusste Gegenposition zu einem bestehenden Trade, um dessen Risiko abzufedern, statt ihn zu schließen.',
};

export const REGIME_BASE_EXPLAIN = {
  trend_up: 'Aufwärtstrend: EMA20 liegt über EMA50 – die letzte Stunde zeigt nach oben.',
  trend_down: 'Abwärtstrend: EMA20 liegt unter EMA50 – die letzte Stunde zeigt nach unten.',
  range: 'Seitwärts: Preis bewegt sich mitten in der 60-Minuten-Range, keine klare Richtung.',
  breakout: 'Bestätigter Ausbruch: Preis am Rand der 60-Minuten-Range MIT deutlichem Move oder Volumen-Spike in Randrichtung.',
  drift: 'Drift: Preis am Rand der 60-Minuten-Range, aber OHNE bestätigten Ausbruch – eher Treiben als echtes Momentum.',
};

export const REGIME_VOL_EXPLAIN = {
  volatil: 'volatil = Schwankung aktuell im obersten Fünftel der eigenen 48h-Historie (≥P80)',
  ruhig: 'ruhig = Schwankung im untersten Drittel der eigenen Historie (≤P30)',
  normal: 'normal = Schwankung im mittleren Bereich der eigenen Historie',
};

export const regimeParts = (regime) => {
  const r = String(regime || '');
  const base = ['trend_up', 'trend_down', 'breakout', 'range', 'drift'].find(b => r.startsWith(b));
  const vol = ['volatil', 'ruhig', 'normal'].find(v => r.endsWith(v));
  return { base, vol };
};

const fmt = (v, d = 2) => (v == null ? '—' : Number(v).toFixed(d));

const fmtDurShort = (s) => {
  if (s == null) return '—';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
};

const Section = ({ title, children }) => (
  <div className="tdx-section">
    <div className="tdc-tl-title">{title}</div>
    {children}
  </div>
);

// Details-Modal + "Trade überdenken" an der Trade-Karte
const TradeAIDetails = ({ trade, onChanged }) => {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [rethink, setRethink] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = async () => {
    setOpen(true);
    try {
      const res = await fetch(`${API_URL}/api/autotrade/trade/${trade.id}/explain`);
      if (!res.ok) throw new Error('Erklärung konnte nicht geladen werden');
      setData(await res.json());
    } catch (e) { toast.error(e.message); }
  };

  const doRethink = async () => {
    setBusy(true);
    setRethink(null);
    setMsg(null);
    try {
      const res = await fetch(`${API_URL}/api/autotrade/trade/${trade.id}/rethink`, {
        method: 'POST', headers: authHeaders(),
      });
      const d = await res.json().catch(() => ({}));
      if (res.status === 429) { setMsg(d.detail || 'Cooldown aktiv – gleich nochmal probieren'); return; }
      if (res.status === 401) { setMsg('Admin-Login erforderlich (Schloss oben rechts)'); return; }
      if (!res.ok) throw new Error(d.detail || 'Überdenken fehlgeschlagen');
      setRethink(d);
      if ((d.applied || []).length > 0) { toast.success('KI hat Aktionen ausgeführt'); onChanged && onChanged(); }
    } catch (e) { setMsg(e.message); toast.error(e.message); } finally { setBusy(false); }
  };

  const canRethink = trade.status === 'open' && trade.strategy_id === 'ai_trader';
  const f = data?.facts || {};
  const dec = data?.decision;
  const rf = dec?.regime_features || {};
  const { base, vol } = regimeParts(dec?.regime);

  return (
    <>
      <div className="tdx-btn-row">
        <button className="tdc-chart-btn" onClick={load} data-testid={`trade-explain-btn-${trade.id}`}
          title="Ausführliche Erklärung: volle KI-Begründung, warum Positionsgröße/SL/TP so gewählt wurden, Marktlage beim Entry – kostenlos, ohne KI-Aufruf">
          <MagnifyingGlass size={13} weight="bold" /> Details
        </button>
        {canRethink && (
          <button className="tdc-chart-btn" onClick={() => { if (!open) load(); doRethink(); }} disabled={busy}
            data-testid={`trade-rethink-btn-${trade.id}`}
            title="Die Trade-Manager-KI überdenkt NUR diesen Trade neu (1 kleiner KI-Aufruf, max. 1× pro 15 min). Ist die These noch intakt? Empfohlene Aktionen (SL anpassen, teilschließen, schließen) werden über die geprüfte Sicherheits-Schicht direkt ausgeführt.">
            <Brain size={13} weight="bold" /> {busy ? 'Überdenkt…' : 'Trade überdenken'}
          </button>
        )}
      </div>
      {msg && <div className="tdx-sub" style={{ color: '#f6465d' }} data-testid={`trade-rethink-msg-${trade.id}`}>{msg}</div>}
      {open && (
        <div className="tdx-overlay" onClick={() => setOpen(false)} data-testid={`trade-explain-modal-${trade.id}`}>
          <div className="tdx-modal" onClick={e => e.stopPropagation()}>
            <div className="tdx-head">
              <span>KI-Erklärung · {trade.symbol} {String(trade.side || '').toUpperCase()}</span>
              <button className="tdx-close" onClick={() => setOpen(false)} data-testid={`trade-explain-close-${trade.id}`}><X size={14} /></button>
            </div>
            {!data ? <div className="no-data">Lädt…</div> : (
              <div className="tdx-body">
                {data.state && (
                  <Section title={data.state.status === 'open' ? 'AKTUELLER STAND' : 'ERGEBNIS'}>
                    {data.state.status === 'open' ? (
                      <>
                        <div className="tdx-sub" data-testid={`trade-explain-state-${trade.id}`}>
                          Kurs {fmt(data.state.current_price, 6)} · uPnL {fmt(data.state.unrealized_pnl)} $ (inkl. Fees {fmt(data.state.live_pnl)} $) · {data.state.r_multiple != null ? `${fmt(data.state.r_multiple, 2)}R` : '—'} · offen seit {fmtDurShort(data.state.duration_seconds)}
                        </div>
                        <div className="tdx-sub" title="Wie weit der aktuelle Kurs von den Levels entfernt ist – kleiner SL-Abstand heißt: Entscheidung steht kurz bevor.">
                          Abstand: SL {fmt(data.state.sl_distance_pct)}% · TP1 {fmt(data.state.tp1_distance_pct)}% · TP Full {fmt(data.state.tpf_distance_pct)}%
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="tdx-sub" data-testid={`trade-explain-state-${trade.id}`}>
                          Exit {fmt(data.state.exit_price, 6)} · {data.state.result === 'win' ? 'GEWINN' : data.state.result === 'loss' ? 'VERLUST' : (data.state.result || '—')} · Dauer {fmtDurShort(data.state.duration_seconds)}
                        </div>
                        <div className="tdx-sub" title="Zerlegung des Ergebnisses: Kurs-Gewinn/Verlust (brutto) minus alle tatsächlich gezahlten Gebühren = das, was wirklich auf dem Konto ankommt.">
                          Brutto {fmt(data.state.gross_pnl)} $ − Gebühren {fmt(data.state.fees_paid)} $ = <b>Netto {fmt(data.state.realized_pnl_net)} $</b>{data.state.r_multiple != null ? ` (${fmt(data.state.r_multiple, 2)}R)` : ''}
                        </div>
                      </>
                    )}
                  </Section>
                )}
                <Section title="VOLLE KI-BEGRÜNDUNG">
                  <div className="tdc-ai-text">{dec?.reasoning || trade.ai_reasoning || 'Keine gespeicherte Begründung (Trade vor dem Update oder nicht vom KI Trader).'}</div>
                  {dec?.model && <div className="tdx-sub">Modell: {dec.model}{dec.confidence != null ? ` · Konfidenz ${dec.confidence}%` : ''}{dec.gate_p_win != null ? ` · ML-Gate p(win) ${(dec.gate_p_win * 100).toFixed(0)}%` : ''}</div>}
                </Section>
                <Section title="WARUM DIESE POSITIONSGRÖSSE">
                  {(dec?.size_reason || trade.ai_size_reason) && <div className="tdc-ai-text">{dec?.size_reason || trade.ai_size_reason}</div>}
                  <div className="tdx-sub">
                    Margin {fmt(f.margin_usdt)} $ × Hebel {fmt(f.leverage, 0)}x = Position {fmt(f.notional_usdt)} $
                    {dec?.capital_pct != null ? ` · KI wählte ${dec.capital_pct}% des erlaubten Kapitals` : ''}
                    {f.risk_usd != null ? ` · Risiko bis SL: ${fmt(f.risk_usd)} $${f.risk_pct_of_margin != null ? ` (${fmt(f.risk_pct_of_margin, 1)}% der Margin)` : ''}` : ''}
                  </div>
                </Section>
                <Section title="WARUM SL/TP DORT">
                  {(dec?.levels_reason || trade.ai_levels_reason) && <div className="tdc-ai-text">{dec?.levels_reason || trade.ai_levels_reason}</div>}
                  <div className="tdx-sub">
                    SL {fmt(f.sl_dist_pct)}% vom Entry ({fmt(f.risk_usd)} $) · TP1 {fmt(f.tp1_dist_pct)}%{f.crv_tp1 != null ? ` (CRV ${fmt(f.crv_tp1, 1)})` : ''} · TP Full {fmt(f.tpf_dist_pct)}%{f.crv_tpf != null ? ` (CRV ${fmt(f.crv_tpf, 1)})` : ''}
                  </div>
                  <div className="tdx-sub" title="Der Fee-Wächter blockt Trades, deren SL-Distanz kleiner als das eingestellte Vielfache der Roundtrip-Gebühren ist – mathematisch garantierte Fee-Verlierer kommen gar nicht durch.">
                    Fees: Roundtrip ≈ {fmt(f.roundtrip_fees_usdt)} $ = {fmt(f.fees_vs_risk_pct, 0)}% des Risikos · Fee-Wächter verlangt SL ≥ {fmt(f.fee_guard_min_sl_pct)}%
                  </div>
                </Section>
                {(dec?.regime || dec?.news_impact) && (
                  <Section title="MARKT BEIM ENTRY">
                    {dec?.regime && (
                      <div className="tdx-sub" title={`${REGIME_BASE_EXPLAIN[base] || ''} ${REGIME_VOL_EXPLAIN[vol] || ''}`}>
                        Regime: <b>{dec.regime}</b>{rf.vol_rank != null ? ` · Vol-Rank P${Math.round(rf.vol_rank)} der eigenen 48h` : ''}{rf.rsi != null ? ` · RSI ${fmt(rf.rsi, 0)}` : ''}
                      </div>
                    )}
                    {rf.trend_1d_pct != null && <div className="tdx-sub">Tages-Trend: 24h {rf.trend_1d_pct > 0 ? '+' : ''}{fmt(rf.trend_1d_pct)}%{rf.trend_3d_pct != null ? ` · 3d ${rf.trend_3d_pct > 0 ? '+' : ''}${fmt(rf.trend_3d_pct)}%` : ''}{rf.daily_bias ? ` (Bias: ${rf.daily_bias})` : ''}</div>}
                    {dec?.news_impact && <div className="tdx-sub">News-Einschätzung: {dec.news_impact}</div>}
                  </Section>
                )}
                {(rethink || trade.rethink_note) && (
                  <Section title="LETZTES ÜBERDENKEN">
                    <div className="tdc-ai-text" data-testid={`trade-rethink-note-${trade.id}`}>{rethink?.note || trade.rethink_note}</div>
                    {(rethink?.applied || []).map((a, i) => (
                      <div key={i} className="tdx-sub tdx-applied">✔ {a.action} ausgeführt – {a.reason}</div>
                    ))}
                    {(rethink?.skipped || []).map((a, i) => (
                      <div key={i} className="tdx-sub">✖ {a.action} verworfen ({a.detail})</div>
                    ))}
                    {rethink?.model && <div className="tdx-sub">Modell: {rethink.model}</div>}
                  </Section>
                )}
                {!dec && <div className="tdx-sub">Hinweis: Für Trades vor diesem Update fehlen einzelne Felder (volle Entscheidung/Gründe) – neue Trades haben alles.</div>}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
};

export default TradeAIDetails;
