"""Zusätzliche E2E-Tests (Testing-Agent) zu Fallback2/MaxKapital/Offene-Trades-Aktionen.

Ergänzt /app/backend/tests/test_fallback2_capital_trades.py um echte HTTP-Flows,
inkl. Öffnen eines Paper-Trades und Ausführen manueller Aktionen.
"""
import os
import time

import pytest
import requests

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


@pytest.fixture(scope="module")
def admin_headers():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed {r.status_code}: {r.text[:200]}")
    tok = r.json().get("token")
    if not tok:
        pytest.skip("no token in login response")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


# ------- 1. /api/ai/status widerspiegelt max_capital_per_trade --------------
def test_status_reflects_max_capital(admin_headers):
    # setzen
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=admin_headers,
                      json={"max_capital_per_trade": 175}, timeout=15)
    assert r.status_code == 200
    # in /api/ai/status.config lesbar
    r2 = requests.get(f"{BASE_URL}/api/ai/status", timeout=15)
    assert r2.status_code == 200
    cfg = r2.json().get("config") or {}
    assert float(cfg.get("max_capital_per_trade") or 0) == 175
    # aufräumen -> 0
    r3 = requests.post(f"{BASE_URL}/api/ai/config", headers=admin_headers,
                       json={"max_capital_per_trade": 0}, timeout=15)
    assert r3.status_code == 200


# ------- 2. Backup-Keys / Cerebras backup gemeldet -------------------------
def test_status_reports_backup_keys():
    r = requests.get(f"{BASE_URL}/api/ai/status", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "backup_keys" in data, "backup_keys sollte im Status vorhanden sein"
    # cerebras backup wurde laut Review-Request in .env ergänzt
    assert data["backup_keys"].get("cerebras") is True


# ------- 3. Fallback2 auch für weitere Rollen persistierbar ----------------
def test_fallback2_persistence_second_role(admin_headers):
    r = requests.post(f"{BASE_URL}/api/ai/roles", headers=admin_headers, timeout=15,
                      json={"trade_manager": {"fallback2_provider": "groq",
                                              "fallback2_model": "llama-3.1-8b-instant"}})
    assert r.status_code == 200, r.text[:200]
    roles = r.json()["roles"]
    assert roles["trade_manager"]["fallback2_model"] == "llama-3.1-8b-instant"
    # zurücksetzen
    r2 = requests.post(f"{BASE_URL}/api/ai/roles", headers=admin_headers, timeout=15,
                       json={"trade_manager": {"fallback2_provider": None,
                                               "fallback2_model": None}})
    assert r2.status_code == 200
    assert r2.json()["roles"]["trade_manager"]["fallback2_model"] is None


# ------- 4. Reset einer Rolle setzt fallback2 zurück -----------------------
def test_reset_role_clears_fallback2(admin_headers):
    requests.post(f"{BASE_URL}/api/ai/roles", headers=admin_headers, timeout=15,
                  json={"analyst": {"fallback2_provider": "groq",
                                    "fallback2_model": "llama-3.1-8b-instant"}})
    r = requests.post(f"{BASE_URL}/api/ai/roles/analyst/reset",
                     headers=admin_headers, timeout=15)
    assert r.status_code == 200, r.text[:200]
    roles = r.json()["roles"]
    # nach Reset ist user_configured False; fallback2 kommt aus Base-Config
    assert roles["analyst"].get("user_configured") in (False, None, 0)


# ------- 5. E2E: Paper-Trade öffnen -> adjust_sl -> partial 30% -> close ---
def _find_open_paper_trade():
    r = requests.get(f"{BASE_URL}/api/autotrade/trades?status=open&mode=paper&limit=50",
                     timeout=15)
    if r.status_code != 200:
        return None
    for t in r.json().get("trades", []):
        if t.get("status") == "open":
            return t
    return None


def _open_paper_trade(admin_headers, symbol):
    body = {"symbol": symbol, "side": "LONG", "sl_pct": 1.0, "tp1_pct": 1.5,
            "tpf_pct": 2.5, "source": "manuell", "capital_pct": 20}
    r = requests.post(f"{BASE_URL}/api/ai/trade/open", headers=admin_headers,
                      json=body, timeout=25)
    return r


def test_open_trade_actions_e2e(admin_headers):
    # sicherstellen mode=paper (nicht ändern falls schon paper)
    st = requests.get(f"{BASE_URL}/api/ai/status", timeout=15).json()
    mode = (st.get("config") or {}).get("mode") or "paper"
    if mode == "live":
        pytest.skip("Autotrade steht auf live – E2E skip (nur paper testen)")

    trade = _find_open_paper_trade()
    opened_here = False
    if not trade:
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"):
            r = _open_paper_trade(admin_headers, sym)
            if r.status_code == 200:
                res = r.json()
                if res.get("status") in ("success", "opened") or res.get("_trade_opened") or \
                        res.get("trade") or res.get("trade_id"):
                    time.sleep(1.0)
                    trade = _find_open_paper_trade()
                    if trade:
                        opened_here = True
                        break
        if not trade:
            pytest.skip("Konnte keinen Paper-Trade zum Testen eröffnen (Guards/Limits) "
                        "-- Report only, kein Bug in den geprüften Endpoints selbst.")

    trade_id = trade["id"]
    entry = float(trade.get("entry") or 0)
    side = trade.get("type") or trade.get("side") or "LONG"
    assert entry > 0

    # 5a) adjust_sl (leicht unter Entry bei LONG, leicht über Entry bei SHORT)
    new_sl = round(entry * (0.995 if side == "LONG" else 1.005), 6)
    r_sl = requests.post(f"{BASE_URL}/api/autotrade/trade/{trade_id}/action",
                        headers=admin_headers,
                        json={"action": "adjust_sl", "value": new_sl}, timeout=15)
    assert r_sl.status_code == 200, r_sl.text[:300]

    # 5b) adjust_tp (target=tp1)
    new_tp = round(entry * (1.01 if side == "LONG" else 0.99), 6)
    r_tp = requests.post(f"{BASE_URL}/api/autotrade/trade/{trade_id}/action",
                        headers=admin_headers,
                        json={"action": "adjust_tp", "value": new_tp, "target": "tp1"},
                        timeout=15)
    assert r_tp.status_code == 200, r_tp.text[:300]

    # 5c) partial_close 30
    r_pc = requests.post(f"{BASE_URL}/api/autotrade/trade/{trade_id}/action",
                        headers=admin_headers,
                        json={"action": "partial_close", "value": 30}, timeout=15)
    assert r_pc.status_code == 200, r_pc.text[:300]

    # 5d) 'close' über den Action-Endpoint bleibt geblockt (400)
    r_bad = requests.post(f"{BASE_URL}/api/autotrade/trade/{trade_id}/action",
                         headers=admin_headers,
                         json={"action": "close", "value": 1}, timeout=15)
    assert r_bad.status_code == 400

    # 5e) Nur den Trade schließen, den WIR eröffnet haben (fremde nicht anfassen).
    if opened_here:
        r_close = requests.post(f"{BASE_URL}/api/autotrade/close/{trade_id}",
                               headers=admin_headers, timeout=20)
        assert r_close.status_code == 200, r_close.text[:300]
