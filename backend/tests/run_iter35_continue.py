"""Continue-Script: nutze bereits offenen Trade und führe partial_close x2 + close aus."""
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
                time.sleep(10); continue
            return r
        except Exception as e:
            print(f"  retry {attempt+1}: {e}")
            time.sleep(10)
    return last

# Login
r = _req("POST", "/api/auth/login",
         json={"username": "Admin", "password": "Dean06Greif!/Admin"})
assert r.status_code == 200
H = {"Authorization": f"Bearer {r.json()['token']}", "Content-Type": "application/json"}

TID = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT-1786868395390"
print(f"Using TRADE_ID={TID}")

# GET current state
r = _req("GET", f"/api/autotrade/trades/{TID}")
t = r.json().get("trade") or r.json()
print(f"Current status={t.get('status')} qty_rem={t.get('qty_remaining')} events={len(t.get('events') or [])}")
if t.get("status") != "open":
    print("Trade is not open, opening a new one...")
    body = {"symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
            "margin_usdt": 20, "leverage": 5,
            "sl_pct": 1.0, "tp1_pct": 1.5, "tpf_pct": 3.0,
            "horizon": "scalp", "source": "manuell",
            "reason": "E2E test staffel iter35 retry"}
    r = _req("POST", "/api/ai/trade/open", json=body, headers=H)
    assert r.status_code == 200, r.text[:300]
    time.sleep(2)
    r = _req("GET", "/api/autotrade/trades?status=open&limit=50")
    trades = r.json().get("trades", [])
    btc = [t for t in trades if t.get("symbol") == "BTCUSDT" and
           t.get("mode") == "paper" and t.get("manual_trade")]
    btc.sort(key=lambda t: t.get("opened_at", ""), reverse=True)
    TID = btc[0]["id"]
    print(f"NEW TRADE_ID={TID}")

# stage 1
print("[stage 1] partial_close 30%")
r = _req("POST", f"/api/autotrade/trade/{TID}/action",
         json={"action": "partial_close", "value": 30, "reason": "stufe 1"},
         headers=H)
print(f"  {r.status_code}: {r.text[:300]}")
assert r.status_code == 200, r.text[:400]
time.sleep(1)

# stage 2
print("[stage 2] partial_close 30%")
r = _req("POST", f"/api/autotrade/trade/{TID}/action",
         json={"action": "partial_close", "value": 30, "reason": "stufe 2"},
         headers=H)
print(f"  {r.status_code}: {r.text[:300]}")
assert r.status_code == 200, r.text[:400]
time.sleep(1)

# verify
r = _req("GET", f"/api/autotrade/trades/{TID}")
t = r.json().get("trade") or r.json()
events = t.get("events") or []
print("EVENTS:")
for e in events: print(f"  - {e}")
print(f"qty_remaining={t.get('qty_remaining')}")

s1 = [e for e in events if "TEIL-EXIT Stufe 1" in str(e)]
s2 = [e for e in events if "TEIL-EXIT Stufe 2" in str(e)]
assert s1 and s2, f"Stages missing: s1={s1} s2={s2}"
assert any(("TP-Staffel" in str(e) or "Teil-Absicherung" in str(e)) for e in s1+s2)

# close
print("[close] full close")
r = _req("POST", f"/api/autotrade/close/{TID}", headers=H)
print(f"  {r.status_code}: {r.text[:300]}")
assert r.status_code == 200, r.text[:400]
time.sleep(1)
r = _req("GET", f"/api/autotrade/trades/{TID}")
t = r.json().get("trade") or r.json()
print(f"FINAL status={t.get('status')} realized_pnl={t.get('realized_pnl')} qty_rem={t.get('qty_remaining')}")
assert t.get("status") == "closed"
assert t.get("realized_pnl") is not None

result = {
    "trade_id": TID,
    "status": t.get("status"),
    "realized_pnl": t.get("realized_pnl"),
    "qty_remaining_final": t.get("qty_remaining"),
    "stage_events": [e for e in (t.get("events") or []) if "TEIL-EXIT" in str(e)],
    "base_url_used": BASE,
}
print("\n=== SUMMARY ===")
print(json.dumps(result, indent=2, ensure_ascii=False))
with open("/app/test_reports/iter35_e2e_result.json", "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print("\nOK")
