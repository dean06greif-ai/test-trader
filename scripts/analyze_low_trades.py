"""Read-only Analyse: Warum macht der KI-Trader so wenige Trades? (Prod-DB, NUR LESEND)"""
import os, asyncio, collections
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv('/app/backend/.env')

async def main():
    cli = AsyncIOMotorClient(os.environ['PROD_MONGO_URL'])
    db = cli[os.environ['PROD_DB_NAME']]
    now = datetime.now(timezone.utc)
    since14 = (now - timedelta(days=14)).isoformat()
    since7 = (now - timedelta(days=7)).isoformat()

    # Config des KI-Traders in Prod
    cfg = await db.settings.find_one({'_id': 'ai_trader'}) or await db.settings.find_one({'key': 'ai_trader'})
    if not cfg:
        async for s in db.settings.find({}):
            sid = str(s.get('_id', ''))
            if 'ai' in sid.lower():
                print('settings doc:', sid, {k: v for k, v in s.items() if k in ('enabled','interval_min','min_confidence','cooldown_min','max_trades_per_coin','schedule','smart_skip','group_analysis','provider','model')})
    else:
        print('AI config:', {k: cfg.get(k) for k in ('enabled','interval_min','min_confidence','cooldown_min','max_trades_per_coin','schedule','smart_skip')})

    # MasterPrompt rules
    mp = await db.settings.find_one({'_id': 'ai_master_prompt'})
    if mp:
        print('MasterPrompt rules:', mp.get('rules'))

    for label, since in (('14d', since14), ('7d', since7)):
        q = {'ts': {'$gte': since}}
        total = await db.ai_decisions.count_documents(q)
        acts = collections.Counter()
        conf_buckets = collections.Counter()
        signaled = 0
        blocked = collections.Counter()
        pass_conf_not_signaled = 0
        min_conf = (cfg or {}).get('min_confidence', 65)
        async for d in db.ai_decisions.find(q, {'action':1,'confidence':1,'signaled':1,'blocked_by':1}):
            a = d.get('action','?'); acts[a]+=1
            c = int(d.get('confidence') or 0)
            conf_buckets[f"{(c//10)*10}-{(c//10)*10+9}"]+=1
            if d.get('signaled'): signaled+=1
            elif a in ('LONG','SHORT') and c >= min_conf:
                pass_conf_not_signaled += 1
                if d.get('blocked_by'): blocked[str(d.get('blocked_by'))[:60]]+=1
        print(f"\n== {label}: decisions={total} actions={dict(acts)} signaled={signaled}")
        print(f"   LONG/SHORT >= min_conf({min_conf}) aber NICHT signaled: {pass_conf_not_signaled}")
        print('   conf-Verteilung:', dict(sorted(conf_buckets.items())))
        if blocked: print('   blocked_by:', dict(blocked.most_common(8)))

    # Governance-Blocks aus ai_chat
    gov = collections.Counter()
    async for c in db.ai_chat.find({'role':'governance','ts':{'$gte':since14}}, {'text':1}):
        t = c.get('text','')
        gov[t.split('–')[-1].strip()[:70]] += 1
    print('\nGovernance-Blocks 14d:', dict(gov.most_common(12)))

    # Smart-Skips 14d
    sk = await db.ai_chat.count_documents({'role':'analysis','ts':{'$gte':since14},'skipped_groups':{'$ne':None}})
    an = await db.ai_chat.count_documents({'role':'analysis','ts':{'$gte':since14}})
    print(f"Analyse-Läufe 14d: {an}, davon mit skipped_groups: {sk}")

    # Trades: KI vs. andere Strategien, 14d
    tq = {'opened_at': {'$gte': since14}}
    tot = await db.auto_trades.count_documents(tq)
    ai = await db.auto_trades.count_documents({**tq, 'strategy_id': 'ai_trader'})
    paper = await db.auto_trades.count_documents({**tq, 'mode': 'paper'})
    print(f"\nTrades 14d: total={tot}, ai_trader={ai}, paper={paper}")
    per_day = collections.Counter()
    async for t in db.auto_trades.find(tq, {'opened_at':1,'strategy_id':1,'mode':1}):
        per_day[str(t.get('opened_at',''))[:10]] += 1
    print('Trades/Tag:', dict(sorted(per_day.items())))

    # Signale gesamt 14d (alle Strategien)
    sq = await db.signals.count_documents({'ts': {'$gte': since14}})
    print(f"Signale 14d (alle Strategien): {sq}")
    cli.close()

asyncio.run(main())
