"""Regression tests for iter rly-2.9 review:
- Strict input validation on POST /api/strategies/custom (422 with problems+fixes)
- Alias auto-canonicalization ema_200 -> ema(200)
- Unsupported timeframe rejection
- Auth still enforced on POST /api/strategies/custom
- Regression: GET /api/strategies, /builder-options, POST /rule-preview
- Liquidity heatmap for BTC, ADA, AVAX incl. oi_venues + orderbook_walls
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, r.text
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def admin(api, admin_token):
    api.headers.update({"Authorization": f"Bearer {admin_token}"})
    return api


# ---------- strategies validation ----------
class TestStrategyValidation:
    def test_invalid_indicator_returns_422_with_problems_and_fixes(self, admin):
        payload = {
            "name": "TEST_rly29_bad",
            "timeframe": "15m",
            "long_rules": [{"indicator": "super_trend_x", "op": "<", "value": "price"}],
            "short_rules": [],
        }
        r = admin.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=15)
        assert r.status_code == 422, r.text
        detail = r.json().get("detail", {})
        assert isinstance(detail, dict)
        assert "problems" in detail and isinstance(detail["problems"], list) and detail["problems"]
        assert "fixes" in detail and isinstance(detail["fixes"], list)
        assert "message" in detail

    def test_alias_ema_200_canonicalized_and_saved(self, admin):
        payload = {
            "name": "T1",
            "timeframe": "15m",
            "long_rules": [{"indicator": "ema_200", "op": "<", "value": "price"}],
            "short_rules": [],
        }
        r = admin.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body["id"]
        try:
            definition = body.get("definition", {})
            long_rules = definition.get("long_rules") or []
            assert long_rules, "long_rules missing after canonicalization"
            indicator = long_rules[0].get("indicator")
            assert indicator == "ema(200)", f"expected ema(200), got {indicator!r}"

            # Also verify GET /api/strategies contains canonicalized indicator
            r2 = admin.get(f"{BASE_URL}/api/strategies", timeout=15)
            assert r2.status_code == 200
            found = [s for s in r2.json()["strategies"] if s.get("id") == sid]
            assert found, "created strategy missing in list"
            d2 = found[0].get("definition", {})
            assert (d2.get("long_rules") or [{}])[0].get("indicator") == "ema(200)"
        finally:
            r_del = admin.delete(f"{BASE_URL}/api/strategies/custom/{sid}", timeout=15)
            assert r_del.status_code == 200

    def test_unsupported_timeframe_7m_returns_422(self, admin):
        payload = {
            "name": "TEST_rly29_bad_tf",
            "timeframe": "7m",
            "long_rules": [{"indicator": "ema(200)", "op": "<", "value": "price"}],
            "short_rules": [],
        }
        r = admin.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=15)
        assert r.status_code == 422, r.text
        detail = r.json().get("detail", {})
        probs = " ".join(detail.get("problems", []))
        assert "timeframe" in probs.lower()

    def test_no_admin_token_returns_401_or_403(self, api):
        # fresh session with no auth header
        s = requests.Session()
        payload = {
            "name": "TEST_rly29_noauth",
            "timeframe": "15m",
            "long_rules": [{"indicator": "ema(200)", "op": "<", "value": "price"}],
            "short_rules": [],
        }
        r = s.post(f"{BASE_URL}/api/strategies/custom", json=payload, timeout=15)
        assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}: {r.text}"


# ---------- regression ----------
class TestStrategyRegression:
    def test_get_strategies(self, api):
        r = api.get(f"{BASE_URL}/api/strategies", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data.get("strategies"), list) and len(data["strategies"]) > 0

    def test_builder_options(self, api):
        r = api.get(f"{BASE_URL}/api/strategies/builder-options", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "indicators" in data and "operators" in data
        assert isinstance(data["indicators"], list) and data["indicators"]

    def test_rule_preview(self, admin):
        body = {
            "definition": {
                "timeframe": "15m",
                "long_rules": [{"indicator": "rsi(14)", "op": "<", "value": 30}],
                "short_rules": [],
            },
            "symbol": "BTCUSDT",
            "days": 3,
        }
        r = admin.post(f"{BASE_URL}/api/strategies/rule-preview", json=body, timeout=45)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "long_rules" in data and "long_signals" in data
        assert data.get("bars", 0) > 0


# ---------- liquidity heatmap ----------
@pytest.mark.parametrize("symbol", ["BTCUSDT", "ADAUSDT", "AVAXUSDT"])
def test_liquidity_heatmap_has_venues_and_walls(api, symbol):
    r = api.get(f"{BASE_URL}/api/liquidity/heatmap/{symbol}", timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["symbol"] == symbol
    assert "bins" in d and isinstance(d["bins"], list) and len(d["bins"]) > 0
    # bins should have heat values
    sample = d["bins"][0]
    assert "heat" in sample or "score" in sample or "value" in sample or "weight" in sample \
        or any(k for k in sample.keys() if "heat" in k.lower())
    assert d.get("oi_usd") is not None
    assert isinstance(d.get("oi_venues"), list) and len(d["oi_venues"]) >= 1
    assert "orderbook_walls" in d
    assert "bids" in d["orderbook_walls"] and "asks" in d["orderbook_walls"]
