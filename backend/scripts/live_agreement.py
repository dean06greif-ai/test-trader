"""Baseline-Messung: Wie gut stimmt die LIVE-Erkennung (kausal) mit den
pivot-korrigierten FINAL-Phasen überein? Pro Bar, Richtung (3er-Achse).

Aufruf: python3 scripts/live_agreement.py <analysis_id>
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


def to_dir(rid, mode):
    if rid is None:
        return None
    if mode == 3:
        return rid
    if mode == 5:
        return 0 if rid <= 1 else (1 if rid == 2 else 2)
    return rid // 3


def expand(segments, hours_ts):
    out = {}
    for s in segments:
        for t in hours_ts:
            if s["from_ts"] <= t <= s["to_ts"]:
                out[t] = s["regime"]
    return out


async def main():
    aid = sys.argv[1]
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ.get("DB_NAME", "trading_bot")]
    doc = await db.regime_analyses.find_one({"id": aid})
    model = (doc.get("combined") or {}).get("model") or {}
    mode = model.get("regime_mode") or (model.get("config") or {}).get("regime_mode", 9)
    for sym, entry in ((doc.get("combined") or {}).get("per_symbol") or {}).items():
        fin = entry.get("segments") or []
        live = entry.get("live_segments") or []
        if not fin or not live:
            continue
        t0 = min(s["from_ts"] for s in fin)
        t1 = max(s["to_ts"] for s in fin)
        hours = list(range(t0, t1 + 1, 3600000))
        f = expand(fin, hours)
        lv = expand(live, hours)
        train_end = (doc.get("bounds") or {}).get(sym, {}).get("train_end_ts")
        both = [(t, f[t], lv[t]) for t in hours if t in f and t in lv]
        same = sum(1 for _, a, b in both if to_dir(a, mode) == to_dir(b, mode))
        print(f"{sym}: bars={len(both)} live-vs-final Richtung: "
              f"{same / max(len(both), 1) * 100:.1f}%")
        if train_end:
            ho = [(t, a, b) for t, a, b in both if t > train_end]
            same_h = sum(1 for _, a, b in ho if to_dir(a, mode) == to_dir(b, mode))
            print(f"  Holdout: bars={len(ho)} Richtung: "
                  f"{same_h / max(len(ho), 1) * 100:.1f}%")


asyncio.run(main())
