"""Read-only Analyse der Regime-Qualität in Prod (Fix 0.7 Vorarbeit).

Prüft:
1. Regime-Verteilung der ai_market_snapshots (gesamt + je Asset-Klasse)
2. Regime-Verteilung an LONG/SHORT-Decisions (entry_market_snapshot)
3. Coverage: Wie viele Decisions/Trades haben überhaupt einen Marktzustand?
4. Regime an ai_rewards + Outcome je Regime (lernt die KI an sinnvollen Labels?)
"""
import asyncio
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient

CRYPTO = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
          "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "POLUSDT", "LINKUSDT", "LTCUSDT"}


def klass(sym):
    s = str(sym or "")
    if s in CRYPTO or s.endswith("USDT") and s[:3] not in ("XAU", "XAG"):
        return "krypto"
    if s in ("GOLD", "SILVER", "OIL", "XAUUSDT", "XAGUSDT") or "OIL" in s:
        return "rohstoff"
    if s in ("QQQUSDT", "SPXUSDT", "DAXUSDT", "NDX", "SPX"):
        return "index"
    return "forex/sonst"


async def main():
    c = AsyncIOMotorClient(os.environ["PROD_MONGO_URL"])
    db = c[os.environ["PROD_DB_NAME"]]
    cut14 = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    print("=== 1) Snapshots (14d): Regime-Verteilung gesamt + je Asset-Klasse ===")
    snaps = await db.ai_market_snapshots.find(
        {"ts": {"$gte": cut14}}, {"symbol": 1, "features.regime": 1,
                                  "features.volatility_pct": 1, "_id": 0}).to_list(60000)
    print(f"Snapshots 14d: {len(snaps)}")
    total = Counter((s.get("features") or {}).get("regime") or "FEHLT" for s in snaps)
    for k, v in total.most_common():
        print(f"  {k}: {v} ({v / max(1, len(snaps)) * 100:.1f}%)")
    by_cls = defaultdict(Counter)
    vols = defaultdict(list)
    for s in snaps:
        f = s.get("features") or {}
        kl = klass(s.get("symbol"))
        by_cls[kl][str(f.get("regime") or "FEHLT")] += 1
        if f.get("volatility_pct") is not None:
            vols[kl].append(float(f["volatility_pct"]))
    for kl, cnt in by_cls.items():
        n = sum(cnt.values())
        top = ", ".join(f"{k} {v * 100 // n}%" for k, v in cnt.most_common(4))
        vv = sorted(vols[kl])
        med = vv[len(vv) // 2] if vv else 0
        print(f"  [{kl}] n={n} median_vol={med:.3f}% -> {top}")

    print("\n=== 2) LONG/SHORT-Decisions (14d): Regime am Entry ===")
    decs = await db.ai_decisions.find(
        {"ts": {"$gte": cut14}, "action": {"$in": ["LONG", "SHORT"]}},
        {"symbol": 1, "entry_market_snapshot.features.regime": 1, "outcome": 1,
         "gate_shadow.p_win": 1, "_id": 0}).to_list(20000)
    print(f"LONG/SHORT-Decisions 14d: {len(decs)}")
    have = sum(1 for d in decs if (d.get("entry_market_snapshot") or {}).get("features"))
    print(f"  mit entry_market_snapshot: {have} ({have / max(1, len(decs)) * 100:.0f}%)")
    reg_cnt = Counter(((d.get("entry_market_snapshot") or {}).get("features") or {})
                      .get("regime") or "FEHLT" for d in decs)
    for k, v in reg_cnt.most_common(8):
        print(f"  {k}: {v}")
    gs = sum(1 for d in decs if d.get("gate_shadow"))
    print(f"  mit gate_shadow: {gs}")

    print("\n=== 3) Trades (alle): entry_market_snapshot-Coverage ===")
    tr = await db.auto_trades.find(
        {}, {"strategy_id": 1, "entry_market_snapshot.features.regime": 1,
             "data_collection": 1, "status": 1, "result": 1, "realized_pnl": 1,
             "fees_paid": 1, "_id": 0}).to_list(5000)
    ai_tr = [t for t in tr if t.get("strategy_id") == "ai_trader"]
    have_t = sum(1 for t in ai_tr if (t.get("entry_market_snapshot") or {}).get("features"))
    print(f"auto_trades gesamt: {len(tr)} | ai_trader: {len(ai_tr)} | "
          f"davon mit Entry-Snapshot: {have_t}")

    print("\n=== 4) ai_rewards: Regime + Ergebnis je Regime ===")
    rws = await db.ai_rewards.find({}, {"regime": 1, "result": 1, "pnl": 1,
                                        "fee_share_pct": 1, "_id": 0}).to_list(3000)
    print(f"ai_rewards: {len(rws)}")
    agg = defaultdict(lambda: {"n": 0, "win": 0, "pnl": 0.0})
    for r in rws:
        k = str(r.get("regime") or "FEHLT")
        agg[k]["n"] += 1
        agg[k]["win"] += 1 if r.get("result") == "win" else 0
        agg[k]["pnl"] += float(r.get("pnl") or 0)
    for k, d in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {k}: n={d['n']} winrate={d['win'] / d['n'] * 100:.0f}% pnl={d['pnl']:+.2f}")

    print("\n=== 5) Closed-Losses: Fee-Anteil (Kontext Fee-Wächter) ===")
    losses = [t for t in ai_tr if t.get("status") == "closed" and t.get("result") == "loss"
              and float(t.get("realized_pnl") or 0) < 0 and float(t.get("fees_paid") or 0) > 0]
    if losses:
        shares = [min(100.0, float(t["fees_paid"]) / abs(float(t["realized_pnl"])) * 100)
                  for t in losses]
        dom = sum(1 for s in shares if s >= 50)
        print(f"KI-Verlust-Trades: {len(losses)} | Ø Fee-Anteil {sum(shares) / len(shares):.0f}% "
              f"| Fees>=50% bei {dom} ({dom / len(losses) * 100:.0f}%)")


asyncio.run(main())
