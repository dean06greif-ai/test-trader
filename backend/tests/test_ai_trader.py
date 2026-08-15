"""
Tests for the new 'KI Trader' AI strategy endpoints and regression checks.

Notes:
- The expensive POST /api/ai/analyze endpoint is NOT re-triggered here (main
  agent already ran it - decisions and chat history are pre-seeded, LLM calls
  cost credits). The pre-existing decisions in /api/ai/status are asserted
  instead. If missing, the analyze-test is skipped.
- Only ONE chat message is sent (SSE streaming validation).
- Config is restored to enabled=False at the end (test-suite fixture).
"""
import os
import json
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASS},
                      timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ---------- Strategies contract ----------
class TestStrategiesRegistry:
    def test_strategies_contains_ai_trader(self):
        r = requests.get(f"{BASE_URL}/api/strategies", timeout=15)
        assert r.status_code == 200
        data = r.json()
        strategies = data.get("strategies") or data
        # Support either list or dict payload
        if isinstance(strategies, dict):
            items = list(strategies.values())
            ids = list(strategies.keys())
        else:
            items = strategies
            ids = [s.get("id") for s in strategies]
        assert "ai_trader" in ids, f"ai_trader missing. ids={ids}"

        ai = next((s for s in items if (s.get("id") == "ai_trader" or s.get("strategy_id") == "ai_trader")), None)
        assert ai is not None
        name = ai.get("name") or ai.get("display_name") or ""
        assert "KI" in name or "AI" in name, f"unexpected name: {name}"
        # Parameterless
        params = ai.get("params") or ai.get("parameters") or {}
        assert not params, f"ai_trader should be parameterless, got {params}"
        # is_ai flag
        assert ai.get("is_ai") is True or ai.get("ai") is True, f"is_ai flag missing: {ai}"
        # enabled list ist eine veränderliche Nutzer-Einstellung (andere Tests
        # / der Trader schalten Strategien um) -> nur Registry-Invarianten prüfen
        enabled = data.get("enabled") if isinstance(data, dict) else None
        if enabled is not None:
            assert isinstance(enabled, list)


# ---------- AI Status ----------
class TestAIStatus:
    def test_status_shape(self):
        r = requests.get(f"{BASE_URL}/api/ai/status", timeout=15)
        assert r.status_code == 200
        d = r.json()
        cfg = d.get("config", {})
        for k in ("enabled", "interval_min", "min_confidence", "provider", "model", "news_enabled", "cooldown_min"):
            assert k in cfg, f"config missing {k}"
        if not d.get("has_key"):
            pytest.skip("Kein GEMINI_API_KEY in dieser Umgebung gesetzt")
        assert "decisions" in d and isinstance(d["decisions"], dict)


# ---------- AI Config (auth guard + persistence) ----------
class TestAIConfig:
    def test_config_requires_admin(self):
        r = requests.post(f"{BASE_URL}/api/ai/config",
                          json={"enabled": False},
                          timeout=15)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"

    def test_config_update_and_persist(self, admin_headers):
        # read current
        cur = requests.get(f"{BASE_URL}/api/ai/status", timeout=15).json()["config"]
        # toggle a benign field: min_confidence  (do NOT enable auto-trade)
        new_conf = 70 if cur["min_confidence"] != 70 else 68
        r = requests.post(f"{BASE_URL}/api/ai/config",
                          headers=admin_headers,
                          json={"min_confidence": new_conf,
                                "interval_min": 10,
                                "model": cur["model"],
                                "enabled": False},
                          timeout=15)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        assert r.json().get("config", {}).get("min_confidence") == new_conf
        # re-read
        r2 = requests.get(f"{BASE_URL}/api/ai/status", timeout=15).json()
        assert r2["config"]["min_confidence"] == new_conf
        assert r2["config"]["enabled"] is False


# ---------- AI Analyze (skip actual call; verify seeded state) ----------
class TestAIAnalyzeSeed:
    def test_decisions_present(self):
        d = requests.get(f"{BASE_URL}/api/ai/status", timeout=15).json()
        decisions = d.get("decisions", {})
        if not decisions:
            pytest.skip("No decisions seeded (analysis has not been run yet)")
        # Expect around 13 decisions per task spec
        assert len(decisions) >= 5, f"decisions too few: {len(decisions)}"
        btc = decisions.get("BTCUSDT")
        assert btc is not None, "BTCUSDT decision missing"
        for k in ("action", "confidence", "reasoning"):
            assert k in btc, f"decision missing {k}: {btc}"
        assert isinstance(btc["confidence"], (int, float))
        # German reasoning heuristic - not strict
        assert isinstance(btc["reasoning"], str) and len(btc["reasoning"]) > 0


# ---------- AI Chat history + analysis message ----------
class TestAIChatHistory:
    def test_history_contains_analysis(self):
        r = requests.get(f"{BASE_URL}/api/ai/chat/history?limit=100", timeout=15)
        assert r.status_code == 200
        msgs = r.json().get("messages", [])
        if not msgs:
            pytest.skip("No chat history yet")
        roles = {m.get("role") for m in msgs}
        assert "analysis" in roles, f"'analysis' role missing (got {roles})"


# ---------- AI Chat SSE streaming ----------
class TestAIChatStream:
    def test_chat_streams_sse(self, admin_headers):
        # One short message to minimize LLM cost
        with requests.post(f"{BASE_URL}/api/ai/chat",
                           headers=admin_headers,
                           json={"message": "Kurz: Wie ist die Marktlage?"},
                           stream=True, timeout=90) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            tokens = []
            saw_done = False
            deadline = time.time() + 90
            for line in resp.iter_lines(decode_unicode=True):
                if time.time() > deadline:
                    break
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if "t" in obj:
                    tokens.append(obj["t"])
                if obj.get("done"):
                    saw_done = True
                    break
            assert saw_done, "did not receive done event"
            assert len(tokens) > 0, "no tokens streamed"
            joined = "".join(tokens)
            assert len(joined) > 5, f"reply too short: {joined!r}"


# ---------- AI News ----------
class TestAINews:
    def test_news_headlines(self):
        r = requests.get(f"{BASE_URL}/api/ai/news?limit=10", timeout=30)
        assert r.status_code == 200
        headlines = r.json().get("headlines", [])
        assert isinstance(headlines, list)
        # RSS may occasionally be empty; warn but don't hard-fail if 0
        if not headlines:
            pytest.skip("news feed returned empty (external RSS transient)")
        first = headlines[0]
        assert "title" in first or "text" in first or isinstance(first, str)


# ---------- Chat clear (auth guard) ----------
class TestAIChatClearAuth:
    def test_clear_requires_admin(self):
        r = requests.delete(f"{BASE_URL}/api/ai/chat", timeout=15)
        assert r.status_code == 401


# ---------- Regression: legacy endpoints unaffected ----------
class TestRegression:
    @pytest.mark.parametrize("path", [
        "/api/signals",
        "/api/performance",
        "/api/settings",
        "/api/autotrade/config",
        "/api/strategies",
    ])
    def test_get_endpoint_ok(self, path):
        r = requests.get(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        # must be JSON-parseable
        r.json()

    def test_auth_login_regression(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"username": ADMIN_USER, "password": ADMIN_PASS},
                          timeout=15)
        assert r.status_code == 200
        assert "token" in r.json()


# ---------- Final teardown - ensure enabled=False as requested ----------
@pytest.fixture(scope="session", autouse=True)
def restore_ai_disabled():
    yield
    try:
        tok = requests.post(f"{BASE_URL}/api/auth/login",
                            json={"username": ADMIN_USER, "password": ADMIN_PASS},
                            timeout=15).json().get("token")
        if tok:
            requests.post(f"{BASE_URL}/api/ai/config",
                          headers={"Authorization": f"Bearer {tok}",
                                   "Content-Type": "application/json"},
                          json={"enabled": False},
                          timeout=15)
    except Exception:
        pass
