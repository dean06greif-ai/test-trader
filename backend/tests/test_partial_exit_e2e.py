"""E2E-Test für gestaffelte Teil-Exits (KI-Staffel):
1) Admin-Login -> Token
2) Manuellen Paper-Trade öffnen (POST /api/ai/trade/open, mode=paper)
3) 2x partial_close (POST /api/autotrade/trade/{id}/action)
4) GET trades -> events prüfen (Stufe 1 + Stufe 2, Tag TP-Staffel|Teil-Absicherung)
5) Trade schließen -> status closed, realized_pnl gesetzt
STRICT: nur mode=paper.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASSWORD = "Dean06Greif!/Admin"

# Testdaten – wird über gesamten Test-Lauf geteilt
STATE = {}


@pytest.fixture(scope="class")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token")
    assert tok
    return tok


@pytest.fixture(scope="class")
def hdr(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class TestPartialExitE2E:
    def test_01_open_paper_trade(self, hdr):
        """Manuellen Paper-Trade öffnen (BTCUSDT LONG, 20 USDT, Hebel 5)."""
        body = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "mode": "paper",
            "margin_usdt": 20,
            "leverage": 5,
            "sl_pct": 1.0,
            "tp1_pct": 1.5,
            "tpf_pct": 3.0,
            "horizon": "scalp",
            "source": "manuell",
            "reason": "E2E test staffel"
        }
        r = requests.post(f"{BASE_URL}/api/ai/trade/open", json=body, headers=hdr, timeout=60)
        assert r.status_code == 200, f"open failed: {r.status_code} {r.text[:400]}"
        d = r.json()
        assert d.get("status") == "ok", f"open not ok: {d}"
        STATE["symbol"] = d["symbol"]
        print(f"[OPEN] {d}")

        # ID via GET trades finden
        time.sleep(2)
        tr = requests.get(f"{BASE_URL}/api/autotrade/trades?status=open&limit=50",
                          timeout=30)
        assert tr.status_code == 200
        trades = tr.json().get("trades", [])
        btc = [t for t in trades if t.get("symbol") == "BTCUSDT" and
               t.get("mode") == "paper" and t.get("manual_trade")]
        assert btc, f"kein offener BTCUSDT paper-trade gefunden. Alle: {[(t.get('symbol'),t.get('mode'),t.get('id')) for t in trades[:10]]}"
        btc.sort(key=lambda t: t.get("opened_at", ""), reverse=True)
        STATE["trade_id"] = btc[0]["id"]
        STATE["initial_qty"] = float(btc[0].get("qty") or 0)
        STATE["initial_qty_rem"] = float(btc[0].get("qty_remaining") or btc[0].get("qty") or 0)
        print(f"[TRADE_ID] {STATE['trade_id']} qty={STATE['initial_qty']} qty_rem={STATE['initial_qty_rem']}")
        assert STATE["initial_qty"] > 0

    def test_02_partial_close_stage_1(self, hdr):
        tid = STATE["trade_id"]
        r = requests.post(f"{BASE_URL}/api/autotrade/trade/{tid}/action",
                          json={"action": "partial_close", "value": 30,
                                "reason": "test staffel stufe 1"},
                          headers=hdr, timeout=60)
        print(f"[PARTIAL1] {r.status_code} {r.text[:400]}")
        assert r.status_code == 200, f"stage 1 failed: {r.status_code} {r.text[:300]}"
        d = r.json()
        assert d, f"empty resp: {d}"

    def test_03_partial_close_stage_2(self, hdr):
        tid = STATE["trade_id"]
        r = requests.post(f"{BASE_URL}/api/autotrade/trade/{tid}/action",
                          json={"action": "partial_close", "value": 30,
                                "reason": "test staffel stufe 2"},
                          headers=hdr, timeout=60)
        print(f"[PARTIAL2] {r.status_code} {r.text[:400]}")
        assert r.status_code == 200, f"stage 2 failed: {r.status_code} {r.text[:300]}"

    def test_04_verify_events_and_qty(self, hdr):
        tid = STATE["trade_id"]
        r = requests.get(f"{BASE_URL}/api/autotrade/trades/{tid}", timeout=30)
        assert r.status_code == 200
        t = r.json().get("trade") or r.json()
        events = t.get("events") or []
        print(f"[EVENTS] {events}")
        print(f"[QTY_REM] {t.get('qty_remaining')} / initial {STATE['initial_qty']}")

        stage1 = [e for e in events if "TEIL-EXIT Stufe 1" in str(e)]
        stage2 = [e for e in events if "TEIL-EXIT Stufe 2" in str(e)]
        assert stage1, f"kein 'TEIL-EXIT Stufe 1' Event: {events}"
        assert stage2, f"kein 'TEIL-EXIT Stufe 2' Event: {events}"

        tag_ok = any(("TP-Staffel" in str(e) or "Teil-Absicherung" in str(e))
                     for e in stage1 + stage2)
        assert tag_ok, f"kein Stage-Tag (TP-Staffel/Teil-Absicherung): {stage1 + stage2}"

        qty_rem = float(t.get("qty_remaining") or 0)
        assert qty_rem < STATE["initial_qty"], \
            f"qty_remaining ({qty_rem}) NICHT reduziert (initial {STATE['initial_qty']})"
        STATE["qty_rem_after_partial"] = qty_rem

    def test_05_close_trade_full(self, hdr):
        tid = STATE["trade_id"]
        r = requests.post(f"{BASE_URL}/api/autotrade/close/{tid}", headers=hdr, timeout=60)
        print(f"[CLOSE] {r.status_code} {r.text[:400]}")
        assert r.status_code == 200, f"close failed: {r.status_code} {r.text[:300]}"

        time.sleep(1)
        g = requests.get(f"{BASE_URL}/api/autotrade/trades/{tid}", timeout=30)
        assert g.status_code == 200
        t = g.json().get("trade") or g.json()
        print(f"[FINAL] status={t.get('status')} realized_pnl={t.get('realized_pnl')} qty_rem={t.get('qty_remaining')}")
        assert t.get("status") == "closed", f"status nicht closed: {t.get('status')}"
        assert t.get("realized_pnl") is not None, "realized_pnl fehlt"

    def test_06_report_trade_id(self):
        print(f"\n=== E2E TRADE_ID (Report): {STATE.get('trade_id')} ===\n")
        assert STATE.get("trade_id")
