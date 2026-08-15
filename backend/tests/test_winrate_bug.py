"""Test the win-rate bug fix: signals emitted with take_profit_1/stop_loss keys
must be re-hydrated after restart and marked win/loss by evaluate_open_signals()."""
import os, time, uuid, subprocess
from datetime import datetime, timezone, timedelta
import pytest, requests
from pymongo import MongoClient

# Dieser Test startet das Backend mehrfach neu und killt damit parallel
# laufende Tests (xdist) – nur gezielt mit RUN_RESTART_TESTS=1 ausführen.
pytestmark = pytest.mark.skipif(os.environ.get("RUN_RESTART_TESTS") != "1",
                                reason="Backend-Neustart – nur mit RUN_RESTART_TESTS=1")

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-ml.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

client = MongoClient(MONGO_URL)
db = client[DB_NAME]


def berlin_today():
    tz = timezone(timedelta(hours=1))  # CET, roughly (production uses zoneinfo Europe/Berlin)
    # Use offset that matches server behaviour – ask backend
    r = requests.get(f"{BASE_URL}/api/session/status", timeout=10)
    if r.status_code == 200:
        js = r.json()
        if "berlin_date" in js:
            return js["berlin_date"]
    return datetime.now(tz).strftime("%Y-%m-%d")


TEST_IDS = []


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    # remove any prior TESTSIG
    db.signals.delete_many({"id": {"$regex": "^TESTSIG-"}})
    yield
    db.signals.delete_many({"id": {"$regex": "^TESTSIG-"}})
    # if signals collection contains no other today's real signals -> reset perf
    today = berlin_today()
    remaining = db.signals.count_documents({"trade_date": today})
    if remaining == 0:
        db.performance.delete_many({})
    else:
        # remove BTCUSDT perf which we polluted
        db.performance.delete_many({"symbol": "BTCUSDT"})
    subprocess.run(["sudo", "supervisorctl", "restart", "backend"], check=False)
    time.sleep(5)


def insert_signal(symbol, typ, tp1, sl, sid_suffix):
    today = berlin_today()
    sid = f"TESTSIG-{sid_suffix}-{uuid.uuid4().hex[:6]}"
    TEST_IDS.append(sid)
    doc = {
        "id": sid,
        "symbol": symbol,
        "type": typ,
        "signal_class": "SIGNAL",
        "entry_price": 60000,
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_full": tp1 + 100 if typ == "LONG" else tp1 - 100,
        "crv": 2,
        "strategy_id": "scalping_4_rules",
        "strategy_name": "Scalping",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trade_date": today,
        "status": "active",
    }
    db.signals.insert_one(doc)
    return sid


def restart_and_wait():
    subprocess.run(["sudo", "supervisorctl", "restart", "backend"], check=True)
    # wait for backend to boot + scanner tick
    for _ in range(20):
        time.sleep(3)
        try:
            r = requests.get(f"{BASE_URL}/api/health", timeout=3)
            if r.status_code == 200:
                break
        except Exception:
            continue
    # extra wait for bootstrap + scanner iterations
    time.sleep(35)


def test_winrate_bugfix_full_flow():
    # 1) LONG win case (tp1=200, sl=100 → price is way above tp1 → win immediately)
    sid_win = insert_signal("BTCUSDT", "LONG", tp1=200, sl=100, sid_suffix="LONGWIN")
    # 2) SHORT edge case (spec description)
    sid_short = insert_signal("BTCUSDT", "SHORT", tp1=999999, sl=1, sid_suffix="SHORT")
    # 3) LONG loss (sl above price)
    sid_loss = insert_signal("BTCUSDT", "LONG", tp1=999999, sl=888888, sid_suffix="LONGLOSS")

    restart_and_wait()

    # Check log for rehydration
    logs = subprocess.run(["tail", "-n", "300", "/var/log/supervisor/backend.err.log"],
                         capture_output=True, text=True).stdout
    print("---BACKEND LOG TAIL---")
    print(logs[-3000:])
    assert "Re-hydrated" in logs, "Rehydration log message missing"

    # Verify DB
    win = db.signals.find_one({"id": sid_win})
    loss = db.signals.find_one({"id": sid_loss})
    short = db.signals.find_one({"id": sid_short})

    print(f"WIN doc: result={win.get('result')} status={win.get('status')}")
    print(f"LOSS doc: result={loss.get('result')} status={loss.get('status')}")
    print(f"SHORT doc: result={short.get('result')} status={short.get('status')}")

    assert win.get("result") == "win", f"LONG win signal not marked win: {win.get('result')}"
    assert win.get("status") == "closed"
    assert loss.get("result") == "loss", f"LONG loss signal not marked loss: {loss.get('result')}"

    # Performance endpoint
    r = requests.get(f"{BASE_URL}/api/performance", timeout=10)
    assert r.status_code == 200
    perf = r.json()
    btc = None
    lst = perf.get("performance") if isinstance(perf, dict) else perf
    if isinstance(lst, list):
        btc = next((p for p in lst if p.get("symbol") == "BTCUSDT"), None)
    elif isinstance(perf, dict):
        btc = perf.get("BTCUSDT")
    print(f"Perf BTCUSDT: {btc}")
    assert btc is not None
    assert btc.get("wins", 0) >= 1
    assert btc.get("losses", 0) >= 1


def test_regression_capital_and_balance():
    r = requests.get(f"{BASE_URL}/api/autotrade/capital", timeout=10)
    assert r.status_code == 200
    js = r.json()
    assert "allocation" in js
    assert "live" in js["allocation"] and "paper" in js["allocation"]

    r = requests.get(f"{BASE_URL}/api/autotrade/balance", timeout=10)
    assert r.status_code == 200
    assert "allocation" in r.json()

    r = requests.get(f"{BASE_URL}/api/autotrade/trades", timeout=10)
    assert r.status_code == 200
    trades = r.json()
    lst = trades if isinstance(trades, list) else trades.get("trades", [])
    if lst:
        # spec: computed.pnl_pct must be present when trades exist
        t0 = lst[0]
        assert "computed" in t0 or "pnl_pct" in t0
