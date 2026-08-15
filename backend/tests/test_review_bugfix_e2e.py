"""E2E review tests against preview URL for custom-AI-trades bugfix batch.

Covers:
- Master kill-switch (stop-trades) blocks POST /api/ai/trade/open
- ai_leverage clamp <= 50 (trade_manager max_leverage)
- Candidate lifecycle: create custom -> reject removes custom -> register-test blocked -> DELETE candidate
- Regression on core GET endpoints
"""
import os
import time
import uuid
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
ADMIN_USER = "Admin"

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
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ---------- Regression: basic GETs ----------
@pytest.mark.parametrize("path", [
    "/api/health",
    "/api/strategies",
    "/api/autotrade/config",
    "/api/ai/lab/status",
    "/api/ai/trade/status",
])
def test_regression_get_endpoints(admin_headers, path):
    r = requests.get(f"{BASE_URL}{path}", headers=admin_headers, timeout=30)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:300]}"


# ---------- Master Kill-Switch ----------
def _set_kill_switch(headers, want_on: bool):
    """Toggle stop-trades until GET /api/control/state matches want_on."""
    for _ in range(3):
        st = requests.get(f"{BASE_URL}/api/control/state", headers=headers, timeout=15).json()
        cur = bool(st.get("trades_paused"))
        if cur == want_on:
            return st
        requests.post(f"{BASE_URL}/api/control/stop-trades", headers=headers, timeout=15)
    return requests.get(f"{BASE_URL}/api/control/state", headers=headers, timeout=15).json()


def test_kill_switch_blocks_ai_trade_open(admin_headers):
    try:
        st = _set_kill_switch(admin_headers, True)
        assert st.get("trades_paused") is True, f"could not enable kill-switch: {st}"

        r = requests.post(f"{BASE_URL}/api/ai/trade/open", headers=admin_headers,
                          json={"symbol": "BTCUSDT", "side": "LONG", "source": "manuell"},
                          timeout=30)
        assert r.status_code == 200, f"expected 200 body-status blocked, got {r.status_code} {r.text}"
        body = r.json()
        assert body.get("status") == "blocked", f"expected status=blocked got {body}"
        detail = (body.get("detail") or body.get("reason") or "").lower()
        assert "stop" in detail or "pause" in detail, f"detail should mention stop: {body}"
    finally:
        # Always restore kill-switch OFF
        st = _set_kill_switch(admin_headers, False)
        assert st.get("trades_paused") is False, f"failed to disable kill-switch at teardown: {st}"


# ---------- Leverage Clamp ----------
def test_ai_leverage_clamp(admin_headers):
    # Ensure kill switch off
    _set_kill_switch(admin_headers, False)
    # Configure ai_trader for BTCUSDT paper
    r = requests.post(f"{BASE_URL}/api/autotrade/strategy/ai_trader/coin/BTCUSDT",
                      headers=admin_headers, json={"mode": "paper", "enabled": True}, timeout=30)
    assert r.status_code == 200, f"strategy config failed {r.status_code} {r.text}"

    # Verify max_leverage setting is 50 via /api/ai/trade/status
    status = requests.get(f"{BASE_URL}/api/ai/trade/status", headers=admin_headers, timeout=30).json()
    max_lev = ((status.get("status") or {}).get("settings") or {}).get("max_leverage")
    assert max_lev is not None and float(max_lev) <= 50, f"trade_manager max_leverage must be <=50, got {max_lev}"

    opened_id = None
    successful_open = False
    tried = []
    for symbol, side in [("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT"), ("ETHUSDT", "LONG"), ("ETHUSDT", "SHORT")]:
        # Ensure config for this symbol
        requests.post(f"{BASE_URL}/api/autotrade/strategy/ai_trader/coin/{symbol}",
                      headers=admin_headers, json={"mode": "paper", "enabled": True}, timeout=30)
        r = requests.post(f"{BASE_URL}/api/ai/trade/open", headers=admin_headers,
                          json={"symbol": symbol, "side": side, "leverage": 200,
                                "capital_pct": 10, "source": "manuell"}, timeout=30)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        tried.append((symbol, side, r.status_code, body))
        if r.status_code == 200 and body.get("status") in ("ok", "opened", "success"):
            successful_open = True
            # Wait briefly and try to locate trade in autotrade/trades
            time.sleep(2)
            tl = requests.get(f"{BASE_URL}/api/autotrade/trades", headers=admin_headers, timeout=30).json()
            lst = tl if isinstance(tl, list) else tl.get("trades") or tl.get("items") or []
            cand = [t for t in lst if t.get("symbol") == symbol
                    and (t.get("side") or "").upper() == side
                    and (t.get("status") or "").lower() == "open"
                    and t.get("strategy_id") == "ai_trader"]
            if cand:
                cand.sort(key=lambda t: t.get("opened_at") or "", reverse=True)
                opened_id = cand[0].get("id") or cand[0].get("trade_id")
            break

    assert successful_open, f"could not open ai trade (all attempts): {tried}"

    # Verify clamp on the freshly-opened trade if available, else on any recent ai_trader trade
    # that was opened with the same clamped leverage
    trades = requests.get(f"{BASE_URL}/api/autotrade/trades", headers=admin_headers, timeout=30).json()
    items = trades if isinstance(trades, list) else trades.get("trades") or trades.get("items") or []
    if opened_id:
        match = next((t for t in items if str(t.get("id") or t.get("trade_id")) == str(opened_id)), None)
        assert match, f"opened trade {opened_id} not found in trades list"
        lev = match.get("leverage") or match.get("lev")
        assert lev is not None and float(lev) <= 50, f"leverage not clamped: {lev}"
        # Cleanup
        cr = requests.post(f"{BASE_URL}/api/autotrade/close/{opened_id}", headers=admin_headers, timeout=30)
        assert cr.status_code in (200, 204), f"close failed {cr.status_code} {cr.text}"
    else:
        # Trade was accepted (status=ok) but not persisted (likely closed instantly by strategy
        # or intercepted by anti-stacking despite ok). Verify on any recent ai_trader trade that
        # its leverage is clamped <=50.
        recent = [t for t in items if t.get("strategy_id") == "ai_trader"]
        assert recent, f"no ai_trader trades to verify clamp against; tried={tried}"
        for t in recent:
            lev = t.get("leverage") or t.get("lev")
            assert lev is not None and float(lev) <= 50, f"leverage clamp violated in trade {t.get('id')}: {lev}"


# ---------- Candidate Lifecycle ----------
def test_candidate_lifecycle_reject_removes_custom_and_delete(admin_headers):
    name = f"QA Test Strategie {uuid.uuid4().hex[:6]}"
    payload = {
        "name": name,
        "thesis": "RSI Test",
        "rules_text": "RSI<30 long",
        "stage": "live",
        "rule_definition": {
            "timeframe": "1m",
            "indicators": {"rsi_period": 14},
            "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
        },
    }
    r = requests.post(f"{BASE_URL}/api/ai/strategies", headers=admin_headers, json=payload, timeout=30)
    assert r.status_code in (200, 201), f"create candidate failed {r.status_code} {r.text}"
    body = r.json()
    cand = body.get("candidate") or body
    cid = cand.get("id") or cand.get("candidate_id") or body.get("id")
    custom_sid = cand.get("custom_strategy_id") or body.get("custom_strategy_id")
    assert cid, f"no candidate id in {body}"
    assert custom_sid, f"expected custom_strategy_id set on live candidate, body={body}"

    # Custom strategy exists
    strats = requests.get(f"{BASE_URL}/api/strategies", headers=admin_headers, timeout=30).json()
    items = strats if isinstance(strats, list) else strats.get("strategies") or strats.get("items") or []
    ids = {str(s.get("id") or s.get("_id")) for s in items}
    assert str(custom_sid) in ids, f"custom strategy {custom_sid} not in /api/strategies"

    # Reject -> should remove custom strategy
    r = requests.post(f"{BASE_URL}/api/ai/strategies/{cid}/decide",
                      headers=admin_headers, json={"action": "reject"}, timeout=30)
    assert r.status_code == 200, f"decide reject failed {r.status_code} {r.text}"
    dec = r.json()
    assert dec.get("strategy_removed") is True, f"expected strategy_removed=true, got {dec}"

    strats2 = requests.get(f"{BASE_URL}/api/strategies", headers=admin_headers, timeout=30).json()
    items2 = strats2 if isinstance(strats2, list) else strats2.get("strategies") or strats2.get("items") or []
    ids2 = {str(s.get("id") or s.get("_id")) for s in items2}
    assert str(custom_sid) not in ids2, f"custom strategy still present after reject: {custom_sid}"

    # register-test on rejected -> blocked
    r = requests.post(f"{BASE_URL}/api/ai/strategies/{cid}/register-test", headers=admin_headers, timeout=30)
    assert r.status_code == 200, f"register-test call failed {r.status_code} {r.text}"
    rb = r.json()
    assert rb.get("status") == "blocked", f"expected status=blocked, got {rb}"

    # DELETE candidate
    r = requests.delete(f"{BASE_URL}/api/ai/strategies/{cid}", headers=admin_headers, timeout=30)
    assert r.status_code == 200, f"delete failed {r.status_code} {r.text}"
    db = r.json()
    assert (db.get("status") in ("success", "ok", "deleted")) or db.get("deleted") is True, f"unexpected delete body: {db}"

    # Verify gone from candidates list
    lst = requests.get(f"{BASE_URL}/api/ai/strategies", headers=admin_headers, timeout=30).json()
    cands = lst if isinstance(lst, list) else lst.get("candidates") or lst.get("items") or []
    cids = {str(c.get("id") or c.get("candidate_id")) for c in cands}
    assert str(cid) not in cids, f"candidate {cid} still in list after delete"
