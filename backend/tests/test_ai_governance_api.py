"""Live-API-Regressionstest für die neuen KI-Governance-Endpunkte
(MasterPrompt, Lektionen, Validierung, Strategie-Labor) sowie Basis-Regressionen.

WICHTIG:
* Keine POST-Aufrufe an /api/autotrade/*, die Trades öffnen/schließen (echte Keys).
* Aufräumen: MasterPrompt/Validierung werden am Ende auf Defaults zurückgesetzt,
  Testlektionen gelöscht, Testkandidat abgelehnt.
"""
import os
import time
import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def token(session):
    r = session.post(f"{BASE}/api/auth/login",
                     json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200, r.text
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def admin(session, token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {token}"})
    return s


# ---------- basic regressions ----------
def test_health(session):
    r = session.get(f"{BASE}/api/health", timeout=15)
    assert r.status_code == 200


def test_strategies_contains_ai_trader(session):
    r = session.get(f"{BASE}/api/strategies", timeout=15)
    assert r.status_code == 200
    data = r.json()
    # find any structure with 'ai_trader'
    txt = str(data).lower()
    assert "ai_trader" in txt


def test_ai_status_has_new_blocks(session):
    r = session.get(f"{BASE}/api/ai/status", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "master_prompt" in data
    assert "validation" in data
    assert "strategy_lab" in data


def test_autotrade_trades_and_config(session):
    r1 = session.get(f"{BASE}/api/autotrade/trades", timeout=15)
    r2 = session.get(f"{BASE}/api/autotrade/config", timeout=15)
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_settings(session):
    r = session.get(f"{BASE}/api/settings", timeout=15)
    assert r.status_code == 200


# ---------- MasterPrompt ----------
ORIG_MP = {}


def test_master_prompt_get(session):
    r = session.get(f"{BASE}/api/ai/master-prompt", timeout=15)
    assert r.status_code == 200
    body = r.json()
    data = body.get("master_prompt", body)
    for key in ("text", "rules", "version"):
        assert key in data, f"missing {key}"
    for k in ("max_leverage", "min_confidence", "allowed_sides", "blocked_symbols",
             "max_open_trades", "require_live_approval"):
        assert k in data["rules"], f"missing rule {k}"
    # defaults may live at top-level or inside master_prompt
    assert "defaults" in body or "defaults" in data
    ORIG_MP["text"] = data["text"]
    ORIG_MP["rules"] = data["rules"]
    ORIG_MP["version"] = data["version"]


def test_master_prompt_post_requires_admin(session):
    r = session.post(f"{BASE}/api/ai/master-prompt",
                     json={"text": "unauthorized"}, timeout=15)
    assert r.status_code in (401, 403), r.status_code


def test_master_prompt_post_admin_increments_version(admin):
    new_text = (ORIG_MP.get("text") or "") + "\n[QA_TEST_MARKER]"
    r = admin.post(f"{BASE}/api/ai/master-prompt",
                   json={"text": new_text, "rules": ORIG_MP["rules"]}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    data = body.get("master_prompt", body)
    assert data["text"] == new_text
    assert data["version"] > ORIG_MP["version"]


def test_master_prompt_history(session):
    r = session.get(f"{BASE}/api/ai/master-prompt?history=true", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "history" in data
    assert isinstance(data["history"], list)
    # after previous save, history should have >= 1 entry (previous version)
    assert len(data["history"]) >= 1, data


def test_master_prompt_restore(admin):
    r = admin.post(f"{BASE}/api/ai/master-prompt",
                   json={"text": ORIG_MP["text"], "rules": ORIG_MP["rules"]}, timeout=15)
    assert r.status_code == 200


# ---------- Lessons ----------
LESSON_ID = {}


def test_lesson_create_requires_admin(session):
    r = session.post(f"{BASE}/api/ai/lessons",
                     json={"title": "x", "detail": "y", "weight": 3}, timeout=15)
    assert r.status_code in (401, 403)


def test_lesson_create_admin(admin):
    r = admin.post(f"{BASE}/api/ai/lessons",
                   json={"title": "TEST_QA_Lesson", "detail": "SL bei 0.5% testen",
                         "weight": 3}, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    # response could be lesson or list; try both
    lesson = data.get("lesson") if isinstance(data, dict) and "lesson" in data else data
    if isinstance(lesson, list):
        lesson = [l for l in lesson if l.get("title") == "TEST_QA_Lesson"][0]
    assert lesson["origin"] == "user"
    assert lesson["locked"] is True
    assert lesson["id"].startswith("les_")
    LESSON_ID["id"] = lesson["id"]


def test_lesson_patch_admin(admin):
    lid = LESSON_ID["id"]
    r = admin.patch(f"{BASE}/api/ai/lessons/{lid}",
                    json={"detail": "SL bei 0.6% testen"}, timeout=15)
    assert r.status_code == 200, r.text
    # verify via GET
    r2 = admin.get(f"{BASE}/api/ai/lessons", timeout=15)
    lst = r2.json()
    lessons = lst.get("lessons", lst) if isinstance(lst, dict) else lst
    found = [l for l in lessons if l["id"] == lid][0]
    assert found["detail"] == "SL bei 0.6% testen"
    assert found["locked"] is True
    assert found.get("user_edited_at")


def test_lesson_patch_requires_admin(session):
    lid = LESSON_ID["id"]
    r = session.patch(f"{BASE}/api/ai/lessons/{lid}",
                      json={"detail": "hack"}, timeout=15)
    assert r.status_code in (401, 403)


def test_ai_insights_contains_lessons(session):
    r = session.get(f"{BASE}/api/ai/insights", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "master_prompt" in data
    assert "validation" in data
    # lessons somewhere
    txt = str(data)
    assert LESSON_ID["id"] in txt


def test_lesson_delete_requires_admin(session):
    lid = LESSON_ID["id"]
    r = session.delete(f"{BASE}/api/ai/lessons/{lid}", timeout=15)
    assert r.status_code in (401, 403)


def test_lesson_delete_admin(admin):
    lid = LESSON_ID["id"]
    r = admin.delete(f"{BASE}/api/ai/lessons/{lid}", timeout=15)
    assert r.status_code == 200


# ---------- Validation ----------
ORIG_VAL = {}


def test_validation_get(session):
    r = session.get(f"{BASE}/api/ai/validation", timeout=15)
    assert r.status_code == 200
    data = r.json()
    settings = data.get("settings", data)
    for k in ("enabled", "min_closed_trades", "min_symbol_trades",
             "min_lesson_results", "min_removal_results"):
        assert k in settings, f"missing {k}"
    ORIG_VAL.update(settings)


def test_validation_post_admin_and_persist(admin, session):
    new_vals = {"enabled": True, "min_closed_trades": 21, "min_symbol_trades": 9,
                "min_lesson_results": 6, "min_removal_results": 13}
    r = admin.post(f"{BASE}/api/ai/validation", json=new_vals, timeout=15)
    assert r.status_code == 200, r.text
    r2 = session.get(f"{BASE}/api/ai/validation", timeout=15)
    settings = r2.json().get("settings", r2.json())
    for k, v in new_vals.items():
        assert settings[k] == v, f"{k}: {settings[k]} != {v}"


def test_validation_restore_defaults(admin):
    defaults = {"enabled": True, "min_closed_trades": 15, "min_symbol_trades": 8,
                "min_lesson_results": 5, "min_removal_results": 12}
    r = admin.post(f"{BASE}/api/ai/validation", json=defaults, timeout=15)
    assert r.status_code == 200


# ---------- Strategy Lab ----------
CAND = {}
CAND_NAME = "TEST_QA_Candidate_LiveClose"


def test_strategies_get(session):
    r = session.get(f"{BASE}/api/ai/strategies", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "candidates" in data
    assert "status" in data
    assert "settings" in data["status"]


def test_strategies_create_admin(admin):
    r = admin.post(f"{BASE}/api/ai/strategies",
                   json={"name": CAND_NAME, "thesis": "QA test candidate",
                         "symbols": ["BTCUSDT"]}, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    cand = data.get("candidate", data)
    assert cand.get("stage") == "ghost"
    CAND["id"] = cand["id"]


def test_strategies_decide_approve(admin):
    cid = CAND["id"]
    r = admin.post(f"{BASE}/api/ai/strategies/{cid}/decide",
                   json={"action": "approve"}, timeout=15)
    assert r.status_code == 200
    stage = r.json().get("candidate", r.json()).get("stage")
    assert stage in ("paper", "live", "ghost"), stage  # settings.promote_to typically paper


def test_strategies_decide_approve_live(admin):
    cid = CAND["id"]
    r = admin.post(f"{BASE}/api/ai/strategies/{cid}/decide",
                   json={"action": "approve_live"}, timeout=15)
    assert r.status_code == 200
    assert r.json().get("candidate", r.json()).get("stage") == "live"


def test_strategies_decide_reset(admin):
    cid = CAND["id"]
    r = admin.post(f"{BASE}/api/ai/strategies/{cid}/decide",
                   json={"action": "reset"}, timeout=15)
    assert r.status_code == 200
    assert r.json().get("candidate", r.json()).get("stage") == "ghost"


def test_strategies_register_test_not_testable(admin):
    cid = CAND["id"]
    r = admin.post(f"{BASE}/api/ai/strategies/{cid}/register-test", timeout=15)
    assert r.status_code == 200
    data = r.json()
    status = data.get("status") or data.get("candidate", {}).get("test_status") or ""
    body = str(data).lower()
    assert "not_testable" in body or "not testable" in body or status == "not_testable"


def test_strategies_ghost_trades_empty(session):
    cid = CAND["id"]
    r = session.get(f"{BASE}/api/ai/strategies/ghost-trades?candidate_id={cid}", timeout=15)
    assert r.status_code == 200
    data = r.json()
    trades = data.get("ghost_trades", data.get("trades", data))
    assert isinstance(trades, list)
    assert len(trades) == 0


def test_strategies_settings_admin(admin, session):
    r = admin.post(f"{BASE}/api/ai/strategies/settings",
                   json={"min_ghost_trades": 22, "min_ghost_winrate": 58.0,
                         "promote_to": "paper"}, timeout=15)
    assert r.status_code == 200
    r2 = session.get(f"{BASE}/api/ai/strategies", timeout=15)
    s = r2.json()["status"]["settings"]
    assert s["min_ghost_trades"] == 22
    assert abs(s["min_ghost_winrate"] - 58.0) < 0.01
    assert s["promote_to"] == "paper"


def test_strategies_reject_cleanup(admin):
    cid = CAND["id"]
    r = admin.post(f"{BASE}/api/ai/strategies/{cid}/decide",
                   json={"action": "reject"}, timeout=15)
    assert r.status_code == 200
    assert r.json().get("candidate", r.json()).get("stage") == "rejected"
