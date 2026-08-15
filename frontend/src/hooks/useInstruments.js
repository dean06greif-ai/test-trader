import { useEffect, useState } from 'react';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const EMPTY = { symbols: [], groups: [], tradable: [], crypto: [], bySymbol: {} };

let cache = null;
let inflight = null;
const listeners = new Set();

const shape = (d) => {
  const groups = d.groups || [];
  const bySymbol = {};
  groups.forEach(g => (g.symbols || []).forEach(s => {
    bySymbol[s.symbol] = { ...s, group: g.name };
  }));
  return {
    symbols: d.coins || [],
    crypto: d.crypto || [],
    tradable: d.tradable || [],
    groups,
    bySymbol,
  };
};

const load = () => {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = fetch(`${API_URL}/api/coins`)
      .then(r => r.json())
      .then(d => {
        cache = shape(d);
        listeners.forEach(fn => fn(cache));
        return cache;
      })
      .catch(() => EMPTY)
      .finally(() => { inflight = null; });
  }
  return inflight;
};

/** Anzeigename eines Symbols (USDT-Suffix wird abgeschnitten). */
export const assetLabel = (symbol) => {
  const s = typeof symbol === 'string' ? symbol : String(symbol ?? '?');
  return s.endsWith('USDT') ? s.slice(0, -4) : s;
};

/**
 * Asset-Universum aus /api/coins (einmal geladen, prozessweit geteilt).
 * Einzige Quelle für Symbol-Listen im Frontend – Backend-Pflege in
 * backend/core/instruments.py.
 */
export default function useInstruments() {
  const [data, setData] = useState(cache || EMPTY);

  useEffect(() => {
    let alive = true;
    const onUpdate = (d) => { if (alive) setData(d); };
    listeners.add(onUpdate);
    load().then(onUpdate);
    return () => { alive = false; listeners.delete(onUpdate); };
  }, []);

  return data;
}
