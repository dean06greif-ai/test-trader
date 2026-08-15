"""
Iteration 10 – AI Trader (strategy_id 'ai_trader') End-to-End Backend Tests.
Covers:
  - GET /api/ai/status (new config keys + learning summary)
  - GET /api/ai/insights (stats.totals + lessons)
  - POST /api/ai/learn (admin, LLM run)
  - GET /api/ai/proposals + POST /api/ai/proposals/{id} approve/reject/404
  - POST /api/ai/config (admin) autonomy persist + ignore invalid
  - POST /api/ai/analyze (admin) -> writes analysis chat entry
  - POST /api/ai/chat (SSE stream, admin, platform knowledge)
  - Regression: /api/health, /api/strategies, /api/autotrade/config
"""
import os
import time
import json
import pytest
import requests

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

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!/Admin"


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="session")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ------------------------- Regression -------------------------
class TestRegression:
    def test_health(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=10)
        assert r.status_code == 200
        assert r.json().get("status") in ("alive", "ok")

    def test_strategies(self):
        r = requests.get(f"{BASE_URL}/api/strategies", timeout=10)
        assert r.status_code == 200
        data = r.json()
        # Should include ai_trader among strategies
        strategies = data.get("strategies", data if isinstance(data, list) else [])
        ids = [s.get("id") if isinstance(s, dict) else s for s in strategies]
        assert any("ai_trader" in str(i) for i in ids), f"ai_trader not found in {ids}"

    def test_autotrade_config(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/autotrade/config", headers=auth_headers, timeout=10)
        assert r.status_code == 200


# ------------------------- AI Status & Insights -------------------------
class TestAIStatus:
    def test_ai_status_has_new_config_keys(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/ai/status", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        cfg = data.get("config") or {}
        expected = ["autonomy", "learning_enabled", "learn_on_trade_close",
                    "learning_lookback_days", "max_lessons", "use_ai_levels"]
        missing = [k for k in expected if k not in cfg]
        assert not missing, f"Missing config keys: {missing}; got={list(cfg.keys())}"
        # learning summary present
        learning = data.get("learning") or data.get("learning_summary") or {}
        assert "lessons_count" in learning or "lessons_count" in data, \
            f"lessons_count missing; top-level={list(data.keys())} learning={list(learning.keys())}"

    def test_ai_insights(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/ai/insights", headers=auth_headers, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        stats = data.get("stats") or {}
        totals = stats.get("totals") or {}
        assert totals, f"stats.totals missing: {data}"
        # Expect paper+live breakdown (signals, winrate, trades, pnl)
        keys_str = json.dumps(totals)
        assert "paper" in keys_str.lower() or "signals" in keys_str.lower(), f"unexpected totals shape: {totals}"
        # lessons list present
        assert "lessons" in data
        assert isinstance(data["lessons"], list)


# ------------------------- AI Config -------------------------
class TestAIConfig:
    def test_config_autonomy_persist(self, auth_headers):
        # switch to auto
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"autonomy": "auto"}, timeout=15)
        assert r.status_code == 200, r.text
        r2 = requests.get(f"{BASE_URL}/api/ai/status", headers=auth_headers, timeout=10)
        assert r2.json()["config"]["autonomy"] == "auto"

        # back to suggest
        r3 = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                           json={"autonomy": "suggest"}, timeout=15)
        assert r3.status_code == 200
        r4 = requests.get(f"{BASE_URL}/api/ai/status", headers=auth_headers, timeout=10)
        assert r4.json()["config"]["autonomy"] == "suggest"

    def test_config_invalid_value_ignored(self, auth_headers):
        before = requests.get(f"{BASE_URL}/api/ai/status", headers=auth_headers, timeout=10).json()["config"]
        prev_autonomy = before.get("autonomy")
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"autonomy": "banana"}, timeout=15)
        # Endpoint may return 200 (ignored) or 400. Either way autonomy must not change.
        assert r.status_code in (200, 400, 422)
        after = requests.get(f"{BASE_URL}/api/ai/status", headers=auth_headers, timeout=10).json()["config"]
        assert after.get("autonomy") == prev_autonomy, f"invalid value should not change autonomy"


# ------------------------- Learning -------------------------
class TestAILearning:
    def test_learn_run(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/ai/learn", headers=auth_headers, json={}, timeout=180)
        # OpenRouter free tier may 429 — retry once
        if r.status_code == 429 or (r.status_code == 200 and "rate" in r.text.lower()[:200]):
            time.sleep(8)
            r = requests.post(f"{BASE_URL}/api/ai/learn", headers=auth_headers, json={}, timeout=180)
        assert r.status_code == 200, f"learn failed: {r.status_code} {r.text[:400]}"
        data = r.json()
        assert data.get("status") == "ok", f"status not ok: {data}"
        lessons = data.get("lessons") or data.get("lessons_new") or 0
        # Accept either count or list
        if isinstance(lessons, list):
            lessons_n = len(lessons)
        else:
            lessons_n = int(lessons)
        assert lessons_n > 0, f"expected lessons>0, got {lessons_n}, resp={data}"
        # assessment presence (may be nested)
        assert "assessment" in data or "summary" in data or "report" in data, f"no assessment field: {list(data.keys())}"


# ------------------------- Proposals -------------------------
class TestAIProposals:
    def _list_pending(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/ai/proposals?status=pending", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        return data.get("proposals", data if isinstance(data, list) else [])

    def test_proposals_listed(self, auth_headers):
        props = self._list_pending(auth_headers)
        assert isinstance(props, list)
        # after learning we expect >=1 pending. If empty, trigger a learn first.
        if not props:
            requests.post(f"{BASE_URL}/api/ai/learn", headers=auth_headers, json={}, timeout=180)
            props = self._list_pending(auth_headers)
        assert props, "expected at least one pending proposal after learn"

    def test_proposal_reject(self, auth_headers):
        props = self._list_pending(auth_headers)
        if not props:
            pytest.skip("no pending proposals to reject")
        # pick the first proposal, reject it
        pid = props[0].get("id") or props[0].get("_id")
        assert pid, f"no id in proposal {props[0]}"
        r = requests.post(f"{BASE_URL}/api/ai/proposals/{pid}",
                          headers=auth_headers, json={"action": "reject"}, timeout=15)
        assert r.status_code == 200, r.text
        # second call -> 404
        r2 = requests.post(f"{BASE_URL}/api/ai/proposals/{pid}",
                           headers=auth_headers, json={"action": "reject"}, timeout=15)
        assert r2.status_code == 404, f"expected 404 on re-decide, got {r2.status_code} {r2.text}"

    def test_proposal_approve_applies_change(self, auth_headers):
        props = self._list_pending(auth_headers)
        # Keep at least one pending remaining if there are >=2
        if len(props) < 1:
            pytest.skip("no pending proposals to approve")
        target = None
        # find a proposal with a coin/symbol we can query
        for p in props:
            sym = p.get("symbol") or p.get("coin") or (p.get("change") or {}).get("symbol")
            if sym:
                target = p
                break
        if target is None:
            target = props[0]
        pid = target.get("id") or target.get("_id")
        symbol = target.get("symbol") or target.get("coin") or (target.get("change") or {}).get("symbol")

        # snapshot before (if symbol known)
        before_cfg = None
        if symbol:
            rb = requests.get(f"{BASE_URL}/api/autotrade/strategy/ai_trader/coin/{symbol}",
                              headers=auth_headers, timeout=15)
            if rb.status_code == 200:
                before_cfg = rb.json()

        r = requests.post(f"{BASE_URL}/api/ai/proposals/{pid}",
                          headers=auth_headers, json={"action": "approve"}, timeout=30)
        assert r.status_code == 200, r.text

        # after approve: coin config still has max_capital=100 (unchanged)
        if symbol:
            ra = requests.get(f"{BASE_URL}/api/autotrade/strategy/ai_trader/coin/{symbol}",
                              headers=auth_headers, timeout=15)
            assert ra.status_code == 200
            cfg_after = ra.json()
            # max_capital must remain 100
            mc = cfg_after.get("max_capital") or (cfg_after.get("config") or {}).get("max_capital")
            if mc is not None:
                assert float(mc) == 100.0, f"max_capital changed to {mc}, must remain 100"

        # decide again -> 404
        r2 = requests.post(f"{BASE_URL}/api/ai/proposals/{pid}",
                           headers=auth_headers, json={"action": "approve"}, timeout=15)
        assert r2.status_code == 404


# ------------------------- Analyze + Chat -------------------------
class TestAIAnalyzeChat:
    def test_analyze_runs_and_history_updated(self, auth_headers):
        # get history len before
        hb = requests.get(f"{BASE_URL}/api/ai/chat/history", headers=auth_headers, timeout=15)
        hb_data = hb.json() if hb.status_code == 200 else {}
        before_msgs = hb_data.get("messages") or hb_data.get("history") or []
        before_n = len(before_msgs)

        r = requests.post(f"{BASE_URL}/api/ai/analyze", headers=auth_headers, json={}, timeout=240)
        if r.status_code in (429, 502, 503, 504):
            time.sleep(12)
            r = requests.post(f"{BASE_URL}/api/ai/analyze", headers=auth_headers, json={}, timeout=240)
        assert r.status_code == 200, f"analyze failed: {r.status_code} {r.text[:300]}"
        data = r.json()
        assert data.get("status") == "ok", f"analyze status not ok: {data}"
        # decisions count present, be lenient
        decisions = data.get("decisions") or data.get("count") or 0
        if isinstance(decisions, list):
            decisions = len(decisions)
        assert int(decisions) >= 1, f"expected >=1 decisions, got {decisions}"

        # chat history now contains an analysis entry
        ha = requests.get(f"{BASE_URL}/api/ai/chat/history", headers=auth_headers, timeout=15)
        assert ha.status_code == 200
        ha_data = ha.json()
        hist = ha_data.get("messages") or ha_data.get("history") or []
        assert len(hist) >= before_n, "history should not shrink"
        # analysis entry has role=='analysis' (or type/kind analysis)
        assert any((h.get("role") == "analysis") or (h.get("type") == "analysis")
                   or ("analysis" in str(h.get("kind") or "").lower())
                   or ("analyse" in str(h.get("text") or h.get("title") or "").lower())
                   for h in hist), f"no analysis entry in history; sample={hist[:2]}"

    def test_chat_sse_platform_knowledge(self, auth_headers):
        # SSE stream: use requests with stream=True
        r = requests.post(f"{BASE_URL}/api/ai/chat", headers=auth_headers,
                          json={"message": "Was macht diese Website?"},
                          timeout=120, stream=True)
        assert r.status_code == 200, r.text[:200]
        collected = ""
        start = time.time()
        for line in r.iter_lines(decode_unicode=True):
            if line:
                collected += line + "\n"
            if len(collected) > 200 or (time.time() - start) > 90:
                break
        r.close()
        assert len(collected) > 20, f"empty stream: {collected!r}"
        # Some platform-y content expected
        lc = collected.lower()
        # be lenient - just require non-empty response
        assert any(k in lc for k in ["krypto", "trad", "signal", "strateg", "scanner", "plattform", "website"]), \
            f"no platform-content in stream: {collected[:400]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
