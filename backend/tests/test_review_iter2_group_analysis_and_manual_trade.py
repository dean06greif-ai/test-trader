"""Review-Test Iteration 2:
- Config-Feld group_analysis default=true, POST-Update wird übernommen und zurücksetzbar.
- POST /api/ai/trade/open mit mode='paper' + margin_usdt=50: Falls Kill-Switch aktiv → status=blocked (Format-Check, korrektes Guard-Verhalten laut Review-Request). Sonst: Trade wird als paper mit margin ~50 USDT eröffnet und danach wieder geschlossen.
- GET /api/notifications?unread_only=true liefert Liste; POST /api/notifications/read (mit ids) hat definiertes Format.

Läuft gegen produktive Preview-URL. KEINE destruktiven Aktionen auf Live-Trades.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://daytrader-ml.preview.emergentagent.com",
).rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Dean06Greif!/Admin")
TIMEOUT = 90


def _login():
    last = None
    for _ in range(4):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return r
        last = r
    return last


def _retry_get(path, headers=None, tries=4):
    """GET mit Retry gegen sporadische Cloudflare-502 (Ingress-Timeout)."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = r
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    return last


def _retry_post(path, headers=None, json_body=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.post(f"{BASE_URL}{path}", headers=headers, json=json_body, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = r
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    return last


@pytest.fixture(scope="session")
def admin_token():
    r = _login()
    assert r.status_code == 200, f"Admin-Login fehlgeschlagen: {r.status_code} {r.text[:200]}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


# ---------------------------- 1. KI-Config: group_analysis ----------------------------
class TestGroupAnalysisConfig:
    def test_group_analysis_default_true(self):
        r = _retry_get("/api/ai/status")
        assert r is not None and r.status_code == 200, getattr(r, "text", str(r))[:400]
        cfg = r.json().get("config", {})
        assert "group_analysis" in cfg, f"group_analysis fehlt in config keys={list(cfg.keys())}"
        assert cfg["group_analysis"] is True, f"group_analysis default sollte True sein, ist {cfg['group_analysis']}"

    def test_group_analysis_toggle_and_restore(self, auth_headers):
        # 1. Setze auf false
        try:
            r = _retry_post("/api/ai/config", headers=auth_headers, json_body={"group_analysis": False})
            assert r is not None and r.status_code == 200, getattr(r, "text", str(r))[:400]
            cfg = r.json().get("config", {})
            assert cfg.get("group_analysis") is False, f"Nach set false: {cfg.get('group_analysis')}"

            # 2. Verifiziere via GET
            r2 = _retry_get("/api/ai/status")
            assert r2 is not None and r2.status_code == 200
            assert r2.json().get("config", {}).get("group_analysis") is False
        finally:
            # 3. Zurücksetzen auf true
            r3 = _retry_post("/api/ai/config", headers=auth_headers, json_body={"group_analysis": True})
            assert r3 is not None and r3.status_code == 200, getattr(r3, "text", str(r3))[:400]
            cfg = r3.json().get("config", {})
            assert cfg.get("group_analysis") is True, f"Reset auf True fehlgeschlagen: {cfg.get('group_analysis')}"


# ---------------------------- 2. POST /api/ai/trade/open (paper) ----------------------------
class TestAiTradeOpenPaper:
    def _find_open_trade(self, headers, symbol, side):
        r = requests.get(f"{BASE_URL}/api/autotrade/trades?limit=20", headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        lst = r.json() if isinstance(r.json(), list) else r.json().get("trades") or r.json().get("items") or []
        cand = [
            t for t in lst
            if (t.get("symbol") == symbol
                and (t.get("side") or "").upper() == side
                and (t.get("status") or "").lower() == "open"
                and (t.get("mode") or "").lower() == "paper")
        ]
        cand.sort(key=lambda t: t.get("opened_at") or "", reverse=True)
        return cand[0] if cand else None

    def test_open_paper_trade_or_blocked_by_killswitch(self, auth_headers):
        payload = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "mode": "paper",
            "margin_usdt": 50,
            "leverage": 5,
            "sl_pct": 1.0,
            "tp1_pct": 1.5,
            "tpf_pct": 3.0,
            "reason": "Test iter2",
            "source": "manuell",
        }
        r = requests.post(
            f"{BASE_URL}/api/ai/trade/open",
            headers=auth_headers,
            json=payload,
            timeout=60,
        )
        assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:400]}"
        body = r.json()
        status = (body.get("status") or "").lower()

        # Kill-Switch aktiv → 'blocked' ist korrektes Guard-Verhalten (Review-Request-Vorgabe)
        if status == "blocked":
            detail = (body.get("detail") or body.get("reason") or "").lower()
            assert any(k in detail for k in ("stop", "pause", "kill", "master")), \
                f"blocked-Grund unerwartet: {body}"
            pytest.skip(f"Kill-Switch aktiv → paper-trade guard OK ({body.get('detail')})")

        # Sonst muss Trade als paper mit margin ~50 eröffnet worden sein
        assert status in ("ok", "opened", "success"), f"unerwarteter status: {body}"
        trade = body.get("trade") or body.get("data") or {}
        # Suche nach margin_used / mode falls im body vorhanden
        mode = (trade.get("mode") or body.get("mode") or "").lower()
        assert mode in ("paper", ""), f"Trade mode sollte paper sein, ist '{mode}': {body}"

        # Warte & finde Trade in Liste
        time.sleep(2)
        trade_row = self._find_open_trade(auth_headers, "BTCUSDT", "LONG")
        try:
            assert trade_row is not None, f"Kein offener paper-Trade in autotrade/trades nach open. body={body}"
            assert (trade_row.get("mode") or "").lower() == "paper"
            comp = trade_row.get("computed") or {}
            margin_used = comp.get("margin_used") or trade_row.get("margin_used") or trade_row.get("margin")
            assert margin_used is not None
            assert 45 <= float(margin_used) <= 55, f"margin_used ~50 erwartet, ist {margin_used}"
        finally:
            # Cleanup: schließe Test-Trade falls existiert
            if trade_row and trade_row.get("id"):
                requests.post(
                    f"{BASE_URL}/api/autotrade/close/{trade_row['id']}",
                    headers=auth_headers, timeout=60,
                )


# ---------------------------- 3. Notifications ----------------------------
class TestNotifications:
    def test_get_notifications_unread_only_format(self):
        r = _retry_get("/api/notifications?unread_only=true")
        assert r is not None and r.status_code == 200, getattr(r, "text", str(r))[:400]
        body = r.json()
        assert "notifications" in body, f"Format: 'notifications' key fehlt: {body}"
        assert isinstance(body["notifications"], list)
        # Format-Check der Elemente falls vorhanden
        for n in body["notifications"]:
            assert "id" in n
            assert "type" in n
            assert "read" in n
            assert n.get("read") is False, "unread_only=true → alle read=false"

    def test_notifications_read_endpoint_format(self, auth_headers):
        # Endpoint muss existieren + valides JSON zurückgeben (mit fake-ID)
        r = _retry_post(
            "/api/notifications/read",
            headers=auth_headers,
            json_body={"ids": ["nonexistent_id_iter2_review"]},
        )
        assert r is not None and r.status_code == 200, getattr(r, "text", str(r))[:400]
        body = r.json()
        assert body.get("status") == "ok", f"Format: status=ok erwartet: {body}"
        assert "updated" in body
        assert isinstance(body["updated"], int)
        assert body["updated"] == 0, "nonexistent-ID sollte updated=0 ergeben"
