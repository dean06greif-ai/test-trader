import React, { useCallback, useEffect, useState } from 'react';
import { Trophy, ArrowsClockwise, TrendUp, TrendDown, Trash } from '@phosphor-icons/react';
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip,
  ReferenceLine, CartesianGrid,
} from 'recharts';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import './AIRewardPanel.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const fmtTs = (ts) => {
  try {
    return new Date(ts).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', timeZone: 'Europe/Berlin' });
  } catch (e) { return ''; }
};

const RewardTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const p = payload[0]?.payload || {};
  return (
    <div className="reward-tooltip">
      <div className="reward-tt-head">{p.symbol} · {p.side} · {p.mode}</div>
      <div>Reward: <b className={p.score >= 0 ? 'pos' : 'neg'}>{(p.score ?? 0).toFixed(2)}</b></div>
      <div>Kumuliert: <b className={p.cum >= 0 ? 'pos' : 'neg'}>{(p.cum ?? 0).toFixed(2)}</b></div>
      <div>PnL: {(p.pnl ?? 0).toFixed(2)} USDT{p.regime ? ` · Regime: ${p.regime}` : ''}</div>
      <div className="reward-tt-ts">{p.ts ? new Date(p.ts).toLocaleString('de-DE', { timeZone: 'Europe/Berlin' }) : ''}</div>
    </div>
  );
};

const AIRewardPanel = () => {
  const [data, setData] = useState(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (d) => {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/rewards?days=${d}`);
      if (res.ok) setData(await res.json());
    } catch (e) { /* silent */ }
    setLoading(false);
  }, []);

  useEffect(() => { load(days); }, [days, load]);

  const clearRewards = async () => {
    if (!window.confirm('Alle Belohnungssystem-Daten (Reward-Verlauf) unwiderruflich löschen? '
      + 'Neue Trades werden danach wieder normal bewertet. '
      + 'Die Löschung wird im Audit-Log protokolliert (wer/wann).')) return;
    try {
      const res = await fetch(`${API_URL}/api/ai/rewards`, { method: 'DELETE', headers: authHeaders() });
      const d = await res.json();
      if (res.status === 401) { toast.error('Admin-Login erforderlich'); return; }
      if (!res.ok) throw new Error(d.detail || 'Löschen fehlgeschlagen');
      toast.success(`Belohnungsdaten gelöscht (${d.deleted ?? 0} Einträge)`);
      load(days);
    } catch (e) { toast.error(e.message); }
  };

  // Auto-Fallback: wenn der Standard-Zeitraum leer ist, aber ältere bewertete
  // Trades existieren könnten, einmalig auf 90 Tage erweitern.
  const [fellBack, setFellBack] = useState(false);
  useEffect(() => {
    if (!fellBack && data && (data.history || []).length === 0 && days < 90) {
      setFellBack(true);
      setDays(90);
    }
  }, [data, days, fellBack]);

  const hist = data?.history || [];
  const regimes = data?.by_regime || [];
  const sum = data?.summary || {};

  return (
    <div className="ai-reward-panel" data-testid="ai-reward-panel">
      <div className="reward-head">
        <span className="reward-title">
          <Trophy size={14} weight="fill" /> Belohnungssystem · Reward-Verlauf
        </span>
        <select value={days} onChange={e => setDays(Number(e.target.value))} data-testid="reward-days-select">
          {[7, 14, 30, 60, 90].map(v => <option key={v} value={v}>{v} Tage</option>)}
        </select>
        <button className="reward-reload" onClick={() => load(days)} title="Neu laden" data-testid="reward-reload-btn">
          <ArrowsClockwise size={13} weight="bold" className={loading ? 'spin' : ''} />
        </button>
        <button className="reward-reload" onClick={clearRewards}
          title="Alle Belohnungsdaten löschen (mit Sicherheitsabfrage)"
          style={{ color: '#FF3366' }} data-testid="reward-clear-btn">
          <Trash size={13} weight="bold" />
        </button>
      </div>
      <div className="reward-summary" data-testid="reward-summary">
        <span>Gesamt-Reward <b className={(sum.total ?? 0) >= 0 ? 'pos' : 'neg'}>{(sum.total ?? 0).toFixed(2)}</b></span>
        <span>Ø/Trade <b className={(sum.avg ?? 0) >= 0 ? 'pos' : 'neg'}>{(sum.avg ?? 0).toFixed(2)}</b></span>
        <span><b>{sum.trades ?? 0}</b> bewertete Trades</span>
        {sum.trend !== null && sum.trend !== undefined && (
          <span className={`reward-trend ${sum.trend >= 0 ? 'pos' : 'neg'}`} data-testid="reward-trend">
            {sum.trend >= 0 ? <TrendUp size={13} weight="bold" /> : <TrendDown size={13} weight="bold" />}
            Trend {sum.trend >= 0 ? '+' : ''}{sum.trend.toFixed(2)} (letzte 10 vs. davor)
          </span>
        )}
      </div>
      {hist.length > 1 ? (
        <div className="reward-chart" data-testid="reward-chart">
          <ResponsiveContainer width="100%" height={160}>
            <AreaChart data={hist} margin={{ top: 6, right: 8, bottom: 0, left: -14 }}>
              <defs>
                <linearGradient id="rewardGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#4ade80" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#4ade80" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1d2338" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="ts" tickFormatter={fmtTs} tick={{ fill: '#5c6070', fontSize: 10 }}
                minTickGap={40} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: '#5c6070', fontSize: 10 }} axisLine={false} tickLine={false} width={44} />
              <Tooltip content={<RewardTooltip />} />
              <ReferenceLine y={0} stroke="#39415f" strokeDasharray="4 4" />
              <Area type="monotone" dataKey="cum" stroke="#4ade80" strokeWidth={1.8}
                fill="url(#rewardGrad)" dot={false} activeDot={{ r: 3 }} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="reward-empty">
          Noch keine bewerteten Trades – jeder geschlossene KI-Trade wird automatisch
          belohnt/bestraft (PnL, Win/Loss, Sofort-Stop-Outs, Konfidenz- &amp; CRV-Disziplin).
        </div>
      )}
      {regimes.length > 0 && (
        <div className="reward-regimes" data-testid="reward-regimes">
          <div className="reward-sub">Reward pro Markt-Regime</div>
          <table>
            <thead>
              <tr><th>Regime</th><th>Trades</th><th>Ø Reward</th><th>Winrate</th><th>PnL</th></tr>
            </thead>
            <tbody>
              {regimes.map(r => (
                <tr key={r.regime} data-testid={`reward-regime-${r.regime}`}>
                  <td>{r.regime}</td>
                  <td>{r.trades}</td>
                  <td className={r.avg_reward >= 0 ? 'pos' : 'neg'}>{r.avg_reward >= 0 ? '+' : ''}{r.avg_reward.toFixed(2)}</td>
                  <td>{r.win_rate}%</td>
                  <td className={r.pnl >= 0 ? 'pos' : 'neg'}>{r.pnl >= 0 ? '+' : ''}{r.pnl.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="reward-hint">
        Der Reward-Score fließt in jeden Lernlauf ein: die KI sieht ihre Belohnungskurve,
        die häufigsten Malus-Gründe und das Reward-Profil pro Regime – und leitet daraus Lektionen ab.
      </div>
    </div>
  );
};

export default AIRewardPanel;
