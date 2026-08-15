"""
Iteration 26 backend tests:
- CRV migration verification (crv_max=4.0)
- GET /api/ai/regime/{symbol}
- GET /api/autotrade/trade/{id}/explain
- POST /api/autotrade/trade/{id}/rethink (auth + cooldown)

Note: The full LLM rethink flow is exercised interactively / by curl to avoid
repeated LLM cost. This pytest suite validates the deterministic paths.
"""
import os
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://regime-analyzer-4.preview.emergentagent.com').rstrip('/')
MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.environ.get('DB_NAME', 'test_database')

TRADE_ID = 't26-x'
DEC_ID = 'dec26-x'

_client = MongoClient(MONGO_URL)
_db = _client[DB_NAME]


def _seed_open_trade():
    import datetime
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    _db.auto_trades.delete_many({'id': TRADE_ID})
    _db.ai_decisions.delete_many({'id': DEC_ID})
    _db.auto_trades.insert_one({
        'id': TRADE_ID, 'strategy_id': 'ai_trader', 'mode': 'paper', 'status': 'open',
        'symbol': 'XRPUSDT', 'side': 'long', 'entry': 2.5, 'sl': 2.45, 'initial_sl': 2.45,
        'tp1': 2.62, 'tpf': 2.8, 'qty': 40, 'max_capital': 100, 'leverage': 5, 'fee_percent': 0.06,
        'decision_id': DEC_ID, 'setup': 'breakout',
        'ai_reasoning': 'Ausbruch mit Volumen', 'ai_confidence': 72, 'ai_news_impact': 'positive',
        'ai_size_reason': 'Konservativer Einsatz', 'ai_levels_reason': 'SL unter Range-Low',
        'opened_at': now,
    })
    _db.ai_decisions.insert_one({
        'id': DEC_ID, 'reasoning': 'Breakout', 'size_reason': 'Konservativ',
        'levels_reason': 'SL knapp', 'confidence': 72, 'news_impact': 'positive',
        'setup': 'breakout', 'model': 'gpt-4o-mini', 'capital_pct': 5,
        'gate_shadow': {'p_win': 0.62},
        'entry_market_snapshot': {'features': {'regime': 'breakout_normal', 'regime_v': 2,
                                               'vol_rank': 55, 'daily_bias': 'up', 'vol_basis': 'percentile'}}
    })


# ---------- crv_max migration ----------
def test_crv_max_migration_settings_doc():
    doc = _db.settings.find_one({'_id': 'ai_trader_config'})
    assert doc is not None
    assert doc.get('crv_max_migrated_v1') is True
    # Migration lief (Flag) – der Wert selbst ist eine Nutzer-Einstellung
    # und darf sich seitdem geändert haben (nicht auf 4.0 pinnen).
    assert float(doc.get('crv_max', 0)) >= 1.0


def test_ai_status_crv_max():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=15)
    assert r.status_code == 200
    assert float(r.json().get('config', {}).get('crv_max')) >= 1.0


# ---------- regime v2 ----------
def test_regime_btcusdt_v2():
    r = requests.get(f"{BASE_URL}/api/ai/regime/BTCUSDT", timeout=20)
    assert r.status_code == 200
    body = r.json()
    feats = body.get('features')
    assert feats is not None
    assert feats.get('regime_v') == 2
    assert 'regime' in feats
    assert 'vol_basis' in feats


def test_regime_non_crypto_no_crash():
    r = requests.get(f"{BASE_URL}/api/ai/regime/XAUUSD", timeout=20)
    assert r.status_code == 200
    body = r.json()
    # features may be null but must not crash
    assert 'features' in body


# ---------- explain ----------
def test_explain_404():
    r = requests.get(f"{BASE_URL}/api/autotrade/trade/definitely-missing-xyz/explain", timeout=10)
    assert r.status_code == 404


def test_explain_facts_and_decision():
    _seed_open_trade()
    r = requests.get(f"{BASE_URL}/api/autotrade/trade/{TRADE_ID}/explain", timeout=15)
    assert r.status_code == 200
    b = r.json()
    facts = b['facts']
    assert facts['sl_dist_pct'] == 2.0
    assert facts['crv_tp1'] == 2.4
    assert facts['risk_usd'] == 2.0
    assert facts['notional_usdt'] == 500.0
    assert facts['roundtrip_fees_usdt'] == 0.12
    assert facts['fees_vs_risk_pct'] == 6.0
    assert facts['fee_guard_min_sl_pct'] == 0.48
    dec = b['decision']
    assert dec['regime'] == 'breakout_normal'
    assert dec['size_reason']
    assert dec['levels_reason']


# ---------- rethink auth + cooldown ----------
def _admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={'username': os.environ.get('ADMIN_USER', 'Admin'),
                            'password': os.environ.get('ADMIN_PASSWORD', 'Dean06Greif!/Admin')},
                      timeout=10)
    assert r.status_code == 200
    return r.json().get('token') or r.json().get('access_token')


def test_rethink_requires_auth():
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/{TRADE_ID}/rethink", timeout=10)
    assert r.status_code == 401


def test_rethink_cooldown_429():
    # Seed open trade with recent rethink_ts to trigger cooldown deterministically
    import datetime
    _seed_open_trade()
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    _db.auto_trades.update_one({'id': TRADE_ID}, {'$set': {'rethink_ts': now}})
    tok = _admin_token()
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/{TRADE_ID}/rethink",
                      headers={'Authorization': f'Bearer {tok}'}, timeout=15)
    assert r.status_code == 429


def test_cleanup_seed():
    _db.auto_trades.delete_many({'id': {'$in': [TRADE_ID, 'ui-test-2']}})
    _db.ai_decisions.delete_many({'id': {'$in': [DEC_ID, 'dec-x1']}})
