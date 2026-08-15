import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv('/app/backend/.env')

async def main():
    cli = AsyncIOMotorClient(os.environ['PROD_MONGO_URL'], serverSelectionTimeoutMS=15000)
    db = cli[os.environ['PROD_DB_NAME']]
    stats = await db.command('dbstats')
    print(f"DB: dataSize={stats['dataSize']/1e6:.1f}MB storage={stats['storageSize']/1e6:.1f}MB index={stats['indexSize']/1e6:.1f}MB collections={stats['collections']}")
    t = db.auto_trades
    total = await t.count_documents({})
    closed = await t.count_documents({"status": "closed"})
    paper = await t.count_documents({"paper": True})
    ai = await t.count_documents({"strategy_id": "ai_engine"})
    ai_closed = await t.count_documents({"strategy_id": "ai_engine", "status": "closed"})
    first = await t.find_one({}, sort=[("opened_at", 1)])
    last = await t.find_one({}, sort=[("opened_at", -1)])
    print(f"auto_trades: total={total} closed={closed} paper={paper} | KI={ai} (closed={ai_closed})")
    print(f"  Zeitraum: {first and first.get('opened_at')} bis {last and last.get('opened_at')}")
    sample = await t.find_one({"status": "closed"}, sort=[("closed_at", -1)])
    if sample:
        print("  Trade-Felder:", sorted(sample.keys()))
    dec = await db.ai_decisions.count_documents({})
    dec_out = await db.ai_decisions.count_documents({"outcome": {"$in": ["win", "loss"]}})
    dec_act = await db.ai_decisions.count_documents({"action": {"$in": ["BUY", "SELL"]}})
    print(f"ai_decisions: total={dec} mit_outcome={dec_out} BUY/SELL={dec_act}")
    sig = await db.signals.count_documents({})
    sig_res = await db.signals.count_documents({"result": {"$in": ["win", "loss"]}})
    print(f"signals: total={sig} mit_result={sig_res}")
    for c in ["ai_market_snapshots", "ai_rewards", "ai_ghost_trades", "ai_token_usage", "backtests", "optimizer_runs"]:
        print(f"{c}: {await db[c].count_documents({})}")
    snap_old = await db.ai_market_snapshots.find_one({}, sort=[("ts", 1)])
    print("aeltester Snapshot:", snap_old and snap_old.get("ts"))

    # Strategie-Verteilung der geschlossenen Trades
    pipe = [{"$match": {"status": "closed"}}, {"$group": {"_id": "$strategy_id", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}]
    async for row in t.aggregate(pipe):
        print("  strat:", row["_id"], row["n"])

asyncio.run(main())
