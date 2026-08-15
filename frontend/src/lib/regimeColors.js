/**
 * Farbschema der Regime – abhängig vom Regime-Modus (3 / 5 / 9).
 *
 * Prinzip: Rot = abwärts, Gelb = seitwärts, Grün = aufwärts.
 * Die Abstufungen innerhalb einer Richtung sind bewusst DEUTLICH getrennt
 * (Helligkeit + Sättigung + Farbwinkel), damit man "stark" und "leicht"
 * bzw. die Volatilitätsstufen auch im Chart sofort unterscheiden kann.
 */
export const REGIME_FALLBACK_COLORS = ['#30D158', '#FF453A', '#FFD60A', '#64D2FF',
  '#BF5AF2', '#FF9F0A', '#5E5CE6', '#FF6482', '#66D4CF', '#A2845E'];

// 3 Regime: id 0 = ab, 1 = seitwärts, 2 = auf
const C3 = ['#E02B2B', '#F5C518', '#1FB855'];

// 5 Regime: 0 stark ab · 1 leicht ab · 2 seitwärts · 3 leicht auf · 4 stark auf
// (Wein-Dunkelrot -> klares Rot -> Gelb -> klares Grün -> Tannen-Dunkelgrün)
const C5 = ['#6E0A2A', '#FF4D4D', '#F5C518', '#3FD67A', '#0A5C2E'];

// 9 Regime: [niedrige Vola, mittlere Vola, hohe Vola] je Richtung.
// Die Stufen wechseln nicht nur die Helligkeit, sondern auch den Farbwinkel
// (Vola niedrig = kühler/dunkler, hoch = greller/wärmer) -> klar trennbar.
const C9 = {
  down: ['#6E0F3C', '#D6202B', '#FF6A3D'],
  side: ['#8A7A16', '#F5C518', '#FFF07A'],
  up: ['#0A5C4A', '#1FB855', '#8CF07A'],
};

const TREND_KEYS = ['down', 'side', 'up'];

/** Regime-Modus (3/5/9) aus Modell/Regime-Liste ableiten. */
export function regimeModeOf(model, regimes) {
  const m = model?.regime_mode || model?.config?.regime_mode;
  if (m === 3 || m === 5 || m === 9) return m;
  const list = regimes || model?.regimes || [];
  if (list.some(r => r.vol)) return 9;
  const maxId = list.reduce((a, r) => Math.max(a, r.id ?? 0), 0);
  if (list.length && maxId <= 2) return 3;
  if (list.length && maxId <= 4) return 5;
  return 9;
}

/** Farbe für eine Regime-ID bei bekanntem Modus. */
export function regimeColorByMode(id, mode) {
  const i = Math.max(id ?? 0, 0);
  if (mode === 3) return C3[Math.min(i, 2)];
  if (mode === 5) return C5[Math.min(i, 4)];
  const t = TREND_KEYS[Math.floor(i / 3)] || 'side';
  return C9[t][Math.min(i % 3, 2)];
}

/** Farbe für eine Regime-ID der Engine v2 (9er-Taxonomie). */
export function v2RegimeColor(id) {
  return regimeColorByMode(id, 9);
}

/**
 * Farbe für ein Regime. `regimes` ist die Regime-Liste des Modells;
 * `model` optional (liefert den Modus zuverlässig).
 */
export function regimeColor(id, regimes, model) {
  const list = regimes || [];
  const r = list.find(x => x.id === id);
  const isV2 = !!(r && (r.trend || r.nnfx || r.stats?.score !== undefined))
    || !!(model?.regime_mode || model?.config?.regime_mode);
  if (!isV2) return REGIME_FALLBACK_COLORS[id % REGIME_FALLBACK_COLORS.length];
  return regimeColorByMode(id, regimeModeOf(model, list));
}

/** Deckkraft der Chart-Bänder – kräftiger bei stärkerem/volatilerem Regime. */
export function regimeOpacity(id, regimes, model) {
  const list = regimes || [];
  const mode = regimeModeOf(model, list);
  if (mode === 3) return 0.20;
  if (mode === 5) return [0.34, 0.22, 0.20, 0.22, 0.34][Math.min(id ?? 0, 4)];
  const r = list.find(x => x.id === id);
  const v = r?.vol ? ['low', 'mid', 'high'].indexOf(r.vol) : ((id ?? 0) % 3);
  return [0.14, 0.20, 0.28][v < 0 ? 1 : v];
}
