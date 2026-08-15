import React, { useEffect, useState } from 'react';
import { REGIME_BASE_EXPLAIN, REGIME_VOL_EXPLAIN, regimeParts } from './TradeAIDetails';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const BASE_LABEL = {
  trend_up: 'Trend ↑', trend_down: 'Trend ↓', range: 'Seitwärts',
  breakout: 'Ausbruch', drift: 'Drift',
};
const BASE_COLOR = {
  trend_up: '#0ecb81', trend_down: '#f6465d', range: '#8b93a7',
  breakout: '#f0b90b', drift: '#8b93a7',
};

// Regime-Badge über dem Haupt-Chart: aktuelles Markt-Regime (v2) + Tages-Bias
const RegimeBadge = ({ symbol }) => {
  const [feat, setFeat] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`${API_URL}/api/ai/regime/${symbol}`);
        if (!res.ok) return;
        const d = await res.json();
        if (alive) setFeat(d.features || null);
      } catch { /* Badge ist optional */ }
    };
    load();
    const iv = setInterval(load, 60000);
    return () => { alive = false; clearInterval(iv); };
  }, [symbol]);

  if (!feat?.regime) return null;
  const { base, vol } = regimeParts(feat.regime);
  const biasArrow = feat.daily_bias === 'up' ? '▲' : feat.daily_bias === 'down' ? '▼' : '•';
  const tip = [
    `Markt-Regime (letzte Stunde, Regime v2): ${feat.regime}`,
    REGIME_BASE_EXPLAIN[base] || '',
    REGIME_VOL_EXPLAIN[vol] || '',
    feat.vol_rank != null ? `Vol-Rank: aktuelle Schwankung liegt bei P${Math.round(feat.vol_rank)} der eigenen 48h-Historie.` : 'Vol-Basis: Fix-Schwellen (noch zu wenig Historie für Perzentile).',
    feat.trend_1d_pct != null ? `Tages-Bias (${feat.daily_bias || '—'}): 24h ${feat.trend_1d_pct > 0 ? '+' : ''}${feat.trend_1d_pct}%${feat.trend_3d_pct != null ? `, 3 Tage ${feat.trend_3d_pct > 0 ? '+' : ''}${feat.trend_3d_pct}%` : ''} – der übergeordnete Blick gegen das "Würfeln" im Kurzfrist-Regime.` : '',
    `RSI ${feat.rsi} · Range-Position ${feat.range_pos}%`,
  ].filter(Boolean).join('\n');

  return (
    <span className="regime-badge" title={tip} data-testid="chart-regime-badge"
      style={{ borderColor: `${BASE_COLOR[base] || '#8b93a7'}55`, color: BASE_COLOR[base] || '#8b93a7' }}>
      {BASE_LABEL[base] || feat.regime}{vol ? ` · ${vol}` : ''}
      {feat.trend_1d_pct != null && (
        <span className="regime-badge-bias" data-testid="chart-regime-bias">
          {biasArrow} 24h {feat.trend_1d_pct > 0 ? '+' : ''}{Number(feat.trend_1d_pct).toFixed(1)}%
        </span>
      )}
    </span>
  );
};

export default RegimeBadge;
