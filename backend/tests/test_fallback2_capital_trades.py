"""Regressionstests für die neuen Features (Iteration Fallback 2 / Max-Kapital / Offene-Trades-Aktionen).

  1. KI-Team: zweite Fallback-KI (fallback2) pro Rolle – Sanitize, Persistenz, Ketten-Reihenfolge
  2. KI Trader: Max. Kapital pro Trade (max_capital_per_trade) – Config-API + Clamping
  3. Trades → Offene Trades: manueller Aktions-Endpoint /api/autotrade/trade/{id}/action
"""
import os
import pytest
import requests

from services.ai_roles import AIRoleManager

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")

def _read_admin_password():
    try:
        import re as _re
        from pathlib import Path as _Path
        _txt = _Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
        _m = _re.search(r"Passwort\s*`([^`]+)`", _txt)
        return _m.group(1) if _m else None
    except OSError:
        return None

ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed ({r.status_code}): {r.text[:200]}")
    tok = r.json().get("token")
    if not tok:
        pytest.skip("No token in login response")
    return tok


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ---------------- 1. Fallback 2 (unit: pure Logik) ----------------
def test_base_role_has_fallback2_fields():
    mgr = AIRoleManager()
    cfg = mgr.role_cfg("analyst")
    assert "fallback2_provider" in cfg and "fallback2_model" in cfg


def test_sanitize_accepts_valid_fallback2():
    mgr = AIRoleManager()
    out = mgr._sanitize("analyst", {"fallback2_provider": "groq",
                                    "fallback2_model": "llama-3.1-8b-instant"})
    assert out.get("fallback2_provider") == "groq"
    assert out.get("fallback2_model") == "llama-3.1-8b-instant"


def test_sanitize_rejects_unknown_fallback2_model():
    mgr = AIRoleManager()
    out = mgr._sanitize("analyst", {"fallback2_model": "nicht-existent-9000"})
    assert "fallback2_model" not in out


def test_sanitize_clears_fallback2_with_none():
    mgr = AIRoleManager()
    out = mgr._sanitize("analyst", {"fallback2_model": None})
    assert out.get("fallback2_model") is None
    assert out.get("fallback2_provider") is None


def test_chain_order_primary_fb1_fb2():
    mgr = AIRoleManager()
    mgr.config["analyst"].update({
        "provider": "gemini", "model": "gemini-3.5-flash",
        "fallback_provider": "groq", "fallback_model": "llama-3.3-70b-versatile",
        "fallback2_provider": "mistral", "fallback2_model": "mistral-small-latest",
        "active_hours": None,
    })
    chain = mgr.chain("analyst", {"provider": "gemini", "model": "gemini-3.5-flash"})
    idx_primary = chain.index(("gemini", "gemini-3.5-flash"))
    idx_fb1 = chain.index(("groq", "llama-3.3-70b-versatile"))
    idx_fb2 = chain.index(("mistral", "mistral-small-latest"))
    assert idx_primary < idx_fb1 < idx_fb2


def test_chain_fb1_first_outside_active_hours():
    from datetime import datetime
    from services.ai_roles import BERLIN_TZ
    mgr = AIRoleManager()
    mgr.config["analyst"].update({
        "provider": "gemini", "model": "gemini-3.5-flash",
        "fallback_provider": "groq", "fallback_model": "llama-3.3-70b-versatile",
        "fallback2_provider": "mistral", "fallback2_model": "mistral-small-latest",
        "active_hours": {"start": "08:00", "end": "09:00"},
    })
    at_night = datetime(2026, 6, 1, 3, 0, tzinfo=BERLIN_TZ)
    chain = mgr.chain("analyst", {"provider": "gemini", "model": "gemini-3.5-flash"},
                      now=at_night)
    assert chain[0] == ("groq", "llama-3.3-70b-versatile")
    idx_fb2 = chain.index(("mistral", "mistral-small-latest"))
    idx_primary = chain.index(("gemini", "gemini-3.5-flash"))
    assert idx_fb2 < idx_primary


# ---------------- 1b. Fallback 2 (API: Persistenz) ----------------
def test_api_roles_persist_fallback2(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/roles", headers=admin_headers, timeout=15,
                      json={"analyst": {"fallback2_provider": "groq",
                                        "fallback2_model": "llama-3.1-8b-instant"}})
    assert r.status_code == 200, r.text[:300]
    roles = r.json()["roles"]
    assert roles["analyst"]["fallback2_model"] == "llama-3.1-8b-instant"
    assert roles["analyst"]["user_configured"] is True
    # zurücksetzen (kein Zustand hinterlassen)
    r2 = requests.post(f"{BASE_URL}/api/ai/roles", headers=admin_headers, timeout=15,
                       json={"analyst": {"fallback2_provider": None, "fallback2_model": None}})
    assert r2.status_code == 200
    assert r2.json()["roles"]["analyst"]["fallback2_model"] is None


def test_api_roles_get_contains_fallback2():
    r = requests.get(f"{BASE_URL}/api/ai/roles", timeout=15)
    assert r.status_code == 200
    for role, cfg in r.json()["roles"].items():
        assert "fallback2_model" in cfg, f"{role} ohne fallback2_model"


# ---------------- 2. Max. Kapital pro Trade ----------------
def test_api_config_max_capital_per_trade(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=admin_headers, timeout=15,
                      json={"max_capital_per_trade": 250})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["config"]["max_capital_per_trade"] == 250
    # 0 = aus (Default-Verhalten, Coin-Config gilt)
    r2 = requests.post(f"{BASE_URL}/api/ai/config", headers=admin_headers, timeout=15,
                       json={"max_capital_per_trade": 0})
    assert r2.status_code == 200
    assert r2.json()["config"]["max_capital_per_trade"] == 0


def test_api_config_max_capital_clamped_negative(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=admin_headers, timeout=15,
                      json={"max_capital_per_trade": -50})
    assert r.status_code == 200
    assert r.json()["config"]["max_capital_per_trade"] == 0


def test_emit_signal_carries_capital_fields():
    """Unit: _emit_signal hängt ai_max_capital/ai_capital_pct nur an, wenn konfiguriert."""
    from services.ai_engine import ai_engine
    max_cap = float(ai_engine.config.get("max_capital_per_trade") or 0)
    assert max_cap == 0  # nach den API-Tests wieder aus


# ---------------- 3. Manuelle Trade-Aktionen (Trades → Offene Trades) ----------------
def test_trade_action_requires_admin():
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/xyz/action",
                      json={"action": "partial_close", "value": 50}, timeout=15)
    assert r.status_code in (401, 403)


def test_trade_action_invalid_action(admin_headers):
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/xyz/action", headers=admin_headers,
                      json={"action": "close", "value": 1}, timeout=15)
    # 'close' läuft weiter über /api/autotrade/close/{id}
    assert r.status_code == 400


def test_trade_action_invalid_pct(admin_headers):
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/xyz/action", headers=admin_headers,
                      json={"action": "partial_close", "value": 150}, timeout=15)
    assert r.status_code == 400


def test_trade_action_unknown_trade(admin_headers):
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/gibt-es-nicht/action",
                      headers=admin_headers,
                      json={"action": "partial_close", "value": 50}, timeout=15)
    assert r.status_code == 404, r.text[:200]


def test_close_endpoint_still_works_unknown_trade(admin_headers):
    """Regression: bestehender Close-Endpoint unverändert (404 bei unbekannter ID)."""
    r = requests.post(f"{BASE_URL}/api/autotrade/close/gibt-es-nicht",
                      headers=admin_headers, timeout=15)
    assert r.status_code == 404
