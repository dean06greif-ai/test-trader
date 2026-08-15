"""
Krypto_Alert comprehensive feature tests.
Covers new multi-strategy, custom-strategy, rule-states, autotrade, klines,
signals/performance/analytics endpoints introduced in this iteration.

Grouped inside ONE test class to keep pytest-xdist loadscope on a single
worker (shared scanner + autotrader singletons in server).
"""
import os
import uuid
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # fallback to frontend/.env if pytest is run from backend dir
    from pathlib import Path
    env = Path("/app/frontend/.env").read_text()
    for line in env.splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip()
            break
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def http():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    # Admin-Token für Schreib-Endpunkte (Auth wurde nachträglich eingeführt)
    r = s.post(f"{API}/auth/login",
               json={"username": os.environ.get("ADMIN_USER", "Admin"),
                     "password": os.environ.get("ADMIN_PASSWORD", "admin")},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return s


# ---------------- keepalive / basics ----------------
class TestKryptoAlert:
    """All feature tests kept in ONE class so xdist loadscope pins them to a single worker."""

    # --- setup: snapshot original state so we can restore in teardown ---
    @classmethod
    def setup_class(cls):
        try:
            cls.orig_settings = requests.get(f"{API}/settings", timeout=10).json()
        except Exception:
            cls.orig_settings = {}
        try:
            cls.orig_at = requests.get(f"{API}/autotrade/config", timeout=10).json()
        except Exception:
            cls.orig_at = {}
        cls.created_custom_ids = []

    @classmethod
    def teardown_class(cls):
        # restore settings
        try:
            if cls.orig_settings:
                requests.post(f"{API}/settings", json=cls.orig_settings, timeout=10)
        except Exception:
            pass
        # restore autotrade
        try:
            if cls.orig_at and cls.orig_at.get("config"):
                requests.post(f"{API}/autotrade/config", json=cls.orig_at["config"], timeout=10)
        except Exception:
            pass
        # remove any leftover custom strategies
        for cid in cls.created_custom_ids:
            try:
                requests.delete(f"{API}/strategies/custom/{cid}", timeout=10)
            except Exception:
                pass

    # --- basics ---
    def test_01_health_minimal_alive(self, http):
        r = http.get(f"{API}/health", timeout=10)
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

    def test_02_coins_groups(self, http):
        r = http.get(f"{API}/coins", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["coins"], list) and len(data["coins"]) > 10
        assert "BTCUSDT" in data["coins"]
        assert len(data["crypto"]) == 10
        names = [g["name"] for g in data["groups"]]
        for expected in ("TOP 10 COINS", "RESOURCES", "INDICES", "FOREX"):
            assert expected in names
        res = next(g for g in data["groups"] if g["name"] == "RESOURCES")
        assert {"GOLD", "SILVER", "OIL"} == {i["symbol"] for i in res["symbols"]}
        idx = next(g for g in data["groups"] if g["name"] == "INDICES")
        assert {"QQQUSDT", "SPYUSDT"} == {i["symbol"] for i in idx["symbols"]}

    # --- klines (chart data) ---
    def test_03_klines_btc(self, http):
        r = http.get(f"{API}/klines/BTCUSDT?limit=50", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["symbol"] == "BTCUSDT"
        assert isinstance(d["candles"], list) and len(d["candles"]) > 0
        c = d["candles"][0]
        for k in ("timestamp", "open", "high", "low", "close"):
            assert k in c

    def test_04_klines_gold_no_crash(self, http):
        r = http.get(f"{API}/klines/GOLD?limit=20", timeout=25)
        assert r.status_code == 200
        d = r.json()
        assert d["symbol"] == "GOLD"
        assert isinstance(d["candles"], list)
        # commodity feed may return 0 outside of trading hours - accept but not crash

    # --- strategies ---
    def test_05_strategies_list(self, http):
        r = http.get(f"{API}/strategies", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "strategies" in d and "enabled" in d and "signals_enabled" in d
        ids = [s["id"] for s in d["strategies"]]
        assert "scalping_4_rules" in ids
        assert "rsi_only" in ids
        # metadata fields
        s0 = next(s for s in d["strategies"] if s["id"] == "scalping_4_rules")
        for k in ("name", "description", "timeframe", "current_params"):
            assert k in s0, f"missing {k} in metadata"

    def test_06_builder_options(self, http):
        r = http.get(f"{API}/strategies/builder-options", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert set(["<", ">", "cross_above", "cross_below"]).issubset(set(d["operators"]))
        assert set(["rsi", "ema_fast", "ema_slow", "price"]).issubset(set(d["indicators"]))

    def test_07_rule_states_btc(self, http):
        # give scanner a moment
        r = http.get(f"{API}/rule-states?symbol=BTCUSDT", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["symbol"] == "BTCUSDT"
        # states may be empty briefly at cold start, but structure must exist
        assert isinstance(d["states"], dict)
        # if populated, check rules structure has long/short booleans
        for sid, s in d["states"].items():
            for rule in s.get("rules", []):
                assert isinstance(rule.get("long"), bool)
                assert isinstance(rule.get("short"), bool)
                assert "id" in rule

    # --- settings persistence ---
    def test_08_settings_get(self, http):
        r = http.get(f"{API}/settings", timeout=10)
        assert r.status_code == 200
        d = r.json()
        for k in ("enabled_strategies", "strategy_signals_enabled",
                  "strategy_params", "coin_params", "notifications",
                  "custom_sessions", "pre_signal_enabled"):
            assert k in d, f"missing key {k}"

    def test_09_settings_post_persists(self, http):
        payload = {
            "enabled_strategies": ["scalping_4_rules", "rsi_only"],
            "strategy_signals_enabled": {"scalping_4_rules": True, "rsi_only": False},
            "coin_params": {"scalping_4_rules": {"BTCUSDT": {"rsi_period": 20}}},
            "strategy_params": {"rsi_only": {"rsi_period": 21}},
            "notifications": {"BTCUSDT": True, "ETHUSDT": False},
        }
        r = http.post(f"{API}/settings", json=payload, timeout=10)
        assert r.status_code == 200
        got = http.get(f"{API}/settings", timeout=10).json()
        assert got["enabled_strategies"] == ["scalping_4_rules", "rsi_only"]
        assert got["strategy_signals_enabled"]["rsi_only"] is False
        assert got["coin_params"]["scalping_4_rules"]["BTCUSDT"]["rsi_period"] == 20
        assert got["strategy_params"]["rsi_only"]["rsi_period"] == 21
        assert got["notifications"]["ETHUSDT"] is False

    # --- custom strategy CRUD ---
    def test_10_create_custom_strategy(self, http):
        cid = f"custom_test_{uuid.uuid4().hex[:6]}"
        definition = {
            "id": cid,
            "name": "TEST Custom",
            "description": "unit-test",
            "timeframe": "1m",
            "indicators": {"ema_fast_period": 9, "ema_slow_period": 50, "rsi_period": 14},
            "long_rules": [{"indicator": "rsi", "op": "<", "value": 30, "label": "RSI <30"}],
            "short_rules": [{"indicator": "rsi", "op": ">", "value": 70, "label": "RSI >70"}],
            "sl_mode": "structure", "sl_ticks": 4, "structure_lookback": 10, "crv_target": 2,
        }
        r = http.post(f"{API}/strategies/custom", json=definition, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "success"
        assert d["id"] == cid
        self.__class__.created_custom_ids.append(cid)

        # GET strategies must now include this one, is_custom=True
        strats = http.get(f"{API}/strategies", timeout=10).json()
        ids = [s["id"] for s in strats["strategies"]]
        assert cid in ids
        me = next(s for s in strats["strategies"] if s["id"] == cid)
        assert me.get("is_custom") is True
        # auto-enabled in tabs
        assert cid in strats["enabled"]

    def test_11_delete_custom_strategy(self, http):
        cid = f"custom_del_{uuid.uuid4().hex[:6]}"
        http.post(f"{API}/strategies/custom", json={
            "id": cid, "name": "toDel", "description": "d",
            "indicators": {"ema_fast_period": 9, "ema_slow_period": 50, "rsi_period": 14},
            "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}],
            "short_rules": [{"indicator": "rsi", "op": ">", "value": 70}],
        }, timeout=10)
        r = http.delete(f"{API}/strategies/custom/{cid}", timeout=10)
        assert r.status_code == 200
        assert r.json()["status"] == "success"

        strats = http.get(f"{API}/strategies", timeout=10).json()
        ids = [s["id"] for s in strats["strategies"]]
        assert cid not in ids
        assert cid not in strats["enabled"]

    # --- autotrade ---
    def test_12_autotrade_config_get(self, http):
        r = http.get(f"{API}/autotrade/config", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "config" in d and "defaults" in d and "bitunix_configured" in d
        # defaults contract
        for k in ("enabled", "max_capital", "leverage", "sl_mode", "tp1_crv", "tp_full_crv",
                  "tp1_close_percent", "breakeven_enabled"):
            assert k in d["defaults"]
        assert d["defaults"]["enabled"] is False  # default OFF
        assert d["config"].get("mode") in ("paper", "live")

    def test_13_autotrade_coin_persists(self, http):
        payload = {
            "enabled": True, "max_capital": 250, "leverage": 15,
            "tp1_crv": 1.0, "tp_full_crv": 3.0, "tp1_close_percent": 60,
            "breakeven_enabled": True, "sl_mode": "structure",
        }
        r = http.post(f"{API}/autotrade/coin/BTCUSDT", json=payload, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["coin"] == "BTCUSDT"
        assert d["config"]["enabled"] is True
        assert d["config"]["max_capital"] == 250

        got = http.get(f"{API}/autotrade/config", timeout=10).json()
        c = got["config"]["coins"]["BTCUSDT"]
        assert c["leverage"] == 15
        assert c["tp1_close_percent"] == 60
        assert c["breakeven_enabled"] is True

    def test_14_autotrade_mode_toggle(self, http):
        r = http.post(f"{API}/autotrade/config", json={"mode": "paper"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["config"]["mode"] == "paper"

    def test_15_autotrade_trades(self, http):
        r = http.get(f"{API}/autotrade/trades?limit=5", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["trades"], list)

    def test_16_autotrade_balance_paper(self, http):
        # force paper first
        http.post(f"{API}/autotrade/config", json={"mode": "paper"}, timeout=10)
        r = http.get(f"{API}/autotrade/balance", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d.get("mode") == "paper"
        assert "realized_pnl" in d
        assert "open_trades" in d
        assert "closed_trades" in d

    # --- signals & performance ---
    def test_17_signals_today_only(self, http):
        r = http.get(f"{API}/signals?limit=20", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["signals"], list)
        # if any signals exist, all must be today's Berlin date
        sess = http.get(f"{API}/session/status", timeout=10).json()
        today = sess["berlin_date"]
        for s in d["signals"]:
            assert s.get("trade_date") == today

    def test_18_performance_by_strategy(self, http):
        r = http.get(f"{API}/performance", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["performance"], list)
        for p in d["performance"]:
            assert "symbol" in p
            assert "by_strategy" in p

    def test_19_analytics_daily(self, http):
        r = http.get(f"{API}/analytics/daily?days=7", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["daily"], list)
        assert isinstance(d["trade_stats"], list)

    def test_20_analytics_time_based(self, http):
        r = http.get(f"{API}/analytics/time-based/BTCUSDT", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["symbol"] == "BTCUSDT"
        assert isinstance(d["time_analytics"], list)
        assert isinstance(d["best_hours"], list)

    # --- session ---
    def test_21_session_status_shape(self, http):
        r = http.get(f"{API}/session/status", timeout=10)
        assert r.status_code == 200
        d = r.json()
        for k in ("is_active", "current_session", "custom_sessions",
                  "pre_signal_enabled", "berlin_time", "berlin_date"):
            assert k in d
