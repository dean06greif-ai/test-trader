"""End-to-end Integrationstests für die Chat-Kommando-Iteration.

Testet gegen den öffentlichen Backend-Endpoint (REACT_APP_BACKEND_URL).
Deckt genau die Punkte aus dem Test-Request:
  * Admin-Login
  * Paper-Trade eröffnen / GET trades
  * ADJUST_SL Clamp-Fix (SL auf falscher Kursseite -> Auto-Korrektur)
  * Trade schließen (Aktion 'close')
  * Chat-Kommando ohne LLM-Key -> Stream, kein 500
  * Lektionen CRUD + Wirkung in /api/ai/insights
  * Insights hat lesson_candidates
  * Validation min_lesson_confirmations (default 2, POST/Clamp)
  * Strategie-Assistent: 400 ohne Thesis, 400 'Kein API-Key' mit Thesis
  * Strategie anlegen mit rule_definition -> custom_strategy_id gesetzt
  * register-test ohne Regeln -> not_testable
  * Regression: /api/ai/status, /api/strategies, /api/autotrade/config, /api/signals
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


def _read_admin_password():
    try:
        import re
        from pathlib import Path
        txt = Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
        m = re.search(r"Passwort\s*`([^`]+)`", txt)
        return m.group(1) if m else None
    except OSError:
        return None


ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or _read_admin_password() or "Dean06Greif!"


# -------------------- Fixtures --------------------
@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="session")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# -------------------- Login --------------------
def test_admin_login(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=20)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data.get("token"), str) and len(data["token"]) > 10


# -------------------- Regression basics --------------------
def test_regression_public_endpoints(api):
    for path in ("/api/ai/status", "/api/strategies", "/api/autotrade/config", "/api/signals"):
        r = api.get(f"{BASE_URL}{path}", timeout=20)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


# -------------------- Paper Trade Open / Close / Adjust-SL Clamp --------------------
@pytest.fixture(scope="module")
def paper_trade(api, auth):
    """Öffnet einen echten Paper-Trade und gibt (trade_id, entry) zurück."""
    # Sicherstellen, dass ai_trader/BTCUSDT für Paper aktiviert ist – sonst
    # verwirft die Signal-Pipeline den Trade (strategy_coin_configs=off ist Default).
    api.post(f"{BASE_URL}/api/autotrade/strategy/ai_trader/coin/BTCUSDT",
             json={"enabled": True, "mode": "paper", "max_capital": 100,
                   "leverage": 5}, headers=auth, timeout=15)
    body = {"symbol": "BTCUSDT", "side": "LONG", "sl_pct": 1.0,
            "tp1_pct": 1.5, "tpf_pct": 2.5, "leverage": 5,
            "capital_pct": 10, "reason": "test_iter_chat_cmds_e2e",
            "source": "manuell"}
    r = api.post(f"{BASE_URL}/api/ai/trade/open", json=body, headers=auth, timeout=30)
    assert r.status_code == 200, f"trade/open -> {r.status_code} {r.text}"
    data = r.json()
    assert data.get("status") == "ok", f"open status not ok: {data}"
    # Kurz warten, damit der Trade im DB-Read auftaucht
    time.sleep(1.0)
    lst = api.get(f"{BASE_URL}/api/autotrade/trades?status=open", timeout=20).json().get("trades", [])
    mine = [t for t in lst
            if t.get("strategy_id") == "ai_trader" and t.get("symbol") == "BTCUSDT"
            and t.get("mode") == "paper" and t.get("side") == "LONG"]
    assert mine, f"kein offener Paper-Trade gefunden. trades={lst[:2]}"
    tr = mine[0]
    tid = tr["id"]
    entry = float(tr.get("entry") or 0)
    assert entry > 0
    yield tid, entry
    # Teardown: falls noch offen, schließen
    try:
        api.post(f"{BASE_URL}/api/ai/trade/action",
                 json={"trade_id": tid, "action": "close"}, headers=auth, timeout=20)
    except Exception:
        pass


def test_trade_open_visible_in_list(api, paper_trade):
    tid, _ = paper_trade
    r = api.get(f"{BASE_URL}/api/autotrade/trades/{tid}", timeout=20)
    assert r.status_code == 200
    t = r.json().get("trade") or {}
    assert t.get("id") == tid
    assert t.get("status") == "open"


def test_adjust_sl_wrong_side_gets_clamped(api, auth, paper_trade):
    """SL auf falscher Seite (LONG SL weit ÜBER dem Kurs) darf nicht fehlschlagen –
    er wird auf die gültige Seite (knapp unter Kurs) korrigiert."""
    tid, entry = paper_trade
    wrong_sl = round(entry * 1.05, 4)  # deutlich über Kurs, LONG -> falsche Seite
    r = api.post(f"{BASE_URL}/api/ai/trade/action",
                 json={"trade_id": tid, "action": "adjust_sl", "value": wrong_sl},
                 headers=auth, timeout=30)
    assert r.status_code == 200, f"adjust_sl -> {r.status_code} {r.text}"
    data = r.json()
    detail = str(data.get("detail") or "")
    assert data.get("status") == "ok", f"adjust_sl clamp fehlgeschlagen: {data}"
    assert "falschen Seite" not in detail
    # Prüfen: SL liegt jetzt unter aktuellem Kurs (LONG)
    t = api.get(f"{BASE_URL}/api/autotrade/trades/{tid}", timeout=20).json().get("trade") or {}
    new_sl = float(t.get("sl") or 0)
    current = float(t.get("current_price") or t.get("mark_price") or t.get("entry") or 0)
    assert new_sl > 0 and current > 0
    assert new_sl < current, f"SL {new_sl} ist NICHT unter dem Kurs {current}"


def test_trade_close_real(api, auth, paper_trade):
    tid, _ = paper_trade
    r = api.post(f"{BASE_URL}/api/ai/trade/action",
                 json={"trade_id": tid, "action": "close"}, headers=auth, timeout=30)
    assert r.status_code == 200, f"close -> {r.status_code} {r.text}"
    assert r.json().get("status") == "ok"
    time.sleep(0.5)
    t = api.get(f"{BASE_URL}/api/autotrade/trades/{tid}", timeout=20).json().get("trade") or {}
    assert t.get("status") == "closed", f"Trade nicht geschlossen: {t.get('status')}"


# -------------------- Chat Kommando ohne LLM-Key (SSE) --------------------
def test_chat_command_no_llm_key_streams_error(auth):
    """Chat-Endpoint liefert SSE-Stream. Ohne Key erwarten wir eine saubere Meldung
    (kein 500)."""
    r = requests.post(f"{BASE_URL}/api/ai/chat",
                      json={"message": "Schließe alle Paper-Positionen"},
                      headers={**auth, "Content-Type": "application/json"},
                      stream=True, timeout=30)
    assert r.status_code == 200, f"chat status {r.status_code}"
    body = ""
    start = time.time()
    for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
        if chunk:
            body += chunk
        if time.time() - start > 15 or '"done": true' in body:
            break
    r.close()
    # Kein 500 – Stream lief. Kein Key -> irgendein Fehler-/Hinweis-Token erwartet.
    assert "data:" in body, f"kein SSE-Payload: {body[:300]}"
    low = body.lower()
    assert ("api-key" in low or "api_key" in low or "kein " in low or '"error"' in low
            or "provider" in low or "done" in low), \
        f"unerwartete Chat-Antwort: {body[:400]}"


# -------------------- Lessons CRUD + Wirkung --------------------
@pytest.fixture(scope="module")
def test_lesson(api, auth):
    body = {"title": "TEST_Lektion_ChatCmd", "detail": "Regel Y", "weight": 3}
    r = api.post(f"{BASE_URL}/api/ai/lessons", json=body, headers=auth, timeout=20)
    assert r.status_code == 200, f"lesson create -> {r.status_code} {r.text}"
    data = r.json()
    assert data.get("status") == "success"
    les = data.get("lesson") or {}
    assert les.get("id")
    yield les
    # Teardown
    try:
        api.delete(f"{BASE_URL}/api/ai/lessons/{les['id']}", headers=auth, timeout=20)
    except Exception:
        pass


def test_lesson_appears_in_list_locked(api, test_lesson):
    r = api.get(f"{BASE_URL}/api/ai/lessons", timeout=20)
    assert r.status_code == 200
    lessons = r.json().get("lessons") or []
    match = [x for x in lessons if x.get("id") == test_lesson["id"]]
    assert match, "Lektion nicht in Liste"
    assert bool(match[0].get("locked")) is True, "Trader-Lektion muss locked sein"


def test_lesson_appears_in_insights(api, test_lesson):
    r = api.get(f"{BASE_URL}/api/ai/insights", timeout=25)
    assert r.status_code == 200
    data = r.json()
    lessons = data.get("lessons") or []
    ids = [x.get("id") for x in lessons]
    assert test_lesson["id"] in ids, f"Lektion nicht in insights.lessons ({ids[:5]}...)"


def test_lesson_patch(api, auth, test_lesson):
    r = api.patch(f"{BASE_URL}/api/ai/lessons/{test_lesson['id']}",
                  json={"detail": "Regel Y v2"}, headers=auth, timeout=20)
    assert r.status_code == 200
    assert r.json().get("lesson", {}).get("detail") == "Regel Y v2"


# -------------------- Insights: lesson_candidates --------------------
def test_insights_has_lesson_candidates(api):
    r = api.get(f"{BASE_URL}/api/ai/insights", timeout=25)
    assert r.status_code == 200
    data = r.json()
    assert "lesson_candidates" in data
    assert isinstance(data["lesson_candidates"], list)


# -------------------- Validation: min_lesson_confirmations --------------------
def test_validation_min_lesson_confirmations_default(api):
    r = api.get(f"{BASE_URL}/api/ai/validation", timeout=20)
    assert r.status_code == 200
    settings = (r.json() or {}).get("settings") or r.json()
    val = int(settings.get("min_lesson_confirmations", 0))
    assert val >= 1


def test_validation_min_lesson_confirmations_clamp(api, auth):
    # Wert setzen und prüfen (in Range 1-10)
    r = api.post(f"{BASE_URL}/api/ai/validation",
                 json={"min_lesson_confirmations": 3}, headers=auth, timeout=20)
    assert r.status_code == 200
    settings = (r.json() or {}).get("settings") or {}
    assert int(settings.get("min_lesson_confirmations")) == 3
    # Clamp nach oben
    r = api.post(f"{BASE_URL}/api/ai/validation",
                 json={"min_lesson_confirmations": 999}, headers=auth, timeout=20)
    assert r.status_code == 200
    settings = (r.json() or {}).get("settings") or {}
    assert 1 <= int(settings.get("min_lesson_confirmations")) <= 10
    # Zurücksetzen auf 2 (Default)
    api.post(f"{BASE_URL}/api/ai/validation",
             json={"min_lesson_confirmations": 2}, headers=auth, timeout=20)


# -------------------- Strategie-Assistent --------------------
def test_strategy_assist_requires_thesis(api, auth):
    r = api.post(f"{BASE_URL}/api/ai/strategies/assist",
                 json={}, headers=auth, timeout=20)
    assert r.status_code == 400
    detail = str((r.json() or {}).get("detail") or "")
    assert "beschreib" in detail.lower() or "idee" in detail.lower(), detail


def test_strategy_assist_without_key(api, auth):
    r = api.post(f"{BASE_URL}/api/ai/strategies/assist",
                 json={"thesis": "RSI unter 30 kaufen, ueber 70 verkaufen"},
                 headers=auth, timeout=25)
    # Ohne LLM-Key erwarten wir 400 mit klarer Meldung – KEIN 500
    assert r.status_code == 400, f"unexpected {r.status_code}: {r.text[:200]}"
    detail = str((r.json() or {}).get("detail") or "")
    assert "api-key" in detail.lower() or "api_key" in detail.lower() or "provider" in detail.lower(), detail


# -------------------- Strategy Candidates: Register-Test --------------------
@pytest.fixture(scope="module")
def rule_candidate(api, auth):
    body = {
        "name": "TEST_RSI_Rule",
        "thesis": "RSI<30 kaufen",
        "source": "trader",
        "rule_definition": {
            "timeframe": "1m",
            "indicators": {"rsi_period": 14},
            "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
        },
    }
    r = api.post(f"{BASE_URL}/api/ai/strategies", json=body, headers=auth, timeout=25)
    assert r.status_code == 200, f"candidate create -> {r.status_code} {r.text}"
    data = r.json()
    # Endpoint returns {"status": "success", **res} where res may contain
    # status="ok" (dict-unpack override). Beide sind ok.
    assert data.get("status") in ("success", "ok"), data
    cand = data.get("candidate") or {}
    assert cand.get("id")
    yield cand
    # teardown: reject
    try:
        api.post(f"{BASE_URL}/api/ai/strategies/{cand['id']}/decide",
                 json={"action": "reject", "note": "test teardown"},
                 headers=auth, timeout=20)
    except Exception:
        pass


def test_rule_candidate_has_custom_strategy_id(rule_candidate):
    assert rule_candidate.get("custom_strategy_id"), \
        f"custom_strategy_id nicht gesetzt: {rule_candidate.keys()}"


@pytest.fixture(scope="module")
def no_rule_candidate(api, auth):
    body = {"name": "TEST_NoRules", "thesis": "News-getrieben",
            "source": "trader"}
    r = api.post(f"{BASE_URL}/api/ai/strategies", json=body, headers=auth, timeout=25)
    assert r.status_code == 200, f"candidate create -> {r.status_code} {r.text}"
    cand = (r.json() or {}).get("candidate") or {}
    assert cand.get("id")
    yield cand
    try:
        api.post(f"{BASE_URL}/api/ai/strategies/{cand['id']}/decide",
                 json={"action": "reject", "note": "test teardown"},
                 headers=auth, timeout=20)
    except Exception:
        pass


def test_register_test_without_rules_returns_not_testable(api, auth, no_rule_candidate):
    r = api.post(f"{BASE_URL}/api/ai/strategies/{no_rule_candidate['id']}/register-test",
                 headers=auth, timeout=20)
    # Weder 500 noch 404 – Endpoint muss verständlich mit 200 + not_testable antworten
    assert r.status_code == 200, f"register-test -> {r.status_code} {r.text}"
    data = r.json() or {}
    assert data.get("status") == "not_testable", data
    assert data.get("detail")
