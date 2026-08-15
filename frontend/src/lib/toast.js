// Zentraler Toast-Wrapper (sonner). Fehler & Warnungen poppen NICHT mehr auf:
// sie landen ausschließlich in der Benachrichtigungsglocke (app_notifications).
// Erfolgs-/Info-Toasts (direktes Aktions-Feedback) bleiben unverändert.
import { toast as base } from 'sonner';

const API_URL = process.env.REACT_APP_BACKEND_URL;
const DEDUPE_MS = 5 * 60 * 1000;
const seen = new Map();

const isDuplicate = (key) => {
  const now = Date.now();
  for (const [k, t] of seen) { if (now - t > DEDUPE_MS) seen.delete(k); }
  const dup = now - (seen.get(key) || 0) < DEDUPE_MS;
  seen.set(key, now);
  return dup;
};

const pushToBell = (title, message, kind) => {
  try {
    fetch(`${API_URL}/api/notifications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, message: String(message), kind }),
    }).catch(() => {});
  } catch (e) { /* still */ }
};

const toast = (...args) => base(...args);
Object.assign(toast, base);

toast.error = (message) => {
  const text = typeof message === 'string' ? message : String(message);
  if (!isDuplicate(`err:${text}`)) pushToBell('Fehler', text, 'error');
  return undefined;
};

toast.warning = (message) => {
  const text = typeof message === 'string' ? message : String(message);
  if (!isDuplicate(`warn:${text}`)) pushToBell('Warnung', text, 'warning');
  return undefined;
};

export { toast };
