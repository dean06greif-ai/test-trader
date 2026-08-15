"""Migration Fix 0.5: Ergebnis-Wahrheit vereinheitlichen.

Kanonische Wahrheit = Vorzeichen von auto_trades.realized_pnl (inkl. Fees),
gespeichert als auto_trades.result beim Close. Diese Migration zieht die
historischen Daten nach:

1. signals: Signale mit geschlossenem Trade bekommen result = Trade-Ergebnis,
   result_source="trade_pnl", trade_id. Bereits gelabelte Signale OHNE Trade
   bekommen result_source="tp1_touch" (Backfill, Wert unverändert).
2. ai_decisions: Decisions mit signal_id, deren Signal einen geschlossenen
   Trade hat, bekommen outcome = Trade-Ergebnis, outcome_source="trade_pnl",
   trade_pnl. Sonstige Decisions mit outcome bekommen outcome_source="tp1_touch".

SICHERHEIT:
- Default ist DRY-RUN (nur lesen + Report). Schreiben NUR mit --apply.
- --apply verweigert HART, wenn die Ziel-URI die Produktions-DB
  (PROD_MONGO_URL) ist. Prod-Migration laeuft spaeter auf Render selbst
  (dort ist MONGO_URL die Prod-DB und PROD_MONGO_URL nicht gesetzt).

Aufrufe:
  python scripts/migrate_0_5_result_truth.py --prod            # Dry-Run gegen Prod (nur lesen)
  python scripts/migrate_0_5_result_truth.py                   # Dry-Run gegen lokale Dev-DB
  python scripts/migrate_0_5_result_truth.py --apply           # Anwenden auf lokale Dev-DB
"""
import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from motor.motor_asyncio import AsyncIOMotorClient

RESULTS = ("win", "loss", "breakeven")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run(mongo_url: str, db_name: str, apply: bool) -> None:
    prod_url = os.environ.get("PROD_MONGO_URL") or ""
    if apply and prod_url and mongo_url == prod_url:
        raise SystemExit("ABBRUCH: --apply gegen PROD_MONGO_URL ist verboten "
                         "(Prod ist aus der Dev-Umgebung NUR LESEND). "
                         "Prod-Migration auf Render ausfuehren.")
    db = AsyncIOMotorClient(mongo_url)[db_name]
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"== Migration 0.5 [{mode}] auf DB '{db_name}' ==")

    # ---- 1) Trades -> Signale (kanonisch) --------------------------------
    trades = await db.auto_trades.find(
        {"status": "closed", "result": {"$in": list(RESULTS)},
         "signal_id": {"$nin": [None, ""]}},
        {"id": 1, "signal_id": 1, "result": 1, "realized_pnl": 1,
         "closed_at": 1}).to_list(length=None)
    by_signal = {}
    for t in trades:  # bei mehreren Trades pro Signal gewinnt der letzte Close
        prev = by_signal.get(t["signal_id"])
        if not prev or str(t.get("closed_at") or "") > str(prev.get("closed_at") or ""):
            by_signal[t["signal_id"]] = t

    sig_changes = Counter()
    sig_flips = Counter()
    for sid, t in by_signal.items():
        s = await db.signals.find_one({"id": sid}, {"result": 1, "result_source": 1})
        if not s:
            sig_changes["signal_fehlt"] += 1
            continue
        old = s.get("result")
        if s.get("result_source") == "trade_pnl" and old == t["result"]:
            sig_changes["schon_kanonisch"] += 1
            continue
        if old != t["result"]:
            sig_flips[f"{old or 'unlabeled'} -> {t['result']}"] += 1
        sig_changes["umgestellt"] += 1
        if apply:
            await db.signals.update_one(
                {"id": sid},
                {"$set": {"result": t["result"], "status": "closed",
                          "result_source": "trade_pnl", "trade_id": t.get("id"),
                          "result_ts": t.get("closed_at") or _now_iso()}})

    # tp1_touch-Backfill: gelabelte Signale ohne Trade-Label
    tp1_filter = {"result": {"$in": list(RESULTS)},
                  "result_source": {"$exists": False},
                  "id": {"$nin": list(by_signal.keys())}}
    tp1_n = await db.signals.count_documents(tp1_filter)
    if apply and tp1_n:
        await db.signals.update_many(
            tp1_filter, {"$set": {"result_source": "tp1_touch"}})

    # ---- 2) Trades -> ai_decisions (kanonisch) ---------------------------
    dec_changes = Counter()
    dec_flips = Counter()
    for sid, t in by_signal.items():
        async for d in db.ai_decisions.find(
                {"signal_id": sid}, {"outcome": 1, "outcome_source": 1}):
            old = d.get("outcome")
            if d.get("outcome_source") == "trade_pnl" and old == t["result"]:
                dec_changes["schon_kanonisch"] += 1
                continue
            if old != t["result"]:
                dec_flips[f"{old or 'unlabeled'} -> {t['result']}"] += 1
            dec_changes["umgestellt"] += 1
            if apply:
                await db.ai_decisions.update_one(
                    {"_id": d["_id"]},
                    {"$set": {"outcome": t["result"],
                              "outcome_source": "trade_pnl",
                              "trade_pnl": t.get("realized_pnl"),
                              "outcome_ts": _now_iso()}})

    # tp1_touch-Backfill fuer restliche gelabelte Decisions
    dec_tp1_filter = {"outcome": {"$in": list(RESULTS)},
                      "outcome_source": {"$exists": False},
                      "signal_id": {"$nin": list(by_signal.keys())}}
    dec_tp1_n = await db.ai_decisions.count_documents(dec_tp1_filter)
    if apply and dec_tp1_n:
        await db.ai_decisions.update_many(
            dec_tp1_filter, {"$set": {"outcome_source": "tp1_touch"}})

    # ---- Report -----------------------------------------------------------
    print(f"Geschlossene Trades mit signal_id: {len(trades)} "
          f"(eindeutige Signale: {len(by_signal)})")
    print(f"signals  : {dict(sig_changes)} | tp1_touch-Backfill: {tp1_n}")
    if sig_flips:
        print("  Label-Flips signals:")
        for k, v in sorted(sig_flips.items()):
            print(f"    {k}: {v}")
    print(f"decisions: {dict(dec_changes)} | tp1_touch-Backfill: {dec_tp1_n}")
    if dec_flips:
        print("  Label-Flips ai_decisions:")
        for k, v in sorted(dec_flips.items()):
            print(f"    {k}: {v}")
    print(f"[{mode}] fertig." + ("" if apply else " Nichts geschrieben."))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Aenderungen schreiben (Default: Dry-Run)")
    ap.add_argument("--prod", action="store_true",
                    help="Gegen PROD_MONGO_URL lesen (nur Dry-Run erlaubt)")
    args = ap.parse_args()
    if args.prod:
        url = os.environ.get("PROD_MONGO_URL")
        name = os.environ.get("PROD_DB_NAME", "crypto_scanner")
        if not url:
            raise SystemExit("PROD_MONGO_URL nicht gesetzt")
        if args.apply:
            raise SystemExit("ABBRUCH: --prod zusammen mit --apply ist verboten.")
    else:
        url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        name = os.environ.get("DB_NAME", "crypto_scanner")
    asyncio.run(run(url, name, apply=args.apply))
