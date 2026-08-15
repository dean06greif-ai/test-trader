"""Dev-Hilfsskript: befüllt die lokale DB mit synthetischen Backtest-/Optimizer-
Ergebnissen und abgeschlossenen KI-Entscheidungen, um den Forschungs-Analysten
und das ML-Labor Ende-zu-Ende zu prüfen. Nur für die Entwicklungsumgebung.

Nutzung:  python scripts/seed_ai_lab_demo.py [--clean]
"""
import asyncio
import os
import random
import sys
import uuid
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

TAG = "ai_lab_demo"


async def main(clean: bool):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "crypto_scanner")]
    if clean:
        for coll in ("backtests", "optimizer_runs", "ai_decisions",
                     "ai_market_snapshots", "signals"):
            res = await db[coll].delete_many({"demo_tag": TAG})
            print(f"{coll}: {res.deleted_count} Demo-Dokumente entfernt")
        return

    now = datetime.now(timezone.utc)
    await db.backtests.insert_one({
        "demo_tag": TAG, "id": str(uuid.uuid4()),
        "created_at": (now - timedelta(hours=5)).isoformat(),
        "params": {"symbols": ["BTCUSDT", "ETHUSDT"], "days": 30, "timeframe": "5m"},
        "result": {"days": 30, "per_strategy": [
            {"strategy_id": "ema_pullback", "strategy_name": "EMA Pullback", "pnl": 42.3,
             "pnl_pct": 42.3, "trades": 120, "win_rate": 58.0, "max_drawdown_pct": 7.1,
             "timeframe": "5m"},
            {"strategy_id": "macd_rsi", "strategy_name": "MACD+RSI", "pnl": -8.4,
             "pnl_pct": -8.4, "trades": 44, "win_rate": 38.6, "max_drawdown_pct": 12.4,
             "timeframe": "15m"}],
            "best_per_symbol": {"BTCUSDT": {"strategy_name": "EMA Pullback", "pnl": 30.1,
                                            "win_rate": 60.0}}},
    })
    await db.optimizer_runs.insert_one({
        "demo_tag": TAG, "id": str(uuid.uuid4()),
        "created_at": (now - timedelta(hours=3)).isoformat(),
        "params": {}, "result": {
            "mode": "params", "objective": "pnl", "days": 60, "timeframe": "5m",
            "strategy_name": "EMA Pullback", "symbols": ["BTCUSDT"],
            "top5": [{"metrics": {"pnl": 51.0, "win_rate": 57.0, "trades": 140,
                                  "max_drawdown": 9.0},
                      "test_metrics": {"pnl": 11.2, "win_rate": 52.0},
                      "wf": {"score": 0.62}, "constancy": {"deviation_pct": 18.0},
                      "params": {"ema_fast": 9, "ema_slow": 34, "rsi_period": 14},
                      "trade_params": {"tp1_crv": 1.5, "sl_fixed_percent": 0.9},
                      "passed": True,
                      "rank_reason": "im Holdout bestätigt, moderate Konstanz-Abweichung"}]},
    })

    random.seed(7)
    snaps, decisions = [], []
    for i in range(90):
        ts = (now - timedelta(hours=90 - i)).isoformat()
        trend = random.uniform(-0.4, 0.4)
        vola = random.uniform(0.03, 0.5)
        rsi = random.uniform(20, 80)
        snaps.append({"demo_tag": TAG, "id": str(uuid.uuid4()), "symbol": "BTCUSDT",
                      "ts": ts, "features": {
                          "price": 60000 + i * 10, "rsi": round(rsi, 2),
                          "trend_pct": round(trend, 4), "atr_pct": 0.05,
                          "volatility_pct": round(vola, 4), "volume_ratio": 1.1,
                          "range_pos": 50.0, "change_60m_pct": round(trend * 3, 3),
                          "regime": "demo"}})
        long_side = trend > 0
        # Muster: Trades in Trendrichtung bei ruhiger Vola gewinnen häufiger.
        p_win = 0.75 if (long_side and vola < 0.3) else 0.35
        win = random.random() < p_win
        decisions.append({"demo_tag": TAG, "id": str(uuid.uuid4()), "symbol": "BTCUSDT",
                          "action": "LONG" if long_side else "SHORT",
                          "confidence": random.randint(55, 90), "ts": ts,
                          "sl_pct": 0.8, "tp1_pct": 1.3, "tpf_pct": 2.2,
                          "news_impact": "neutral", "rsi": round(rsi, 1),
                          "outcome": "win" if win else "loss",
                          "trade_pnl": round(random.uniform(0.5, 4.0), 2) if win
                          else round(-random.uniform(0.5, 3.0), 2)})
    await db.ai_market_snapshots.insert_many(snaps)
    await db.ai_decisions.insert_many(decisions)
    print(f"Demo-Daten angelegt: 1 Backtest, 1 Optimizer-Lauf, "
          f"{len(decisions)} Entscheidungen, {len(snaps)} Snapshots")


if __name__ == "__main__":
    asyncio.run(main("--clean" in sys.argv))
