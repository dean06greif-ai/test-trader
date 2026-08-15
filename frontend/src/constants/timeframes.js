export const TIMEFRAMES = [
  { v: '1m', l: '1 Min' },
  { v: '2m', l: '2 Min' },
  { v: '3m', l: '3 Min' },
  { v: '5m', l: '5 Min' },
  { v: '10m', l: '10 Min' },
  { v: '15m', l: '15 Min' },
  { v: '30m', l: '30 Min' },
  { v: '1h', l: '1 Std' },
  { v: '2h', l: '2 Std' },
  { v: '4h', l: '4 Std' },
  { v: '6h', l: '6 Std' },
  { v: '8h', l: '8 Std' },
  { v: '12h', l: '12 Std' },
  { v: '24h', l: '24 Std' },
  { v: '3d', l: '3 Tage' },
  { v: '1w', l: '1 Woche' },
  { v: '1M', l: '1 Monat' },
];

// Pro-Regel Timeframe-Override (Custom-Strategien) – muss zum Backend
// (services/timeframes.py RULE_TIMEFRAMES) passen.
export const RULE_TIMEFRAMES = [
  { v: '1m', l: '1 Min' },
  { v: '3m', l: '3 Min' },
  { v: '5m', l: '5 Min' },
  { v: '15m', l: '15 Min' },
  { v: '30m', l: '30 Min' },
  { v: '1h', l: '1 Std' },
  { v: '2h', l: '2 Std' },
  { v: '4h', l: '4 Std' },
  { v: '8h', l: '8 Std' },
  { v: '12h', l: '12 Std' },
  { v: '1d', l: '1 Tag' },
];

export const TF_MINUTES = {
  '1m': 1, '2m': 2, '3m': 3, '5m': 5, '10m': 10, '15m': 15, '30m': 30,
  '1h': 60, '2h': 120, '4h': 240, '6h': 360, '8h': 480, '12h': 720,
  '24h': 1440, '1d': 1440, '3d': 4320, '1w': 10080, '1M': 43200,
};

export default TIMEFRAMES;
