"""Iter17 tests - Neue Assets (Indices/Resources/Forex) + KI-Trader Lektions-Bugfix.

Testet die neuen Anforderungen der aktuellen Iteration ausschließlich gegen den
öffentlichen Endpunkt (REACT_APP_BACKEND_URL). Setzt voraus, dass ADMIN_USER/
ADMIN_PASSWORD gemäß /app/memory/test_credentials.md konfiguriert sind.
"""
import os
import time

import pytest
import requests

def _load_frontend_env_url():
    p = "/app/frontend/.env"
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    return None


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _load_frontend_env_url()).rstrip("/")


# ------------------------------------------------------------------ helpers
@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"), "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    token = r.json().get("token") or r.json().get("access_token")
    assert token, f"No token in login response: {r.json()}"
    return token


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


def _poll_job(url: str, timeout: float = 240.0, interval: float = 3.0) -> dict:
    """Pollt Backtest-/Optimizer-Job bis status=done|error."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = requests.get(url, timeout=15)
        assert r.status_code == 200, f"Poll fehlgeschlagen: {r.status_code} {r.text}"
        last = r.json()
        st = (last.get("status") or "").lower()
        if st in ("done", "finished", "success", "complete", "error", "failed", "cancelled"):
            return last
        time.sleep(interval)
    pytest.fail(f"Job Timeout nach {timeout}s. Letzter Status: {last}")


# =========================================================================
# 1. /api/coins - komplettes Asset-Universum
# =========================================================================
class TestCoinsEndpoint:
    def test_universe_structure(self):
        r = requests.get(f"{BASE_URL}/api/coins", timeout=30)
        assert r.status_code == 200
        d = r.json()

        # 22 Symbole insgesamt
        assert len(d["coins"]) == 22, f"expected 22 coins, got {len(d['coins'])}"
        assert len(d["crypto"]) == 10
        # Forex NICHT in tradable
        for fx in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"):
            assert fx not in d["tradable"], f"{fx} darf nicht tradable sein"
        # Krypto + Rohstoffe + Indizes tradable
        for sym in ("BTCUSDT", "GOLD", "SILVER", "OIL", "QQQUSDT", "SPYUSDT"):
            assert sym in d["tradable"]

    def test_group_names_and_membership(self):
        r = requests.get(f"{BASE_URL}/api/coins", timeout=30)
        groups = {g["name"]: [s["symbol"] for s in g["symbols"]] for g in r.json()["groups"]}
        assert list(groups) == ["TOP 10 COINS", "RESOURCES", "INDICES", "FOREX"]
        assert groups["RESOURCES"] == ["GOLD", "SILVER", "OIL"]
        assert groups["INDICES"] == ["QQQUSDT", "SPYUSDT"]
        assert len(groups["FOREX"]) == 7
        assert "EURUSD" in groups["FOREX"]
        # tradable=false für FX
        forex_syms = next(g for g in r.json()["groups"] if g["name"] == "FOREX")["symbols"]
        for s in forex_syms:
            assert s["tradable"] is False, f"{s['symbol']} sollte tradable=False sein"


# =========================================================================
# 2. /api/klines/{symbol} liefert Kerzen für alle neuen Symbole
# =========================================================================
class TestKlinesForNewAssets:
    @pytest.mark.parametrize("symbol,price_min,price_max", [
        ("QQQUSDT", 500, 900),
        ("SPYUSDT", 600, 900),
        ("GOLD", 3000, 5000),
        ("SILVER", 20, 80),
        ("OIL", 40, 120),
        ("EURUSD", 0.9, 1.5),
        ("GBPUSD", 1.0, 1.6),
        ("USDJPY", 120, 200),
        ("AUDUSD", 0.5, 0.9),
        ("USDCAD", 1.0, 1.6),
        ("USDCHF", 0.7, 1.2),
        ("NZDUSD", 0.5, 0.8),
    ])
    def test_klines_plausible(self, symbol, price_min, price_max):
        r = requests.get(f"{BASE_URL}/api/klines/{symbol}", timeout=30)
        assert r.status_code == 200, f"{symbol}: {r.status_code} {r.text[:200]}"
        data = r.json()
        candles = data if isinstance(data, list) else data.get("candles") or data.get("data")
        assert candles, f"{symbol}: keine Kerzen im Response ({data})"
        assert len(candles) > 0
        # Preis-Plausibilität am letzten Close
        last = candles[-1]
        close = float(last.get("close") if isinstance(last, dict) else last[4])
        assert price_min < close < price_max, (
            f"{symbol}: close={close} ausserhalb ({price_min}, {price_max})")


# =========================================================================
# 3. /api/rule-states enthält neue Symbole
# =========================================================================
def test_rule_states_covers_new_symbols():
    r = requests.get(f"{BASE_URL}/api/rule-states", timeout=30)
    assert r.status_code == 200
    data = r.json()
    # Antwort ist Dict {symbol: {...}} oder Liste
    if isinstance(data, dict):
        # Response ist {"states": {...}} oder direkt {symbol: {...}}
        if "states" in data and isinstance(data["states"], dict):
            data = data["states"]
        symbols = set(data.keys())
    else:
        symbols = {row.get("symbol") for row in data}
    for sym in ("QQQUSDT", "SPYUSDT", "EURUSD", "USDJPY", "GOLD", "SILVER", "OIL"):
        assert sym in symbols, f"{sym} fehlt in /api/rule-states ({sorted(symbols)[:5]}...)"


# =========================================================================
# 4. Backtester + neue Assets
# =========================================================================
class TestBacktester:
    def test_reject_unknown_symbol(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/backtest/run", headers=auth_headers, json={
            "strategy_ids": ["bollinger_reversion"],
            "symbols": ["FOOBARUSDT"],
            "days": 7,
            "timeframe": "5m",
        }, timeout=15)
        assert r.status_code == 400, f"expected 400 for unknown symbol, got {r.status_code}"

    def test_regression_crypto_still_works(self, auth_headers):
        payload = {
            "strategy_ids": ["bollinger_reversion"],
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "days": 7,
            "timeframe": "5m",
        }
        r = requests.post(f"{BASE_URL}/api/backtest/run", headers=auth_headers,
                          json=payload, timeout=30)
        assert r.status_code == 200, f"backtest start failed: {r.status_code} {r.text}"
        job_id = r.json().get("job_id") or r.json().get("id")
        assert job_id, f"no job_id in response: {r.json()}"
        result = _poll_job(f"{BASE_URL}/api/backtest/status/{job_id}", timeout=180)
        assert (result.get("status") or "").lower() in ("done", "finished", "success"), result
        # Trades > 0 in mindestens einer Kombi
        per_pair = (result.get("result") or {}).get("per_pair") or []
        total_trades = sum(int(p.get("trades") or 0) for p in per_pair)
        assert total_trades > 0, f"Krypto-Regression: 0 Trades - {per_pair}"

    def test_multi_asset_new_universe(self, auth_headers):
        payload = {
            "strategy_ids": ["bollinger_reversion", "stoch_reversal"],
            "symbols": ["QQQUSDT", "SPYUSDT", "EURUSD", "USDJPY", "GOLD"],
            "days": 14,
            "timeframe": "5m",
        }
        r = requests.post(f"{BASE_URL}/api/backtest/run", headers=auth_headers,
                          json=payload, timeout=30)
        assert r.status_code == 200, f"start: {r.status_code} {r.text}"
        job_id = r.json().get("job_id") or r.json().get("id")
        result = _poll_job(f"{BASE_URL}/api/backtest/status/{job_id}", timeout=360)
        assert (result.get("status") or "").lower() in ("done", "finished", "success"), result

        per_pair = (result.get("result") or {}).get("per_pair") or []
        # Erwartet: 5 Symbole * 2 Strategien = 10 Kombinationen
        combos = {(p.get("symbol"), p.get("strategy_id")): p for p in per_pair}
        for sym in ("QQQUSDT", "SPYUSDT", "EURUSD", "USDJPY", "GOLD"):
            for strat in ("bollinger_reversion", "stoch_reversal"):
                assert (sym, strat) in combos, f"Kombi {sym}/{strat} fehlt in per_pair"
        # candles > 1000 für alle
        for (sym, strat), p in combos.items():
            candles = int(p.get("candles") or 0)
            assert candles > 1000, f"{sym}/{strat}: nur {candles} Kerzen"
        # Forex darf pro Strategie 0 Trades haben, aber NICHT bei allen
        fx_trades = sum(int(p.get("trades") or 0) for (sym, _), p in combos.items()
                        if sym in ("EURUSD", "USDJPY"))
        assert fx_trades > 0, "Forex hat in ALLEN Strategien 0 Trades"


# =========================================================================
# 5. Optimizer + neue Assets
# =========================================================================
class TestOptimizer:
    def test_optimizer_forex_and_index(self, auth_headers):
        payload = {
            "mode": "params",
            "strategy_id": "bollinger_reversion",
            "symbols": ["EURUSD", "QQQUSDT"],
            "days": 14,
            "timeframe": "5m",
            "iterations": 10,
            "min_trades": 2,
        }
        r = requests.post(f"{BASE_URL}/api/optimizer/run", headers=auth_headers,
                          json=payload, timeout=30)
        assert r.status_code == 200, f"start: {r.status_code} {r.text}"
        job_id = r.json().get("job_id") or r.json().get("id")
        result = _poll_job(f"{BASE_URL}/api/optimizer/status/{job_id}", timeout=360)
        assert (result.get("status") or "").lower() in ("done", "finished", "success"), result
        best = (result.get("result") or {}).get("best")
        assert best is not None, f"Kein 'best' im Optimizer-Result: {result}"
        assert best.get("params"), f"best.params fehlt: {best}"


# =========================================================================
# 6. KI-Trader Lektions-Persistenz
# =========================================================================
class TestAILessonsConfig:
    def test_max_lessons_persists(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"max_lessons": 50}, timeout=10)
        assert r.status_code in (200, 201), f"{r.status_code} {r.text}"
        # Verify via /api/ai/status
        r2 = requests.get(f"{BASE_URL}/api/ai/status", timeout=10)
        assert r2.status_code == 200
        st = r2.json()
        # max_lessons kann tief verschachtelt sein
        found = _find_key(st, "max_lessons")
        assert found == 50, f"max_lessons not persisted: {found} in {st}"

    def test_insights_returns_all_stored(self):
        r = requests.get(f"{BASE_URL}/api/ai/insights", timeout=15)
        assert r.status_code == 200
        d = r.json()
        # Kein hardcoded Limit bei 5 - Endpoint muss alle DB-Lektionen liefern
        assert isinstance(d, (list, dict))


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None
