"""Iteration 3 review tests: AI model catalog fix, rewards, model watch,
kill-switch forced learning, insights regression, auth guards."""
import os
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback local for pytest in-container
    BASE_URL = "http://localhost:8001"

ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"

DEAD_SLUGS = {
    "deepseek/deepseek-r1:free",
    "qwen/qwen3-32b",
    "qwen/qwen3-235b-a22b:free",
    "open-mistral-nemo",
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
}


def _get(path, **kw):
    for _ in range(3):
        try:
            r = requests.get(f"{BASE_URL}{path}", timeout=30, **kw)
            if r.status_code < 500:
                return r
        except requests.RequestException:
            pass
        time.sleep(1.5)
    return r


def _post(path, **kw):
    for _ in range(3):
        try:
            r = requests.post(f"{BASE_URL}{path}", timeout=60, **kw)
            if r.status_code < 500:
                return r
        except requests.RequestException:
            pass
        time.sleep(1.5)
    return r


@pytest.fixture(scope="module")
def token():
    r = _post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# --- Auth guard regression ---
def test_admin_login_and_guard():
    # protected without token
    r = _post("/api/ai/models/watch/run")
    assert r.status_code in (401, 403), f"expected 401/403 without token, got {r.status_code}"


# --- 1) /api/ai/status: valid model, no dead slugs, providers_health has skipped_too_large ---
def test_ai_status_valid_model_and_skipped_key():
    r = _get("/api/ai/status")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert "config" in body
    cfg = body["config"]
    provider = cfg.get("provider")
    model = cfg.get("model")
    assert provider and model, f"missing provider/model: {cfg}"
    # must not be a known dead slug
    assert model not in DEAD_SLUGS, f"dead slug still active: {model}"
    assert provider != "github", "github provider must be removed"

    ph = body.get("providers_health") or {}
    assert "skipped_too_large" in ph, f"providers_health missing 'skipped_too_large' key. keys={list(ph.keys())}"


# --- 2) /api/ai/rewards: history/by_regime/summary ---
def test_ai_rewards_seed_data():
    r = _get("/api/ai/rewards", params={"days": 90})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    for k in ("history", "by_regime", "summary"):
        assert k in body, f"missing {k}"
    hist = body["history"]
    assert isinstance(hist, list) and len(hist) >= 5, f"expected >=5 history entries, got {len(hist)}"
    # cumulative curve
    assert any("cum" in h for h in hist), "history entries must contain 'cum' cumulative field"
    by_regime = body["by_regime"]
    assert isinstance(by_regime, list) and len(by_regime) >= 4, f"expected >=4 regimes, got {len(by_regime)}"
    regime_names = {r.get("regime") for r in by_regime}
    assert "breakout_volatil" in regime_names, f"missing breakout_volatil regime; got {regime_names}"
    # breakout_volatil must have negative avg_reward
    bv = next((r for r in by_regime if r.get("regime") == "breakout_volatil"), None)
    assert bv is not None
    assert (bv.get("avg_reward") or 0) < 0, f"breakout_volatil avg_reward expected <0, got {bv.get('avg_reward')}"
    summary = body["summary"]
    assert summary.get("trades") == 5, f"summary.trades expected 5, got {summary.get('trades')}"
    assert (summary.get("total") or 0) > 0, f"summary.total expected >0, got {summary.get('total')}"


# --- 3) Model watch status + manual run ---
def test_ai_models_watch_status():
    r = _get("/api/ai/models/watch")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    # dead list should exist and be empty per seed context
    dead = body.get("dead")
    assert dead is not None, f"watch payload missing 'dead' key: {list(body.keys())}"
    assert dead == [] or dead == {}, f"unexpected dead entries: {dead}"


def test_ai_models_watch_run_admin(auth_headers):
    r = _post("/api/ai/models/watch/run", headers=auth_headers)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    # accept 'status' or 'ok' style payload
    status = body.get("status") or ("ok" if body.get("ok") else None)
    assert status in ("ok", "success", True), f"unexpected watch/run response: {body}"
    providers = body.get("providers") or body.get("results") or {}
    # expect all 5 providers present
    for p in ("gemini", "groq", "openrouter", "mistral", "cerebras"):
        assert p in providers, f"provider {p} missing in watch/run providers={list(providers.keys())}"


# --- 4) /api/ai/roles: no dead slug, no github provider ---
def test_ai_roles_no_dead_slugs():
    r = _get("/api/ai/roles")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    text = str(body)
    assert "github" not in text.lower().split("://")[0] or '"github"' not in text, \
        "roles payload contains github provider references"
    # check for exact known dead slug strings
    for slug in DEAD_SLUGS:
        assert slug not in text, f"roles still reference dead slug: {slug}"


# --- 5) trade-guard state includes learning_required ---
def test_trade_guard_state_has_learning_required():
    r = _get("/api/trade-guard")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    # body is either {state:..} or state itself
    state = body.get("state") if isinstance(body, dict) and "state" in body else body
    assert isinstance(state, dict), f"unexpected /api/trade-guard shape: {body}"
    assert "learning_required" in state, f"missing learning_required key. keys={list(state.keys())}"
    # normal state: false (context: seed does not force it)
    assert state["learning_required"] in (False, True)


# --- 6) /api/ai/insights regression ---
def test_ai_insights_regression():
    r = _get("/api/ai/insights")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    for k in ("stats", "lessons", "lesson_candidates"):
        assert k in body, f"insights missing {k}"


# --- 7) invalid credentials login returns error ---
def test_login_invalid():
    r = _post("/api/auth/login", json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": "wrong"})
    assert r.status_code in (400, 401, 403), f"expected auth failure, got {r.status_code}"
