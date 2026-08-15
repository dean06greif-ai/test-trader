"""Offline-Prüfung: passen die Regime-Labels einer gespeicherten Analyse zum
tatsächlichen Kursverlauf? Nutzt die Chart-Punkte im Dokument (downsampled).

Aufruf: python3 scripts/check_labels.py <analysis_id> [live]
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


def seg_stats(points, segments, mode, label_of):
    """Je Segment: Netto-Änderung in % und %/Tag aus den Chart-Punkten."""
    rows = []
    for s in segments:
        pts = [p for p in points if s["from_ts"] <= p[0] <= s["to_ts"]]
        if len(pts) < 2:
            continue
        net = (pts[-1][1] / pts[0][1] - 1) * 100
        days = (s["to_ts"] - s["from_ts"]) / 86400000
        rows.append({"regime": s["regime"], "label": label_of(s["regime"]),
                     "days": round(days, 1), "net_pct": round(net, 2),
                     "per_day": round(net / max(days, 1e-9), 3)})
    return rows


async def main():
    aid = sys.argv[1]
    use_live = len(sys.argv) > 2 and sys.argv[2] == "live"
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ.get("DB_NAME", "trading_bot")]
    doc = await db.regime_analyses.find_one({"id": aid})
    if not doc:
        print("nicht gefunden")
        return
    model = (doc.get("combined") or {}).get("model") or {}
    mode = model.get("regime_mode") or (model.get("config") or {}).get("regime_mode", 9)
    labels = {r["id"]: r["label"] for r in model.get("regimes") or []}
    print(f"analysis={aid} mode={mode} profile={(model.get('config') or {}).get('adapt_applied')}")
    print(f"min_phase_days={(model.get('config') or {}).get('min_phase_days')}")
    for sym, entry in ((doc.get("combined") or {}).get("per_symbol") or {}).items():
        segs = entry.get("live_segments" if use_live else "segments") or []
        pts = (doc.get("chart") or {}).get(sym) or []
        rows = seg_stats(pts, segs, mode, lambda r: labels.get(r, f"R{r}"))
        print(f"\n=== {sym} ({'LIVE' if use_live else 'FINAL'}) {len(segs)} Segmente ===")
        bad = 0
        for r in rows:
            # Richtungs-Konsistenz: up-Regime sollte netto steigen etc.
            t = r["regime"] if mode == 3 else (
                0 if r["regime"] <= 1 else (1 if r["regime"] == 2 else 2)) if mode == 5 else r["regime"] // 3
            flag = ""
            if t == 2 and r["net_pct"] < -1:
                flag = " <-- LABEL AUF, KURS FIEL"
            if t == 0 and r["net_pct"] > 1:
                flag = " <-- LABEL AB, KURS STIEG"
            if flag:
                bad += 1
            print(f"  {r['label']:<28} {r['days']:>7.1f}d  net {r['net_pct']:>8.2f}%  "
                  f"({r['per_day']:>6.3f}%/d){flag}")
        print(f"  -> {bad}/{len(rows)} widersprüchliche Segmente")
        # 5er-Modus: Stärke-Check – "stark" sollte |%/Tag| größer haben als "leicht"
        if mode == 5:
            import statistics
            for pair, ids in (("abwärts", (0, 1)), ("aufwärts", (4, 3))):
                strong = [abs(r["per_day"]) for r in rows if r["regime"] == ids[0]]
                weak = [abs(r["per_day"]) for r in rows if r["regime"] == ids[1]]
                if strong and weak:
                    print(f"  Stärke-Check {pair}: stark Ø{statistics.mean(strong):.3f}%/d "
                          f"vs leicht Ø{statistics.mean(weak):.3f}%/d "
                          f"{'OK' if statistics.mean(strong) > statistics.mean(weak) else 'VERTAUSCHT?'}")


asyncio.run(main())
