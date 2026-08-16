"""Sequentieller E2E-Test für gestaffelte Teil-Exits – KEIN pytest/xdist."""
import os, sys, time, json
import requests

BASE = os.environ.get("BASE_URL_OVERRIDE") or os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

def _req(method, path, **kw):
    kw.setdefault("timeout", 90)
    last = None
    for attempt in range(6):
        try:
            r = requests.request(method, f"{BASE}{path}", **kw)
            if r.status_code >= 500 or r.status_code == 429:
                print(f"  retry {attempt+1}: HTTP {r.status_code}")
                last = r
                time.sleep(10)
                continue
            return r
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            print(f"  retry {attempt+1}: {e}")
            time.sleep(10)
    if last is not None:
        return last
    raise RuntimeError(f"failed after retries: {method} {path}")

# --- 1. Login
print("[1] Login...")
r = _req("POST", "/api/auth/login",
         json={"username": "Admin", "password": "Dean06Greif!/Admin"})
print(f"  status={r.status_code}")
assert r.status_code == 200, r.text
token = r.json()["token"]
H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# --- 2. Manuellen Paper-Trade eröffnen
print("[2] Open manual paper trade BTCUSDT LONG 20USDT lev5...")
body = {"symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
        "margin_usdt": 20, "leverage": 5,
        "sl_pct": 1.0, "tp1_pct": 1.5, "tpf_pct": 3.0,
        "horizon": "scalp", "source": "manuell",
        "reason": "E2E test staffel iter35"}
r = _req("POST", "/api/ai/trade/open", json=body, headers=H)
print(f"  status={r.status_code} body={r.text[:400]}")
assert r.status_code == 200, r.text
d = r.json()
assert d.get("status") == "ok", d

time.sleep(2)
r = _req("GET", "/api/autotrade/trades?status=open&limit=50")
assert r.status_code == 200
trades = r.json().get("trades", [])
btc = [t for t in trades if t.get("symbol") == "BTCUSDT"
       and t.get("mode") == "paper" and t.get("manual_trade")]
btc.sort(key=lambda t: t.get("opened_at", ""), reverse=True)
assert btc, f"no matching paper trade found; open trades symbols/modes: {[(t.get('symbol'),t.get('mode'),t.get('manual_trade')) for t in trades[:15]]}"
TID = btc[0]["id"]
INIT_QTY = float(btc[0].get("qty") or 0)
print(f"  TRADE_ID={TID} qty={INIT_QTY} qty_rem={btc[0].get('qty_remaining')}")

# --- 3. Partial close stage 1
print("[3] partial_close stage 1 (30%)...")
r = _req("POST", f"/api/autotrade/trade/{TID}/action",
         json={"action": "partial_close", "value": 30, "reason": "stufe 1"},
         headers=H)
print(f"  status={r.status_code} body={r.text[:400]}")
assert r.status_code == 200, r.text
time.sleep(1)

# --- 4. Partial close stage 2
print("[4] partial_close stage 2 (30%)...")
r = _req("POST", f"/api/autotrade/trade/{TID}/action",
         json={"action": "partial_close", "value": 30, "reason": "stufe 2"},
         headers=H)
print(f"  status={r.status_code} body={r.text[:400]}")
assert r.status_code == 200, r.text
time.sleep(1)

# --- 5. Verify events + qty_remaining
print("[5] Verify events + qty_remaining reduced...")
r = _req("GET", f"/api/autotrade/trades/{TID}")
assert r.status_code == 200
t = r.json().get("trade") or r.json()
events = t.get("events") or []
print(f"  EVENTS ({len(events)}):")
for e in events: print(f"    - {e}")
print(f"  qty_remaining={t.get('qty_remaining')} (initial {INIT_QTY})")

stage1 = [e for e in events if "TEIL-EXIT Stufe 1" in str(e)]
stage2 = [e for e in events if "TEIL-EXIT Stufe 2" in str(e)]
assert stage1, "no 'TEIL-EXIT Stufe 1' event"
assert stage2, "no 'TEIL-EXIT Stufe 2' event"
tag_ok = any(("TP-Staffel" in str(e) or "Teil-Absicherung" in str(e))
             for e in stage1 + stage2)
assert tag_ok, f"no stage tag: {stage1 + stage2}"
qty_rem = float(t.get("qty_remaining") or 0)
assert qty_rem < INIT_QTY, f"qty_rem {qty_rem} not < initial {INIT_QTY}"

# --- 6. Trade komplett schließen
print("[6] full close...")
r = _req("POST", f"/api/autotrade/close/{TID}", headers=H)
print(f"  status={r.status_code} body={r.text[:400]}")
assert r.status_code == 200, r.text
time.sleep(1)
r = _req("GET", f"/api/autotrade/trades/{TID}")
t = r.json().get("trade") or r.json()
print(f"  FINAL status={t.get('status')} realized_pnl={t.get('realized_pnl')} qty_rem={t.get('qty_remaining')}")
assert t.get("status") == "closed", t.get("status")
assert t.get("realized_pnl") is not None

# summary
result = {
    "trade_id": TID, "initial_qty": INIT_QTY,
    "final_status": t.get("status"),
    "realized_pnl": t.get("realized_pnl"),
    "qty_remaining_final": t.get("qty_remaining"),
    "events_count": len(t.get("events") or []),
    "stage_events": [e for e in (t.get("events") or []) if "TEIL-EXIT" in str(e)],
}
print("\n=== RESULT ===")
print(json.dumps(result, indent=2, ensure_ascii=False))
with open("/app/test_reports/iter35_e2e_result.json", "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print("\nOK - saved to /app/test_reports/iter35_e2e_result.json")
