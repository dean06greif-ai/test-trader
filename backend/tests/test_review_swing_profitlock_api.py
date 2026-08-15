"""Review-Test: End-to-End HTTP-Prüfungen für Swing/Profit-Lock/PnL-Bugfixes.

Vorsicht: Läuft gegen produktive Preview-URL mit echten Live-Daten.
KEINE destruktiven Aktionen. Config wird nach dem Test zurückgesetzt.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://daytrader-ml.preview.emergentagent.com",
).rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Dean06Greif!/Admin")
TIMEOUT = 90


def _login():
    last = None
    for _ in range(4):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                          timeout=TIMEOUT)
        if r.status_code == 200:
            return r
        last = r
    return last


@pytest.fixture(scope="session")
def admin_token():
    r = _login()
    assert r.status_code == 200, f"Admin-Login fehlgeschlagen: {r.status_code} {r.text[:200]}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _get(path):
    """GET mit einfachem Retry (Ingress-502 gelegentlich bei /api/ai/trade/status)."""
    last = None
    for _ in range(3):
        r = requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        if r.status_code == 200:
            return r
        last = r
    return last


# ---------------------------- Trade-Manager (Profit-Lock) ----------------------------
class TestTradeManagerStatus:
    def test_status_contains_profit_lock_settings(self):
        r = _get("/api/ai/trade/status")
        assert r.status_code == 200, r.text
        s = r.json().get("status", {}).get("settings", {})
        for key in ("profit_lock_enabled", "profit_lock_max_leverage", "profit_lock_min_margin_pct"):
            assert key in s, f"{key} fehlt"
        assert isinstance(s["profit_lock_enabled"], bool)
        assert 1 <= int(s["profit_lock_max_leverage"]) <= 200
        assert 0 <= int(s["profit_lock_min_margin_pct"]) <= 90

    def test_actions_contains_secure_profit(self):
        r = _get("/api/ai/trade/status")
        assert r.status_code == 200
        assert "secure_profit" in r.json()["status"]["actions"]


class TestTradeManagerSettingsClampAndReset:
    def test_settings_persist_and_clamp(self, auth_headers):
        r = _get("/api/ai/trade/status")
        s0 = r.json()["status"]["settings"]
        orig_max = int(s0["profit_lock_max_leverage"])
        orig_min = int(s0["profit_lock_min_margin_pct"])

        try:
            r = requests.post(f"{BASE_URL}/api/ai/trade/settings",
                              json={"profit_lock_max_leverage": 125,
                                    "profit_lock_min_margin_pct": 20},
                              headers=auth_headers, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            s = r.json().get("settings", {})
            assert s.get("profit_lock_max_leverage") == 125
            assert s.get("profit_lock_min_margin_pct") == 20

            r = _get("/api/ai/trade/status")
            s = r.json()["status"]["settings"]
            assert s["profit_lock_max_leverage"] == 125
            assert s["profit_lock_min_margin_pct"] == 20

            # Clamp obere Grenze
            r = requests.post(f"{BASE_URL}/api/ai/trade/settings",
                              json={"profit_lock_max_leverage": 999},
                              headers=auth_headers, timeout=TIMEOUT)
            assert r.status_code == 200
            v = r.json()["settings"]["profit_lock_max_leverage"]
            assert 5 <= v <= 200 and v < 999, f"clamp obere Grenze verletzt: {v}"

            # Clamp untere Grenze für profit_lock_min_margin_pct (5..90)
            r = requests.post(f"{BASE_URL}/api/ai/trade/settings",
                              json={"profit_lock_min_margin_pct": 1},
                              headers=auth_headers, timeout=TIMEOUT)
            assert r.status_code == 200
            v = r.json()["settings"]["profit_lock_min_margin_pct"]
            assert 5 <= v <= 90, f"clamp untere Grenze verletzt: {v}"
        finally:
            requests.post(f"{BASE_URL}/api/ai/trade/settings",
                          json={"profit_lock_max_leverage": orig_max or 100,
                                "profit_lock_min_margin_pct": orig_min or 15},
                          headers=auth_headers, timeout=TIMEOUT)

    def test_settings_requires_admin(self):
        r = requests.post(f"{BASE_URL}/api/ai/trade/settings",
                          json={"profit_lock_max_leverage": 100}, timeout=TIMEOUT)
        assert r.status_code in (401, 403)


# ---------------------------- KI-Trader-Config (Swing) --------------------------------
class TestAIConfigSwing:
    def test_status_exposes_swing_fields(self):
        r = _get("/api/ai/status")
        assert r.status_code == 200
        cfg = r.json().get("config", {})
        assert "swing_enabled" in cfg
        assert "swing_max_leverage" in cfg
        assert isinstance(cfg["swing_enabled"], bool)
        assert 1 <= int(cfg["swing_max_leverage"]) <= 20

    def test_update_swing_max_leverage_persist_clamp_reset(self, auth_headers):
        r = _get("/api/ai/status")
        orig = int(r.json()["config"].get("swing_max_leverage", 8))
        try:
            r = requests.post(f"{BASE_URL}/api/ai/config",
                              json={"swing_max_leverage": 10},
                              headers=auth_headers, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["config"]["swing_max_leverage"] == 10

            r = _get("/api/ai/status")
            assert r.json()["config"]["swing_max_leverage"] == 10

            # Clamp obere Grenze (max 20)
            r = requests.post(f"{BASE_URL}/api/ai/config",
                              json={"swing_max_leverage": 50},
                              headers=auth_headers, timeout=TIMEOUT)
            assert r.status_code == 200
            v = r.json()["config"]["swing_max_leverage"]
            assert 1 <= v <= 20, f"clamp verletzt: {v}"
        finally:
            requests.post(f"{BASE_URL}/api/ai/config",
                          json={"swing_max_leverage": orig or 8},
                          headers=auth_headers, timeout=TIMEOUT)


# ---------------------------- Autotrade / Trades (PnL) --------------------------------
class TestAutotradeTradesComputed:
    def test_trades_have_pnl_pct_margin(self):
        r = _get("/api/autotrade/trades?limit=10")
        assert r.status_code == 200
        data = r.json()
        items = data if isinstance(data, list) else data.get("items", data.get("trades", []))
        assert len(items) > 0
        seen_open = False
        for t in items[:10]:
            c = t.get("computed")
            assert c is not None, f"Trade {t.get('id')} ohne computed"
            assert "margin_used" in c
            assert "pnl_pct_margin" in c
            if str(t.get("status") or "").lower() == "open":
                assert "upnl_pct_margin" in c, f"upnl_pct_margin fehlt bei offenem Trade {t.get('id')}"
                seen_open = True
        assert seen_open, "kein offener Trade zum Prüfen"


# ---------------------------- Watchdog -------------------------------------------------
class TestWatchdog:
    def test_status_ok_and_dust_closed_field(self):
        r = _get("/api/autotrade/watchdog/status")
        assert r.status_code == 200
        j = r.json()
        assert "dust_closed" in j
        assert isinstance(j["dust_closed"], int)
        assert "positions" in j
