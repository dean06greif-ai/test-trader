// Gemeinsamer Indikator-Pool für Strategie-Discovery, Optimizer und Regime Lab.
// Gruppiert nach typischem Einsatzzweck – beim Deep-Test / der Endlos-Suche
// dürfen trotzdem ALLE Gruppen gleichzeitig aktiv sein.

export const INDICATOR_GROUPS = [
  {
    key: 'trend',
    label: 'Trend & Richtung',
    hint: 'Typisch für Trendfolge-Strategien: Einstieg in Richtung des übergeordneten Trends.',
    items: [
      { id: 'ema_fast', label: 'EMA Fast', desc: 'Schneller gleitender Durchschnitt. Regeln: Preis über/unter EMA fast sowie EMA fast über/unter EMA slow (klassisches Trend-Signal).' },
      { id: 'ema_slow', label: 'EMA Slow', desc: 'Langsamer gleitender Durchschnitt. Regel: Preis über EMA slow = Aufwärtstrend (Long), darunter = Abwärtstrend (Short).' },
      { id: 'dist_ema200_pct', label: 'EMA 200 Trend', desc: 'Abstand des Preises zur EMA 200 in %. Über der EMA 200 = übergeordneter Aufwärtstrend, darunter = Abwärtstrend. Der meistgenutzte Langfrist-Filter.' },
      { id: 'channel_slope_pct', label: 'Trendkanal-Richtung', desc: 'Steigung des Regressions-Trendkanals in %. Positiv = steigender Kanal (Longs bevorzugen), negativ = fallender Kanal (Shorts).' },
      { id: 'market_structure', label: 'Markt-Struktur (HH/HL)', desc: 'Erkennt die Swing-Struktur: Higher Highs + Higher Lows = intakter Aufwärtstrend, Lower Highs + Lower Lows = Abwärtstrend. Kernkonzept aus Smart-Money/Price-Action.' },
      { id: 'bos_up', label: 'Struktur-Bruch (BOS)', desc: 'Break of Structure: Preis bricht das letzte Swing-Hoch (Long-Signal) bzw. Swing-Tief (Short-Signal) – früher Hinweis auf Trend(fortsetzung/wechsel).' },
    ],
  },
  {
    key: 'momentum',
    label: 'Momentum & Oszillatoren',
    hint: 'Typisch für Einstiegs-Timing: Überkauft/Überverkauft und Schwung-Wechsel.',
    items: [
      { id: 'rsi', label: 'RSI', desc: 'Relative Strength Index (0–100). Unter 25–40 = überverkauft (Long-Chance), über 60–75 = überkauft (Short-Chance). Klassiker für Reversal-Timing.' },
      { id: 'macd', label: 'MACD Cross', desc: 'MACD-Linie kreuzt die Signallinie: Kreuzung nach oben = Long, nach unten = Short. Momentum-Wechsel-Signal.' },
      { id: 'macd_hist', label: 'MACD Histogramm', desc: 'MACD-Histogramm über 0 = positives Momentum (Long), unter 0 = negatives Momentum (Short). Weicher als das Cross, dafür früher.' },
      { id: 'stoch_k', label: 'Stochastik', desc: 'Stochastik-Oszillator: unter 20/30 überverkauft, über 70/80 überkauft; zusätzlich %K/%D-Kreuzung als Timing-Signal.' },
      { id: 'price_change_pct', label: 'Momentum %', desc: 'Kursänderung der letzten Kerzen in %. Über +X% = Aufwärts-Schub (Long), unter −X% = Abwärts-Schub (Short). Einfachster Momentum-Filter.' },
    ],
  },
  {
    key: 'volatility',
    label: 'Volatilität & Volumen (Filter)',
    hint: 'Meist als Zusatz-Filter: handelt nur, wenn genug Bewegung/Volumen im Markt ist. Wirken für Long UND Short gleich.',
    items: [
      { id: 'atr_pct', label: 'ATR %', desc: 'Average True Range in % vom Preis. Filter: nur handeln, wenn die Schwankung über X% liegt – vermeidet tote Märkte. Gleicher Filter für Long und Short.' },
      { id: 'bb_width_pct', label: 'Bollinger Breite', desc: 'Breite der Bollinger-Bänder in %. Enge Bänder = Squeeze (Ausbruch steht oft bevor), weite Bänder = hohe Volatilität. Reiner Filter, keine Richtung.' },
      { id: 'rel_volume', label: 'Rel. Volumen', desc: 'Aktuelles Volumen im Verhältnis zum Durchschnitt. Über 1.2–2.0 = überdurchschnittliches Interesse – bestätigt Ausbrüche und Impulse. Reiner Filter.' },
    ],
  },
  {
    key: 'reversion',
    label: 'Mean-Reversion & Range',
    hint: 'Typisch für Gegenbewegungs-Strategien: Kauf am unteren, Verkauf am oberen Rand.',
    items: [
      { id: 'bb_lower', label: 'Bollinger Reversion', desc: 'Preis unter dem unteren Bollinger-Band = überdehnt nach unten (Long-Reversion); über dem oberen Band = Short-Reversion.' },
      { id: 'bb_upper', label: 'Bollinger Breakout', desc: 'Preis KREUZT das obere Band nach oben = Ausbruchs-Long; Kreuzung unter das untere Band = Ausbruchs-Short. Gegenteil der Reversion-Logik.' },
      { id: 'vwap', label: 'VWAP', desc: 'Volume Weighted Average Price – der „faire" Tagespreis der Institutionellen. Als Trend- (Preis über VWAP = Long) oder Reversion-Signal (Rückkehr zum VWAP) nutzbar.' },
      { id: 'range_pos', label: 'Range-Trading', desc: 'Position des Preises in der jüngsten Handelsspanne (0–100%). Unter 20–30% = nahe Range-Tief (Long), über 70–80% = nahe Range-Hoch (Short).' },
      { id: 'channel_pos', label: 'Trendkanal-Reversion', desc: 'Position im Regressions-Trendkanal (0–100%). Am unteren Kanalrand Long, am oberen Short – Reversion INNERHALB eines Trends.' },
    ],
  },
  {
    key: 'liquidity',
    label: 'Liquidität & Smart Money',
    hint: 'Typisch für institutionelle Setups: wo liegen Stops, wo greift großes Geld zu?',
    items: [
      { id: 'liq_sweep_low', label: 'Liquidity Grab (Sweep)', desc: 'Preis sticht kurz unter ein markantes Tief (holt Stop-Loss-Liquidität ab) und kehrt zurück = Long-Signal; Sweep über ein Hoch = Short-Signal. Kern-Setup der Flossbach-Strategie.' },
      { id: 'eq_low_dist_pct', label: 'Equal Highs/Lows-Nähe', desc: 'Abstand zu Equal Lows/Highs (mehrfach getestete gleiche Levels). Dort liegt institutionelle Liquidität – Preis wird oft dorthin gezogen.' },
      { id: 'dist_support_pct', label: 'Support/Widerstand-Nähe', desc: 'Abstand zum nächsten Support (Long, wenn nah) bzw. Widerstand (Short, wenn nah) in %. Einstiege an bestätigten Levels statt im Niemandsland.' },
    ],
  },
  {
    key: 'events',
    label: 'Events & Termine',
    hint: 'Termin-Filter: an Hochrisiko-Tagen keine neuen Trades eröffnen.',
    items: [
      { id: 'days_to_fomc', label: 'FOMC-Filter', desc: 'Fest eingepflegter FOMC-Kalender 2024–2026: am Tag der Fed-Zinsentscheidung werden KEINE neuen Trades eröffnet (extreme, unberechenbare Volatilität).' },
    ],
  },
];

export const INDICATOR_POOL = INDICATOR_GROUPS.flatMap(g => g.items);
