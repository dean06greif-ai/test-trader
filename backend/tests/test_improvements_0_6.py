"""Regressionstests für die 0.6-Verbesserungen:

  * Unbegrenzte Backup-Keys (_BACKUP, _BACKUP2, … _BACKUP17, …) für alle Provider
  * PnL-Wahrheit: Börsen-uPnL (Bitunix) überschreibt die Scanner-Schätzung
  * Watchdog: übernommene Positionen sind klar als 'Manuell (Bitunix)' markiert
  * Session-Levels (Asia/London/NY) & Umverteilungszonen (reine Funktionen)
"""
import asyncio
import os

from core.utils import _enrich_trade
from services import ai_providers, session_levels
from services.position_watchdog import PositionWatchdog


# --------------------- Backup-Keys (unbegrenzt) ---------------------------

def test_backup_env_names_unlimited(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY_BACKUP", "k1")
    monkeypatch.setenv("CEREBRAS_API_KEY_BACKUP2", "k2")
    monkeypatch.setenv("CEREBRAS_API_KEY_BACKUP17", "k17")
    names = ai_providers._backup_env_names("CEREBRAS_API_KEY")
    assert names[0] == "CEREBRAS_API_KEY_BACKUP"
    assert "CEREBRAS_API_KEY_BACKUP2" in names
    assert "CEREBRAS_API_KEY_BACKUP17" in names
    # numerische Reihenfolge (2 vor 17)
    assert names.index("CEREBRAS_API_KEY_BACKUP2") < names.index("CEREBRAS_API_KEY_BACKUP17")


def test_provider_keys_pick_up_high_backup_numbers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_BACKUP", "or-b1")
    monkeypatch.setenv("OPENROUTER_API_KEY_BACKUP12", "or-b12")
    keys = ai_providers.provider_keys("openrouter")
    assert keys[0] == "or-primary"
    assert "or-b1" in keys and "or-b12" in keys


def test_backup_names_ignore_foreign_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_BACKUPX", "junk")   # kein numerisches Suffix
    names = ai_providers._backup_env_names("GROQ_API_KEY")
    assert "GROQ_API_KEY_BACKUPX" not in names


# --------------------- PnL: Börsen-Wahrheit vor Scanner --------------------

def _open_trade(**kw):
    t = {"status": "open", "mode": "live", "side": "LONG", "entry": 100.0,
         "qty": 2.0, "qty_remaining": 2.0, "realized_pnl": -0.1, "risk": 1.0,
         "sl": 99.0, "leverage": 10}
    t.update(kw)
    return t


def test_enrich_trade_prefers_exchange_upnl():
    # Scanner-Preis würde +170 suggerieren, Börse sagt -2 (Gold-Bug)
    t = _enrich_trade(_open_trade(), current_price=185.0,
                      exchange={"upnl": -2.0, "qty": 2.0})
    c = t["computed"]
    assert c["unrealized_pnl"] == -2.0
    assert c["live_pnl"] == -2.1
    assert c["live_pnl_source"] == "bitunix"
    # abgeleiteter Kurs liegt auf der Börsen-Seite (Entry - 1$/Einheit)
    assert abs(c["current_price"] - 99.0) < 1e-6


def test_enrich_trade_scales_partial_position():
    # Nach TP1 hält die Website nur noch die Hälfte der Börsen-Menge
    t = _enrich_trade(_open_trade(qty_remaining=1.0), current_price=None,
                      exchange={"upnl": 4.0, "qty": 2.0})
    c = t["computed"]
    assert c["unrealized_pnl"] == 2.0  # 4$ uPnL anteilig auf 50% Rest-Menge


def test_enrich_trade_scanner_fallback_without_exchange():
    t = _enrich_trade(_open_trade(), current_price=101.0)
    c = t["computed"]
    assert c["unrealized_pnl"] == 2.0
    assert c["live_pnl_source"] == "scanner"


def test_enrich_trade_short_side_exchange():
    t = _enrich_trade(_open_trade(side="SHORT"), current_price=None,
                      exchange={"upnl": 6.0, "qty": 2.0})
    c = t["computed"]
    assert c["unrealized_pnl"] == 6.0
    assert abs(c["current_price"] - 97.0) < 1e-6  # Entry - upnl/qty bei SHORT


# --------------------- Watchdog: Manuell-Markierung ------------------------

class _FakeColl:
    def __init__(self):
        self.inserted = []

    async def insert_one(self, doc):
        self.inserted.append(doc)


class _FakeDB:
    def __init__(self):
        self.auto_trades = _FakeColl()


class _FakeClient:
    async def get_mark_price(self, symbol):
        return 100.0


def test_watchdog_adopt_marks_manual_trade():
    wd = PositionWatchdog()
    wd.db = _FakeDB()
    wd.client = _FakeClient()
    pos = {"bitunix_symbol": "BTCUSDT", "side": "LONG", "qty": 0.5,
           "entry": 100.0, "position_id": "p1", "leverage": 10, "margin": 5}
    trade = asyncio.run(wd._adopt("BTCUSDT", pos))
    assert trade["strategy_name"] == "Manuell (Bitunix)"
    assert trade["manual_trade"] is True
    assert trade["strategy_id"] == "external"       # Rückwärtskompatibel (clear/Filter)
    assert trade["external_adopted"] is True
    assert wd.db.auto_trades.inserted


def test_watchdog_default_does_not_manage_external():
    wd = PositionWatchdog()
    assert wd.settings.get("manage_external") is False


# --------------------- Session-Levels & Zonen ------------------------------

def _mk_candles(start_ms, n, price=100.0, vol=1.0, spread=1.0):
    out = []
    for i in range(n):
        p = price + (i % 5) * 0.1
        out.append({"timestamp": start_ms + i * 60_000, "open": p,
                    "high": p + spread, "low": p - spread, "close": p,
                    "volume": vol})
    return out


def _day_ms(hour):
    # fixer UTC-Tag: 2026-06-10
    import datetime as dt
    return int(dt.datetime(2026, 6, 10, hour, tzinfo=dt.timezone.utc).timestamp() * 1000)


def test_session_levels_detects_asia_and_london():
    candles = (_mk_candles(_day_ms(1), 120, price=100) +      # Asia
               _mk_candles(_day_ms(8), 120, price=110) +      # London
               _mk_candles(_day_ms(13), 120, price=120))      # NY (+London-Overlap-Ende)
    rows = session_levels.session_levels(candles)
    names = {r["session"] for r in rows}
    assert {"Asia", "London", "NY"} <= names
    asia = next(r for r in rows if r["session"] == "Asia")
    assert asia["high"] <= 102.5 and asia["low"] >= 98.0


def test_session_levels_text_contains_sweep_hint():
    candles = _mk_candles(_day_ms(1), 120, price=100)
    txt = session_levels.levels_text(candles, price=105.0)  # klar über Asia-High
    assert "Session-Levels" in txt and "Sweep?" in txt


def test_volume_zones_finds_high_volume_cluster():
    # 23h normales Volumen verteilt, 1h massives Volumen eng um 100
    base = _mk_candles(_day_ms(0), 600, price=95, vol=1.0, spread=6.0)
    cluster = _mk_candles(_day_ms(10), 120, price=100, vol=50.0, spread=0.3)
    zones = session_levels.volume_zones(base + cluster)
    assert zones, "mindestens eine Zone erwartet"
    z = zones[0]
    assert z["low"] <= 100 <= z["high"]
    assert z["vol_share"] > 10


def test_volume_zones_empty_on_flat_or_short_data():
    assert session_levels.volume_zones([]) == []
    assert session_levels.volume_zones(_mk_candles(_day_ms(0), 30)) == []
