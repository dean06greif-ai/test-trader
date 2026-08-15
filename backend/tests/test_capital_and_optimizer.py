"""
Tests for new features:
- Capital allocation (GET/POST /api/autotrade/capital)
- Balance allocation field (GET /api/autotrade/balance)
- Trades pnl_pct computed field (GET /api/autotrade/trades)
- Optimizer apply scope=coins + overrides endpoint
"""
import os
import pytest
import requests
from pymongo import MongoClient
from datetime import datetime, timezone

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.environ.get('DB_NAME', 'test_database')


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")})
    assert r.status_code == 200
    return r.json()["token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    return c[DB_NAME]


# -------- Capital allocation --------
class TestCapital:
    def test_get_capital_structure(self):
        r = requests.get(f"{BASE_URL}/api/autotrade/capital")
        assert r.status_code == 200
        data = r.json()
        assert "allocation" in data
        for scope in ("live", "paper"):
            assert scope in data["allocation"]
            a = data["allocation"][scope]
            for k in ("mode", "value", "allocated", "used_margin", "free"):
                assert k in a, f"missing key {k} for {scope}"

    def test_post_capital_requires_admin(self):
        r = requests.post(f"{BASE_URL}/api/autotrade/capital",
                          json={"scope": "paper", "mode": "full", "value": 0})
        assert r.status_code == 401

    def test_invalid_scope(self, headers):
        r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                          json={"scope": "xxx", "mode": "full", "value": 0})
        assert r.status_code == 400

    def test_invalid_mode(self, headers):
        r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                          json={"scope": "paper", "mode": "bad", "value": 0})
        assert r.status_code == 400

    def test_fixed_le_zero(self, headers):
        r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                          json={"scope": "paper", "mode": "fixed", "value": 0})
        assert r.status_code == 400

    def test_percent_over_100(self, headers):
        r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                          json={"scope": "paper", "mode": "percent", "value": 150})
        assert r.status_code == 400

    def test_paper_percent_computes_allocated(self, headers):
        # base 1000, 25% => 250
        r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                          json={"scope": "paper", "mode": "percent", "value": 25,
                                "base_balance": 1000})
        assert r.status_code == 200

        g = requests.get(f"{BASE_URL}/api/autotrade/capital").json()
        paper = g["allocation"]["paper"]
        assert paper["mode"] == "percent"
        assert paper["value"] == 25
        assert paper["allocated"] == 250.0

        # Verify same value in balance endpoint
        b = requests.get(f"{BASE_URL}/api/autotrade/balance").json()
        assert "allocation" in b
        assert b["allocation"]["paper"]["allocated"] == 250.0


# -------- Trades: pnl_pct --------
class TestTradesPnlPct:
    def test_pnl_pct_present(self, mongo):
        # Insert a test trade
        test_trade = {
            "id": "TEST_TRADE_PNLPCT",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "mode": "paper",
            "entry": 100.0,
            "qty": 1.0,
            "max_capital": 10.0,
            "leverage": 10,
            "status": "closed",
            "result": "win",
            "realized_pnl": 2.5,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "events": [],
            "trade_date": "2026-01-15",
        }
        mongo.auto_trades.delete_many({"id": "TEST_TRADE_PNLPCT"})
        mongo.auto_trades.insert_one(dict(test_trade))

        try:
            r = requests.get(f"{BASE_URL}/api/autotrade/trades?limit=200")
            assert r.status_code == 200
            trades = r.json()["trades"]
            match = next((t for t in trades if t.get("id") == "TEST_TRADE_PNLPCT"), None)
            assert match is not None, "test trade not found in response"
            comp = match.get("computed", {})
            assert "pnl_pct" in comp
            assert "pnl_pct_capital" in comp
            # 2.5 / (100*1) * 100 = 2.5
            assert comp["pnl_pct"] == 2.5
            # 2.5 / 10 * 100 = 25.0
            assert comp["pnl_pct_capital"] == 25.0
        finally:
            mongo.auto_trades.delete_many({"id": "TEST_TRADE_PNLPCT"})


# -------- Optimizer apply --------
class TestOptimizerApply:
    def test_scope_coins_missing_symbols(self, headers):
        r = requests.post(f"{BASE_URL}/api/optimizer/apply", headers=headers,
                          json={"type": "params", "scope": "coins",
                                "strategy_id": "scalping_4_rules",
                                "params": {"rsi_period": 12}})
        assert r.status_code == 400

    def test_scope_coins_success(self, headers, mongo):
        try:
            r = requests.post(f"{BASE_URL}/api/optimizer/apply", headers=headers,
                              json={"type": "params", "scope": "coins",
                                    "strategy_id": "scalping_4_rules",
                                    "symbols": ["BTCUSDT"],
                                    "params": {"rsi_period": 12},
                                    "trade_params": {"tp1_crv": 1.5}})
            assert r.status_code == 200, r.text

            # verify override endpoint
            o = requests.get(f"{BASE_URL}/api/optimizer/overrides/scalping_4_rules")
            assert o.status_code == 200
            syms = o.json().get("symbols", [])
            assert "BTCUSDT" in syms

            # verify tp1_crv persisted
            doc = mongo.strategy_coin_configs.find_one({"_id": "scalping_4_rules_BTCUSDT"})
            assert doc is not None
            assert doc.get("config", {}).get("tp1_crv") == 1.5
        finally:
            # cleanup
            mongo.strategy_coin_configs.delete_one({"_id": "scalping_4_rules_BTCUSDT"})
            settings_doc = mongo.settings.find_one({"_id": "scanner_settings"})
            if settings_doc:
                cp = settings_doc.get("coin_params", {})
                if "scalping_4_rules" in cp:
                    cp.pop("scalping_4_rules", None)
                    mongo.settings.update_one({"_id": "scanner_settings"},
                                              {"$set": {"coin_params": cp}})

    def test_scope_global_success(self, headers):
        r = requests.post(f"{BASE_URL}/api/optimizer/apply", headers=headers,
                          json={"type": "params",
                                "strategy_id": "scalping_4_rules",
                                "params": {"rsi_period": 14}})
        assert r.status_code == 200


# -------- Cleanup / reset defaults --------
def test_zzz_cleanup_defaults(headers):
    """Reset capital allocation to defaults after all tests."""
    r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                      json={"scope": "live", "mode": "full", "value": 0})
    assert r.status_code == 200
    r = requests.post(f"{BASE_URL}/api/autotrade/capital", headers=headers,
                      json={"scope": "paper", "mode": "full", "value": 0,
                            "base_balance": 1000})
    assert r.status_code == 200
