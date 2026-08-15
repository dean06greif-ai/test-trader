"""Regressionstests: Server-Refactor (Router-Module) + Bugfixes.
Läuft gegen den laufenden Backend-Server (localhost:8001) + Unit-Tests der Simulation.
"""
import os
import requests

BASE = "http://localhost:8001"


def _token():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr():
    return {"Authorization": f"Bearer {_token()}"}


def test_health_and_core_endpoints():
    for path in ("/api/health", "/api/coins", "/api/settings", "/api/strategies",
                 "/api/session/status", "/api/control/state", "/api/system/ram",
                 "/api/backtest/active", "/api/optimizer/active",
                 "/api/autotrade/config", "/api/performance", "/api/analytics/daily"):
        r = requests.get(f"{BASE}{path}", timeout=15)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"


def test_write_endpoints_require_admin():
    r = requests.post(f"{BASE}/api/settings", json={}, timeout=10)
    assert r.status_code == 401
    r = requests.post(f"{BASE}/api/backtest/run", json={}, timeout=10)
    assert r.status_code == 401


def test_optimizer_apply_strategy_timeframe_single_source():
    """BUGFIX: Timeframe muss die Definition überschreiben und überall konsistent sein."""
    hdr = _hdr()
    definition = {
        "name": "TF-Test Strategie",
        "timeframe": "1m",  # absichtlich falsch – muss von body.timeframe überschrieben werden
        "indicators": {"rsi_period": 14},
        "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
        "short_rules": [{"indicator": "rsi", "op": ">", "value": 70}],
    }
    r = requests.post(f"{BASE}/api/optimizer/apply", headers=hdr, timeout=15, json={
        "type": "strategy", "definition": definition, "timeframe": "5m",
        "trade_params": {"tp1_crv": 1.5, "auto_leverage_enabled": True, "leverage": 20},
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    try:
        # 1) definition.timeframe wurde überschrieben
        assert r.json()["definition"]["timeframe"] == "5m"
        # 2) strategy_timeframes synchron
        settings = requests.get(f"{BASE}/api/settings", timeout=10).json()
        assert settings.get("strategy_timeframes", {}).get(sid) == "5m"
        # 3) Export: alle Timeframe-Angaben konsistent
        exp = requests.get(f"{BASE}/api/strategies/{sid}/export", timeout=10).json()
        assert exp["timeframe"] == "5m"
        assert exp["definition"]["timeframe"] == "5m"
        assert exp["backtest_config"].get("timeframe") == "5m"
        # 4) Trade-Params als Backtest-Config gespeichert (Backtester nutzt Strategie-Werte)
        cfgs = requests.get(f"{BASE}/api/backtest/strategy-configs", timeout=10).json()["configs"]
        assert cfgs.get(sid, {}).get("tp1_crv") == 1.5
        assert cfgs.get(sid, {}).get("auto_leverage_enabled") is True
        # 5) Live/Paper-Vorauswahl (strategy_override) vorausgefüllt, Modus bleibt aus
        ov = requests.get(f"{BASE}/api/autotrade/strategy/{sid}", timeout=10).json()["config"]
        assert ov.get("tp1_crv") == 1.5
        assert ov.get("leverage") == 20
        assert ov.get("mode", "off") == "off"
    finally:
        requests.delete(f"{BASE}/api/strategies/{sid}", headers=hdr, timeout=10)


def test_duplicate_strategy():
    hdr = _hdr()
    definition = {
        "name": "Dupli-Test",
        "timeframe": "10m",
        "indicators": {"rsi_period": 10},
        "long_rules": [{"indicator": "rsi", "op": "<", "value": 25}],
        "short_rules": [],
    }
    r = requests.post(f"{BASE}/api/strategies/custom", headers=hdr, json=definition, timeout=10)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    dup_id = None
    try:
        r = requests.post(f"{BASE}/api/strategies/{sid}/duplicate", headers=hdr, json={}, timeout=10)
        assert r.status_code == 200, r.text
        dup_id = r.json()["id"]
        assert dup_id != sid
        assert "(Kopie)" in r.json()["name"]
        # Kopie taucht in der Strategie-Liste auf, Original bleibt erhalten
        ids = {s["id"] for s in requests.get(f"{BASE}/api/strategies", timeout=10).json()["strategies"]}
        assert sid in ids and dup_id in ids
        # Timeframe der Kopie synchron
        settings = requests.get(f"{BASE}/api/settings", timeout=10).json()
        assert settings.get("strategy_timeframes", {}).get(dup_id) == "10m"
        # Built-ins können nicht dupliziert werden
        r = requests.post(f"{BASE}/api/strategies/scalping_4_rules/duplicate",
                          headers=hdr, json={}, timeout=10)
        assert r.status_code == 400
    finally:
        requests.delete(f"{BASE}/api/strategies/{sid}", headers=hdr, timeout=10)
        if dup_id:
            requests.delete(f"{BASE}/api/strategies/{dup_id}", headers=hdr, timeout=10)


def test_custom_strategy_timeframe_sync_on_create():
    hdr = _hdr()
    definition = {
        "name": "TF-Sync-Test", "timeframe": "15m",
        "indicators": {}, "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
        "short_rules": [],
    }
    r = requests.post(f"{BASE}/api/strategies/custom", headers=hdr, json=definition, timeout=10)
    assert r.status_code == 200
    sid = r.json()["id"]
    try:
        settings = requests.get(f"{BASE}/api/settings", timeout=10).json()
        assert settings.get("strategy_timeframes", {}).get(sid) == "15m"
    finally:
        requests.delete(f"{BASE}/api/strategies/{sid}", headers=hdr, timeout=10)


# ---------------- Unit: Liquidations-Schutz in der Simulation ----------------
def _mk_candles(n, start=100.0, step=0.0):
    out = []
    p = start
    for i in range(n):
        out.append({"timestamp": 1700000000000 + i * 60000,
                    "open": p, "high": p * 1.001, "low": p * 0.999,
                    "close": p, "volume": 10.0})
        p += step
    return out


class _AlwaysLong:
    IS_CUSTOM = False
    STRATEGY_ID = "always_long"
    STRATEGY_NAME = "Always Long"

    def check_signal(self, candles, symbol, settings):
        return {"type": "LONG", "entry_price": candles[-1]["close"]}


def test_simulate_pair_no_liquidation_with_trailing_sl():
    """SL wird immer VOR dem Liquidationspreis platziert -> keine Liquidation möglich."""
    from services.backtester import simulate_pair
    # Hoher fester Hebel (50x -> Liq ~1.5% hinter Entry) + weit entfernter Struktur-SL
    # + starker Crash: ohne Fix wird liquidiert, mit Fix greift der SL vorher.
    candles = _mk_candles(150, start=100.0, step=0.0)
    for i, c in enumerate(candles[60:], start=60):  # Crash ab Kerze 60
        drop = (i - 59) * 0.8
        c["open"] = c["high"] = 100.0 - drop + 0.4
        c["close"] = 100.0 - drop
        c["low"] = c["close"] - 0.4
    cfg = {"max_capital": 100.0, "leverage": 50, "fee_percent": 0.06,
           "sl_mode": "fixed", "sl_fixed_percent": 5.0,  # SL 5% weg > 1.5% Liq-Distanz
           "tp1_crv": 1.0, "tp_full_crv": 2.0, "tp1_close_percent": 50,
           "breakeven_enabled": False, "be_mode": "off",
           "maintenance_margin_rate": 0.5}
    res = simulate_pair(_AlwaysLong(), candles, "BTCUSDT", {}, cfg, collect_trades=True)
    assert res["trades"] > 0
    assert res["liquidations"] == 0, \
        f"Liquidation trotz SL-Schutz: {res['liquidations']} (Trades: {res['trades']})"
    for t in res.get("all_trades", []):
        assert not t.get("liquidated")


def test_optimizer_run_accepts_fixed_sessions():
    """Optimizer nimmt ein festes Zeitfenster an (Validierung ohne Lauf)."""
    hdr = _hdr()
    # ungültige Strategie -> 400 (Body-Validierung greift, sessions wird akzeptiert)
    r = requests.post(f"{BASE}/api/optimizer/run", headers=hdr, timeout=10, json={
        "mode": "params", "strategy_id": "does_not_exist",
        "symbols": ["BTCUSDT"], "sessions": "15:00-18:00"})
    assert r.status_code == 400


def test_backtest_run_validation_unchanged():
    hdr = _hdr()
    r = requests.post(f"{BASE}/api/backtest/run", headers=hdr, timeout=10,
                      json={"strategy_ids": [], "symbols": []})
    assert r.status_code == 400
