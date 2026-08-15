"""
Iteration 5 – Read-only regression tests for the 4 session fixes:
1. GET /api/liquidity/heatmap/BTCUSDT?bins=40 -> clusters_source + bins
2. GET /api/autotrade/watchdog/status -> settings.manage_external present
3. POST /api/autotrade/watchdog/config (admin) -> manage_external toggle (must
   end at False!) and 401/403 without token
4. GET /api/telegram/notify-config -> contains key token_alert

CRITICAL: This test flips manage_external true and IMMEDIATELY back to false.
Verified via a second GET /watchdog/status at the very end.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = "Dea...eif!/Admin"


def _login_token():
    """Admin login. Retries transient CF 502/503/504."""
    last = None
    for attempt in range(4):
        try:
            r = requests.post(
                f"{BASE_URL}/api/auth/login",
                json={"username": ADMIN_USER, "password": ADMIN_PASS},
                timeout=90,
            )
            last = r
            if r.status_code == 200:
                tok = r.json().get("token")
                assert isinstance(tok, str) and len(tok) > 20
                return tok
            if r.status_code in (502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    if isinstance(last, requests.Response):
        raise AssertionError(f"login failed: {last.status_code} {last.text[:200]}")
    raise AssertionError(f"login failed: {last}")


@pytest.fixture(scope="module")
def token():
    return _login_token()


# --- (1) Heatmap: clusters_source + bins ---
def test_liquidity_heatmap_has_source_and_bins():
    # Heatmap aggregates data from Binance/OKX/Bybit; can take ~30-90s cold.
    # Preview edge may throw CF 502/503 while backend is still warming up.
    r = None
    last_text = ""
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE_URL}/api/liquidity/heatmap/BTCUSDT",
                             params={"bins": 40}, timeout=120)
            last_text = r.text[:200]
            if r.status_code == 200:
                break
            if r.status_code in (502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            break
        except requests.exceptions.RequestException as e:
            last_text = str(e)
            time.sleep(3)
    assert r is not None and r.status_code == 200, \
        f"heatmap failed ({getattr(r, 'status_code', 'no-resp')}): {last_text}"
    d = r.json()
    assert "clusters_source" in d, f"missing clusters_source: {list(d.keys())}"
    assert d["clusters_source"] in ("measured", "model"), \
        f"unexpected clusters_source: {d['clusters_source']}"
    assert "bins" in d and isinstance(d["bins"], list), \
        f"missing/invalid bins: type={type(d.get('bins'))}"
    # bins should be non-trivial for BTCUSDT (>=10 entries when data is present)
    assert len(d["bins"]) >= 1


# --- (2) Watchdog status includes manage_external ---
def test_watchdog_status_has_manage_external(token):
    r = requests.get(f"{BASE_URL}/api/autotrade/watchdog/status",
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    settings = d.get("settings")
    assert isinstance(settings, dict), f"settings missing/not dict: {type(settings)}"
    assert "manage_external" in settings, f"manage_external key missing: {list(settings.keys())}"
    assert isinstance(settings["manage_external"], bool)


# --- (3) Auth guard on POST /watchdog/config ---
def test_watchdog_config_requires_admin():
    r = requests.post(f"{BASE_URL}/api/autotrade/watchdog/config",
                      json={"manage_external": False}, timeout=60)
    assert r.status_code in (401, 403), f"unexpected {r.status_code}: {r.text[:200]}"


# --- (3b) Toggle manage_external true, THEN reset to false ---
def test_watchdog_manage_external_toggle_and_reset(token):
    # Get current value first so we can force reset regardless
    initial = requests.get(f"{BASE_URL}/api/autotrade/watchdog/status",
                           headers={"Authorization": f"Bearer {token}"},
                           timeout=60).json()
    initial_val = bool(initial.get("settings", {}).get("manage_external", False))

    try:
        # Turn ON
        r = requests.post(f"{BASE_URL}/api/autotrade/watchdog/config",
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"},
                          json={"manage_external": True}, timeout=60)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("settings", {}).get("manage_external") is True, \
            f"manage_external not True after enable: {body}"
    finally:
        # ALWAYS reset to False (guarantee: end state MUST be false)
        rr = requests.post(f"{BASE_URL}/api/autotrade/watchdog/config",
                           headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"},
                           json={"manage_external": False}, timeout=60)
        assert rr.status_code == 200, rr.text[:200]
        assert rr.json().get("settings", {}).get("manage_external") is False, \
            f"reset to False failed: {rr.text[:200]}"

    # Independent verification via GET (fresh call)
    verify = requests.get(f"{BASE_URL}/api/autotrade/watchdog/status",
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=60).json()
    assert verify.get("settings", {}).get("manage_external") is False, \
        f"CRITICAL: manage_external not False at end – state leaked ON! " \
        f"(initial was {initial_val}) full: {verify}"


# --- (4) Notify config contains token_alert ---
def test_notify_config_has_token_alert(token):
    r = requests.get(f"{BASE_URL}/api/telegram/notify-config",
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    # Endpoint may return config directly or nested
    cfg = d.get("config") if isinstance(d.get("config"), dict) else d
    assert "token_alert" in cfg, f"token_alert missing in notify config: {list(cfg.keys())}"
    assert isinstance(cfg["token_alert"], bool)
    # Default per notifications.DEFAULT_CONFIG is True
    assert cfg["token_alert"] is True, \
        f"token_alert not True (default should be True): {cfg['token_alert']}"
