"""Spaltenbasierter Kerzen-Container.

Grund: Eine Liste von Dicts braucht ~450 Byte pro Kerze. 5400 Tage 1m-Kerzen
(7,8 Mio.) = >3 GB RAM -> der lokale Worker läuft in den Swap/OOM und die
Verbindung bricht ab. Als numpy-Spalten sind dieselben Daten 48 Byte/Kerze
(~370 MB), das Laden von Platte ist ~50x schneller und das Verschicken an
Kind-Prozesse (Windows/spawn) ist kompakt statt Millionen Dict-Pickles.

CandleArray verhält sich wie eine `List[Dict]`:
    len(ca), ca[i] -> dict, ca[a:b] -> CandleArray, for c in ca -> dict
Dadurch funktioniert der gesamte bestehende Strategie-/Simulations-Code
unverändert weiter; die heißen Pfade greifen zusätzlich direkt auf die Arrays zu.
"""
from typing import Dict, Iterable, List, Union

import numpy as np

FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


class CandleArray:
    __slots__ = ("ts", "op", "hi", "lo", "cl", "vol")

    def __init__(self, ts, op, hi, lo, cl, vol):
        self.ts = np.asarray(ts, dtype=np.int64)
        self.op = np.asarray(op, dtype=np.float64)
        self.hi = np.asarray(hi, dtype=np.float64)
        self.lo = np.asarray(lo, dtype=np.float64)
        self.cl = np.asarray(cl, dtype=np.float64)
        self.vol = np.asarray(vol, dtype=np.float64)

    # ---- Sequence-Protokoll (kompatibel zu List[Dict]) ----
    def __len__(self) -> int:
        return int(self.ts.shape[0])

    def __bool__(self) -> bool:
        return self.ts.shape[0] > 0

    def _row(self, i: int) -> Dict:
        return {"timestamp": int(self.ts[i]), "open": float(self.op[i]),
                "high": float(self.hi[i]), "low": float(self.lo[i]),
                "close": float(self.cl[i]), "volume": float(self.vol[i])}

    def __getitem__(self, key):
        if isinstance(key, slice):
            return CandleArray(self.ts[key], self.op[key], self.hi[key],
                               self.lo[key], self.cl[key], self.vol[key])
        return self._row(int(key))

    def __iter__(self):
        for i in range(len(self)):
            yield self._row(i)

    def __repr__(self):
        n = len(self)
        return f"<CandleArray n={n}>"

    # ---- Pickle (numpy-Arrays statt Millionen Dicts) ----
    def __getstate__(self):
        return (np.ascontiguousarray(self.ts), np.ascontiguousarray(self.op),
                np.ascontiguousarray(self.hi), np.ascontiguousarray(self.lo),
                np.ascontiguousarray(self.cl), np.ascontiguousarray(self.vol))

    def __setstate__(self, state):
        self.ts, self.op, self.hi, self.lo, self.cl, self.vol = state

    # ---- Konstruktion / Konvertierung ----
    @staticmethod
    def empty() -> "CandleArray":
        z = np.zeros(0)
        return CandleArray(np.zeros(0, dtype=np.int64), z, z, z, z, z)

    @staticmethod
    def from_dicts(rows: Iterable[Dict]) -> "CandleArray":
        rows = list(rows)
        if not rows:
            return CandleArray.empty()
        m = np.array([[r["timestamp"], r["open"], r["high"], r["low"],
                       r["close"], r.get("volume") or 0.0] for r in rows], dtype=np.float64)
        return CandleArray.from_matrix(m)

    @staticmethod
    def from_matrix(m: np.ndarray) -> "CandleArray":
        m = np.asarray(m, dtype=np.float64)
        if m.size == 0:
            return CandleArray.empty()
        return CandleArray(m[:, 0].astype(np.int64), m[:, 1], m[:, 2],
                           m[:, 3], m[:, 4], m[:, 5])

    def matrix(self) -> np.ndarray:
        return np.column_stack([self.ts.astype(np.float64), self.op, self.hi,
                                self.lo, self.cl, self.vol])

    def to_list(self) -> List[Dict]:
        return [self._row(i) for i in range(len(self))]

    @staticmethod
    def concat(parts: Iterable["CandleArray"]) -> "CandleArray":
        parts = [p for p in parts if p is not None and len(p)]
        if not parts:
            return CandleArray.empty()
        if len(parts) == 1:
            return parts[0]
        return CandleArray(np.concatenate([p.ts for p in parts]),
                           np.concatenate([p.op for p in parts]),
                           np.concatenate([p.hi for p in parts]),
                           np.concatenate([p.lo for p in parts]),
                           np.concatenate([p.cl for p in parts]),
                           np.concatenate([p.vol for p in parts]))

    # ---- Zeitbereiche (ohne Kopie der Daten) ----
    def slice_from_ts(self, start_ms: int) -> "CandleArray":
        i = int(np.searchsorted(self.ts, np.int64(start_ms), side="left"))
        return self if i <= 0 else self[i:]

    def slice_range(self, start_ms=None, end_ms=None) -> "CandleArray":
        a = 0 if not start_ms else int(np.searchsorted(self.ts, np.int64(start_ms), "left"))
        b = len(self) if not end_ms else int(np.searchsorted(self.ts, np.int64(end_ms), "right"))
        return self[a:b]

    def dedup_sorted(self) -> "CandleArray":
        """Nach Zeitstempel sortieren und Duplikate entfernen (letzter gewinnt)."""
        if len(self) < 2:
            return self
        order = np.argsort(self.ts, kind="stable")
        ca = CandleArray(self.ts[order], self.op[order], self.hi[order],
                         self.lo[order], self.cl[order], self.vol[order])
        keep = np.ones(len(ca), dtype=bool)
        keep[:-1] = ca.ts[:-1] != ca.ts[1:]
        if keep.all():
            return ca
        return CandleArray(ca.ts[keep], ca.op[keep], ca.hi[keep],
                           ca.lo[keep], ca.cl[keep], ca.vol[keep])

    def nbytes(self) -> int:
        return int(self.ts.nbytes + self.op.nbytes + self.hi.nbytes +
                   self.lo.nbytes + self.cl.nbytes + self.vol.nbytes)


Candles = Union[CandleArray, List[Dict]]


def as_array(candles: Candles) -> CandleArray:
    return candles if isinstance(candles, CandleArray) else CandleArray.from_dicts(candles)


def as_list(candles: Candles) -> List[Dict]:
    return candles.to_list() if isinstance(candles, CandleArray) else candles
