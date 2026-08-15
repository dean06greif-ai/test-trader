"""Backend regression for rly-3.0 review: order blocks in liquidity levels, klines history endpoint, and open trade E2E prep."""
import os
import json
import time
import uuid
import datetime as dt
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def s():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


# ---------- Order blocks in liquidity levels ----------
class TestLiquidityOrderBlocks:
    def test_levels_structure_and_ob_shape(self, s):
        r = s.get(f"{BASE_URL}/api/liquidity/levels/BTCUSDT?interval=15m", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "levels" in data
        levels = data["levels"]
        assert isinstance(levels, list) and len(levels) > 0

        # counts dict may include ob_bull/ob_bear keys
        counts = data.get("counts") or {}
        assert isinstance(counts, dict)

        ob_levels = [lv for lv in levels if lv.get("type") in ("ob_bull", "ob_bear")]
        # If OBs exist, validate their shape strictly
        for ob in ob_levels:
            for key in ("zone_low", "zone_high", "untested", "strength", "type"):
                assert key in ob, f"OB missing key {key}: {ob}"
            assert isinstance(ob["untested"], bool)
            assert isinstance(ob["zone_low"], (int, float))
            assert isinstance(ob["zone_high"], (int, float))
            assert ob["zone_high"] >= ob["zone_low"]
            assert isinstance(ob["strength"], (int, float))
        print(f"OBs found: {len(ob_levels)} / total levels {len(levels)}; counts={counts}")


# ---------- Klines history endpoint ----------
class TestKlinesHistory:
    def test_days_7_default_15m(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=7", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("timeframe") == "15m"
        n = len(d.get("candles", []))
        assert 600 <= n <= 720, f"expected ~672 candles, got {n}"

    def test_days_30_default_1h(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=30", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("timeframe") == "1h"
        n = len(d.get("candles", []))
        assert 650 <= n <= 760, f"expected ~720 candles, got {n}"

    def test_explicit_timeframe_param(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT/history?days=7&timeframe=30m", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get("timeframe") == "30m", d
        n = len(d.get("candles", []))
        # 7 days at 30m -> ~336 candles
        assert 300 <= n <= 400, f"expected ~336 got {n}"

    def test_invalid_symbol_502(self, s):
        r = s.get(f"{BASE_URL}/api/klines/ZZZZZZZZ/history?days=7", timeout=30)
        assert r.status_code == 502, f"expected 502 for invalid sym, got {r.status_code} - {r.text[:200]}"


# ---------- Regression: live klines ----------
class TestKlinesLiveRegression:
    def test_live_klines_200_bars(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT?limit=200", timeout=30)
        assert r.status_code == 200
        d = r.json()
        # candles may be top-level list or under 'candles'
        candles = d.get("candles") if isinstance(d, dict) else d
        assert candles and len(candles) >= 190


# ---------- E2E synthetic OPEN trade in Mongo + autotrade endpoint ----------
class TestSyntheticOpenTrade:
    TRADE_ID = "TEST-CHART-1"

    def _price(self, s):
        r = s.get(f"{BASE_URL}/api/klines/BTCUSDT?limit=2", timeout=20)
        d = r.json()
        candles = d.get("candles") if isinstance(d, dict) else d
        last = candles[-1]
        # candle shape: {time,open,high,low,close,volume} or list
        if isinstance(last, dict):
            return float(last.get("close"))
        return float(last[4])

    def test_insert_verify_delete(self, s):
        cli = MongoClient(MONGO_URL)
        db = cli[DB_NAME]
        coll = db["auto_trades"]
        # cleanup any stale
        coll.delete_many({"id": self.TRADE_ID})

        entry = self._price(s)
        now = dt.datetime.utcnow()
        doc = {
            "id": self.TRADE_ID,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "mode": "paper",
            "status": "open",
            "entry": entry,
            "sl": entry * 0.99,
            "tp1": entry * 1.01,
            "tpf": entry * 1.02,
            "qty": 0.001,
            "qty_remaining": 0.001,
            "tp1_hit": False,
            "strategy_id": "test_strat",
            "strategy_name": "TEST Strategie",
            "opened_at": now.isoformat(),
            "trade_date": now.date().isoformat(),
            "realized_pnl": 0,
            "fees_paid": 0,
            "max_capital": 10,
            "leverage": 5,
        }
        coll.insert_one(doc)

        try:
            r = s.get(f"{BASE_URL}/api/autotrade/trades?status=open", timeout=20)
            assert r.status_code == 200, r.text
            data = r.json()
            trades = data if isinstance(data, list) else data.get("trades", [])
            ids = [t.get("id") for t in trades]
            assert self.TRADE_ID in ids, f"synthetic trade not returned by API. ids={ids}"
            picked = next(t for t in trades if t.get("id") == self.TRADE_ID)
            assert picked.get("strategy_name") == "TEST Strategie"
            assert picked.get("side") == "LONG"
            assert picked.get("status") == "open"
        finally:
            coll.delete_many({"id": self.TRADE_ID})
            cli.close()
