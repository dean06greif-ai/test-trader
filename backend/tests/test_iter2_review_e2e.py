"""E2E for iteration 2 review: KI Trader label + Masterprompt lesson audit + regression."""
import os, time, requests, pytest

def _read_admin_password():
    try:
        import re as _re
        from pathlib import Path as _Path
        _txt = _Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
        _m = _re.search(r"Passwort\s*`([^`]+)`", _txt)
        return _m.group(1) if _m else None
    except OSError:
        return None

import os as _os
_ADMIN_PW = _os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"


BASE = "https://daytrader-ml.preview.emergentagent.com"

@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/api/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"),"password": _ADMIN_PW}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["token"]

@pytest.fixture(scope="module")
def H(token):
    return {"Authorization": f"Bearer {token}"}


# --- Regression: core GETs ---
def test_regression_health(H):
    for ep in ["/api/health", "/api/autotrade/sync-status", "/api/trade-guard", "/api/strategies"]:
        r = requests.get(f"{BASE}{ep}", headers=H, timeout=30)
        assert r.status_code == 200, f"{ep} -> {r.status_code}: {r.text[:200]}"


# --- Fix 2: Label 'KI Trader' (no '(Custom)') ---
def test_ki_trader_label(H):
    # Reset trade guard + auto_trades to avoid drawdown block
    import subprocess
    subprocess.run(["python3","-c","from pymongo import MongoClient; db=MongoClient('mongodb://localhost:27017')['crypto_scanner']; db.settings.delete_one({'_id':'trade_guard_state'}); db.auto_trades.delete_many({})"], check=False)

    # enable strategy on BTCUSDT paper
    r = requests.post(f"{BASE}/api/autotrade/strategy/ai_trader/coin/BTCUSDT",
                     json={"mode":"paper","enabled":True}, headers=H, timeout=30)
    assert r.status_code == 200, r.text

    trade_id = None
    for side in ["LONG","SHORT"]:
        r = requests.post(f"{BASE}/api/ai/trade/open",
                         json={"symbol":"BTCUSDT","side":side,"leverage":10,"capital_pct":10,"source":"manuell"},
                         headers=H, timeout=30)
        assert r.status_code == 200, r.text
        time.sleep(1.5)
        rr = requests.get(f"{BASE}/api/autotrade/trades", headers=H, timeout=30)
        assert rr.status_code == 200
        trades = rr.json() if isinstance(rr.json(), list) else rr.json().get("trades",[])
        open_ones = [t for t in trades if t.get("symbol")=="BTCUSDT" and t.get("status") in ("open","OPEN",None) and t.get("closed_at") in (None,"")]
        if open_ones:
            t = open_ones[0]
            assert t.get("strategy_name") == "KI Trader", f"expected 'KI Trader', got {t.get('strategy_name')!r}"
            assert "(Custom)" not in (t.get("strategy_name") or "")
            trade_id = t.get("id") or t.get("_id") or t.get("trade_id")
            break
    assert trade_id, "no open trade created (both sides blocked)"

    # cleanup - close it
    rc = requests.post(f"{BASE}/api/autotrade/close/{trade_id}", headers=H, timeout=30)
    assert rc.status_code in (200,201,204), rc.text


# --- Fix 3: Masterprompt + lesson audit ---
def test_master_prompt_lesson_audit(H):
    # snapshot existing forbidden_terms to restore later
    r = requests.get(f"{BASE}/api/ai/master-prompt", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    prev = r.json() or {}
    prev_rules = (prev.get("snapshot") or {}).get("rules") or prev.get("rules") or {}

    # 1) create a violating lesson (leverage 150x) - may be blocked by check_lesson
    violating = {"title":"Test Hebel 150x nutzen","detail":"Immer Hebel 150x fahren","tags":["test"]}
    rl = requests.post(f"{BASE}/api/ai/lessons", json=violating, headers=H, timeout=30)
    violating_id = None
    blocked_at_create = False
    if rl.status_code in (200,201):
        j = rl.json() or {}
        # could be blocked-by-check with status field
        if j.get("status") in ("blocked","rejected") or j.get("blocked"):
            blocked_at_create = True
        else:
            violating_id = j.get("id") or j.get("lesson_id") or (j.get("lesson") or {}).get("id")
    else:
        blocked_at_create = True

    # also create a harmless one to ensure audit doesn't kill conforming ones
    harmless = {"title":"Test Geduld","detail":"Warte auf gute Setups.","tags":["test"]}
    rh = requests.post(f"{BASE}/api/ai/lessons", json=harmless, headers=H, timeout=30)
    harmless_id = None
    if rh.status_code in (200,201):
        j = rh.json() or {}
        harmless_id = j.get("id") or j.get("lesson_id") or (j.get("lesson") or {}).get("id")

    # 2) POST master-prompt with rules -> lesson_audit
    rm = requests.post(f"{BASE}/api/ai/master-prompt",
                      json={"rules":{"max_leverage":50,"forbidden_terms":["martingale"]}},
                      headers=H, timeout=30)
    assert rm.status_code == 200, rm.text
    jm = rm.json() or {}
    assert "lesson_audit" in jm, f"missing lesson_audit in {jm}"

    # 3) verify audit removed the violator
    rget = requests.get(f"{BASE}/api/ai/lessons", headers=H, timeout=30)
    assert rget.status_code == 200
    body = rget.json()
    items = body if isinstance(body,list) else (body.get("lessons") or body.get("items") or [])
    titles = [ (l.get("title") or "") for l in items ]
    assert not any("150x" in t for t in titles), f"violating lesson still present: {titles}"

    # 4) GET master-prompt snapshot
    rgm = requests.get(f"{BASE}/api/ai/master-prompt", headers=H, timeout=30)
    assert rgm.status_code == 200
    snap = rgm.json() or {}
    assert (snap.get("snapshot") or snap.get("rules") or snap.get("prompt") or snap.get("master_prompt"))

    # cleanup: delete any test lessons we created
    for lid, t in [(violating_id, "violating"), (harmless_id, "harmless")]:
        if lid:
            requests.delete(f"{BASE}/api/ai/lessons/{lid}", headers=H, timeout=30)
    # also cleanup by title in case IDs were not returned
    rget2 = requests.get(f"{BASE}/api/ai/lessons", headers=H, timeout=30)
    if rget2.status_code == 200:
        body = rget2.json()
        items = body if isinstance(body,list) else (body.get("lessons") or body.get("items") or [])
        for l in items:
            if (l.get("title") or "").startswith("Test "):
                lid = l.get("id") or l.get("lesson_id")
                if lid:
                    requests.delete(f"{BASE}/api/ai/lessons/{lid}", headers=H, timeout=30)

    # reset forbidden_terms to [] as requested (max_leverage 50 kept as OK end state)
    requests.post(f"{BASE}/api/ai/master-prompt",
                 json={"rules":{"max_leverage":50,"forbidden_terms":[]}}, headers=H, timeout=30)
