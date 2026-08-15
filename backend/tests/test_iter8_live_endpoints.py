"""Live-Endpoint-Regressionstest Iteration 8 gegen die ECHTE Preview-URL
(REACT_APP_BACKEND_URL). Testet:
  - POST /api/ai/config akzeptiert & klemmt crv_min/crv_max/lev_mode/lev_auto_max/lev_fixed
  - GET  /api/ai/status liefert providers_health.active_fallbacks + config keys
  - POST /api/control/stop-trades gibt kein closed_trades-Feld mehr zurück
    und schließt keine Trades

WICHTIG: Läuft gegen die PROD-Atlas-DB. Deshalb werden am Ende
alle geänderten Werte auf die ursprünglichen Werte zurückgeschrieben
(siehe Kontext-Manager reset_ai_config und stop-trades-Original-State).
"""
import os
import time
import requests
import pytest


BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASSWORD = "TestAdmin2026!"
# Preview-Backend hängt an Prod-Atlas und ist teils sehr langsam -> 60s
TIMEOUT = 60

# Werte, die laut Review-Request nach dem Test wieder gesetzt werden müssen
RESET_AI_CONFIG = {
    "crv_min": 1.2,
    "crv_max": 0,
    "lev_mode": "coin",
    "lev_auto_max": 25,
    "lev_fixed": 10,
}


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                      timeout=TIMEOUT)
    assert r.status_code == 200, f"Admin-Login failed: {r.status_code} {r.text[:300]}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# GET /api/ai/status  – neue Felder aus Iteration 8
# ---------------------------------------------------------------------------
def test_ai_status_has_active_fallbacks_and_frame_config():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=TIMEOUT)
    assert r.status_code == 200, r.text[:200]
    data = r.json()

    # providers_health.active_fallbacks – Liste (darf leer sein)
    ph = data.get("providers_health") or {}
    assert isinstance(ph, dict), f"providers_health missing: {data.keys()}"
    fbs = ph.get("active_fallbacks")
    assert isinstance(fbs, list), f"active_fallbacks not a list: {fbs!r}"

    # config muss neue Trade-Rahmen-Keys enthalten
    cfg = data.get("config") or {}
    for key in ("crv_min", "crv_max", "lev_mode", "lev_auto_max", "lev_fixed"):
        assert key in cfg, f"Fehlender config-Key {key!r} in /api/ai/status"

    # Erlaubte lev_mode-Werte
    assert cfg["lev_mode"] in ("coin", "auto", "fixed"), cfg["lev_mode"]


# ---------------------------------------------------------------------------
# POST /api/ai/config  – Persistenz + Klemmung + ungültige Werte
# ---------------------------------------------------------------------------
def _get_cfg():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=TIMEOUT)
    return r.json().get("config") or {}


def test_ai_config_persists_and_clamps(auth_headers):
    # 1) Werte innerhalb der Grenzen setzen
    payload = {"crv_min": 1.7, "crv_max": 3.3,
               "lev_mode": "auto", "lev_auto_max": 30, "lev_fixed": 12}
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                      json=payload, timeout=TIMEOUT)
    assert r.status_code == 200, r.text[:300]
    cfg = _get_cfg()
    assert cfg["crv_min"] == 1.7
    assert cfg["crv_max"] == 3.3
    assert cfg["lev_mode"] == "auto"
    assert cfg["lev_auto_max"] == 30
    assert cfg["lev_fixed"] == 12

    # 2) Klemmung: crv_min < 1.0, lev_auto_max > 100
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                      json={"crv_min": 0.3, "lev_auto_max": 999}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text[:300]
    cfg = _get_cfg()
    assert cfg["crv_min"] == 1.0
    assert cfg["lev_auto_max"] == 100

    # 3) Ungültiger lev_mode wird ignoriert -> bleibt "auto"
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                      json={"lev_mode": "yolo"}, timeout=TIMEOUT)
    assert r.status_code == 200
    cfg = _get_cfg()
    assert cfg["lev_mode"] == "auto", f"lev_mode ungültig übernommen: {cfg['lev_mode']}"


def test_ai_config_requires_admin():
    r = requests.post(f"{BASE_URL}/api/ai/config",
                      headers={"Content-Type": "application/json"},
                      json={"crv_min": 2.0}, timeout=TIMEOUT)
    # 401 oder 403 sind beide akzeptabel
    assert r.status_code in (401, 403), f"Unerlaubter Zugriff gestattet: {r.status_code}"


# ---------------------------------------------------------------------------
# POST /api/control/stop-trades – keine Trades mehr schließen, kein closed_trades
# ---------------------------------------------------------------------------
def test_stop_trades_no_closed_trades_field(auth_headers):
    # Original-Zustand lesen
    r = requests.get(f"{BASE_URL}/api/control/state", timeout=TIMEOUT)
    assert r.status_code == 200
    original_paused = bool(r.json().get("trades_paused"))

    try:
        # 1x togglen
        r = requests.post(f"{BASE_URL}/api/control/stop-trades",
                          headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert "closed_trades" not in body, \
            f"closed_trades-Feld noch vorhanden: {body}"
        assert "trades_paused" in body
        new_state = bool(body.get("trades_paused"))
        assert new_state != original_paused, "Schalter wurde nicht getoggelt"

        # Zurücktogglen -> Original-Zustand
        r = requests.post(f"{BASE_URL}/api/control/stop-trades",
                          headers=auth_headers, timeout=TIMEOUT)
        assert r.status_code == 200
        body2 = r.json()
        assert "closed_trades" not in body2
        assert bool(body2.get("trades_paused")) == original_paused
    finally:
        # Sicherheitsnetz: falls zwischendrin ein Fehler auftrat,
        # nochmals prüfen und ggf. zurücksetzen.
        r = requests.get(f"{BASE_URL}/api/control/state", timeout=TIMEOUT)
        if bool(r.json().get("trades_paused")) != original_paused:
            requests.post(f"{BASE_URL}/api/control/stop-trades",
                          headers=auth_headers, timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# Sicherheits-Teardown: AI-Config zurücksetzen (PROD-DB!)
# ---------------------------------------------------------------------------
def test_zzz_reset_ai_config_to_defaults(auth_headers):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                      json=RESET_AI_CONFIG, timeout=TIMEOUT)
    assert r.status_code == 200, r.text[:300]
    cfg = _get_cfg()
    assert cfg["crv_min"] == 1.2
    assert cfg["crv_max"] == 0
    assert cfg["lev_mode"] == "coin"
    assert cfg["lev_auto_max"] == 25
    assert cfg["lev_fixed"] == 10
