/**
 * Zeit-Formatierung an EINER Stelle – Anzeige immer deutsche Zeit (Europe/Berlin,
 * inkl. automatischer Sommer-/Winterzeit), unabhängig von der Zeitzone des
 * Geräts oder des Servers.
 *
 * Zusätzlich robust gegen Alt-Daten: ein ISO-String OHNE Zeitzonen-Angabe
 * (z.B. "2026-06-01T12:00:00") wird als UTC gelesen – so wie der Server ihn
 * gemeint hat – und nicht als lokale Zeit interpretiert.
 */
export const TZ = 'Europe/Berlin';

const NAIVE_ISO = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

/** ISO/Datum -> Date oder null (naive Strings gelten als UTC). */
export function toDate(value) {
  if (value === null || value === undefined || value === '') return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') return new Date(value);
  const s = String(value);
  const d = new Date(NAIVE_ISO.test(s) ? `${s.replace(' ', 'T')}Z` : s);
  return isNaN(d.getTime()) ? null : d;
}

function fmt(value, opts, fallback) {
  const d = toDate(value);
  if (!d) return fallback;
  return d.toLocaleString('de-DE', { timeZone: TZ, ...opts });
}

/** 01.06.2026, 14:35 */
export const fmtDateTime = (value, fallback = '–') =>
  fmt(value, { day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit' }, fallback);

/** 01.06., 14:35 (kompakt für Tabellen/Listen) */
export const fmtShort = (value, fallback = '–') =>
  fmt(value, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' },
    fallback);

/** 01.06.2026 */
export const fmtDate = (value, fallback = '–') =>
  fmt(value, { day: '2-digit', month: '2-digit', year: 'numeric' }, fallback);

/** 14:35 */
export const fmtTime = (value, fallback = '–') =>
  fmt(value, { hour: '2-digit', minute: '2-digit' }, fallback);

/** 14:35:02 */
export const fmtTimeSec = (value, fallback = '–') =>
  fmt(value, { hour: '2-digit', minute: '2-digit', second: '2-digit' }, fallback);
