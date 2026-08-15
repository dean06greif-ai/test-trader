"""Regressionstests für die KI-Trader-Qualitäts-Fixes.

Hintergrund (Bug-Report des Traders): Der KI Trader wurde nach Updates deutlich
schlechter. Ursache-Analyse:
  1. Die "Liquidations-Cluster" waren nie echte Daten, sondern eine reine
     Formel (Preis ± 1/Hebel) – sie existieren IMMER, für JEDEN Coin. Der
     Prompt nannte sie zusätzlich "Magnete -> Umkehrpunkte" und hat die KI so
     systematisch in Fade-Trades an erfundenen Levels gelockt.
  2. Trader-Lektionen widersprachen sich (Auto-Lev 200x vs. fester Hebel 10,
     Break-Even 30/35/40%) – alle flossen gleichzeitig in den Prompt.
  3. Kosten: jeder Analyse-Lauf trug große statische Blöcke; 30-min-Intervall
     × 3 Gruppen = viele teure LLM-Calls.

Fixes: use_heatmap_data (Standard AUS) / use_liquidation_data (Standard AN),
ehrliche Kennzeichnung der Modell-Cluster, Lektions-Konsolidierung (neueste
Trader-Anweisung gewinnt), lean_prompt + Smart-Skip zur Kostenreduktion.
"""
import asyncio
import inspect

from services import ai_lessons
from services.ai_engine import AIEngine, DEFAULT_AI_CONFIG
from services.ai_lessons import (active_lessons, consolidate_conflicts,
                                 lesson_topics, lessons_text)
from services.liquidity_data import _model_clusters


# --------------------------------------------------------------------------- #
#  Hilfen
# --------------------------------------------------------------------------- #
class _FakeSettings:
    async def update_one(self, *a, **k):
        return None


class _FakeDB:
    settings = _FakeSettings()


def _engine() -> AIEngine:
    eng = AIEngine()
    eng.db = _FakeDB()
    return eng


def L(title, locked=True, updated_at="2026-01-01T00:00:00", detail=None, lid=None):
    out = {"title": title, "detail": detail or f"detail {title}",
           "locked": locked, "updated_at": updated_at, "origin": "user" if locked else "ai"}
    if lid:
        out["id"] = lid
    return out


# --------------------------------------------------------------------------- #
#  1) Heatmap-/Liquidations-Schalter
# --------------------------------------------------------------------------- #
def test_default_config_heatmap_off_liquidation_on():
    assert DEFAULT_AI_CONFIG["use_heatmap_data"] is False
    assert DEFAULT_AI_CONFIG["use_liquidation_data"] is True


def test_default_config_cost_savers_on():
    assert DEFAULT_AI_CONFIG["lean_prompt"] is True
    assert DEFAULT_AI_CONFIG["smart_skip"] is True
    assert 0.02 <= DEFAULT_AI_CONFIG["smart_skip_move_pct"] <= 2.0


def test_update_config_accepts_new_toggles():
    eng = _engine()
    cfg = asyncio.run(eng.update_config({
        "use_heatmap_data": True, "use_liquidation_data": False,
        "lean_prompt": False, "smart_skip": False, "smart_skip_move_pct": 0.5,
    }))
    assert cfg["use_heatmap_data"] is True
    assert cfg["use_liquidation_data"] is False
    assert cfg["lean_prompt"] is False
    assert cfg["smart_skip"] is False
    assert cfg["smart_skip_move_pct"] == 0.5


def test_update_config_clamps_move_pct():
    eng = _engine()
    cfg = asyncio.run(eng.update_config({"smart_skip_move_pct": 99}))
    assert cfg["smart_skip_move_pct"] == 2.0


def test_model_clusters_are_marked_as_modelled():
    out = _model_clusters(50000.0, 1e9)
    assert out.get("modelled") is True
    assert all(c.get("modelled") for c in out["below_price"])
    assert all(c.get("modelled") for c in out["above_price"])


def test_liquidity_prompt_no_longer_sells_model_clusters_as_magnets():
    """Der Prompt darf die Modell-Cluster nicht mehr als 'Magnete/Umkehrpunkte'
    verkaufen; seit Iteration 2 werden statt der Formel-Cluster nur noch
    GEMESSENE Liquidationen (echte Force-Orders) verwendet."""
    src = inspect.getsource(AIEngine._liquidity_block)
    assert "KEINE Ausbrüche" not in src
    assert "GEMESSENE LIQUIDATIONEN" in src
    assert "use_liquidation_data" in src and "use_heatmap_data" in src


def test_extra_blocks_lean_gates_static_content():
    src = inspect.getsource(AIEngine._analysis_extra_blocks)
    assert src.count('self.config.get("lean_prompt", True)') >= 2
    assert "PLATTFORM-WISSEN" in src


# --------------------------------------------------------------------------- #
#  2) Lektions-Konsolidierung (Widersprüche -> neueste Trader-Anweisung gilt)
# --------------------------------------------------------------------------- #
def test_lesson_topics_matches_title_only():
    assert "leverage" in lesson_topics(L("STRIKTE NUTZUNG DES AUTO-LEVERAGE"))
    assert "break_even" in lesson_topics(L("BREAK-EVEN-UMSETZUNG FÜR ALLE RICHTUNGEN"))
    assert "cooldown" in lesson_topics(L("COOLDOWN-DISZIPLIN BEI VERLUSTSERIEN"))
    # Beiläufige Erwähnung im Detail-Text erzeugt KEIN Thema
    l = L("REGIME-4-FOKUS", detail="... mit geringem Hebel und Cooldown ...")
    assert lesson_topics(l) == set()


def test_leverage_conflict_newest_trader_instruction_wins():
    lessons = [
        L("AUTO-LEVERAGE-KONFIGURATION-ERZWUNGEN", updated_at="2026-05-01T10:00:00", lid="a"),
        L("AUTO-LEVERAGE UND HEBEL-OPTIMIERUNG: fester Hebel 10 bis ich sage",
          updated_at="2026-06-01T10:00:00", lid="b"),
        L("STRIKTE NUTZUNG DES AUTO-LEVERAGE", updated_at="2026-04-01T10:00:00", lid="c"),
    ]
    out, conflicts = consolidate_conflicts(lessons)
    by_id = {l["id"]: l for l in out}
    assert not by_id["b"].get("superseded")          # neueste gewinnt
    assert by_id["a"].get("superseded") and by_id["a"]["superseded_by"] == "b"
    assert by_id["c"].get("superseded") and by_id["c"]["superseded_by"] == "b"
    assert len(conflicts) == 1 and conflicts[0]["topic"] == "leverage"
    assert len(conflicts[0]["superseded"]) == 2


def test_break_even_conflict_resolved_by_recency():
    lessons = [
        L("STOP-LOSS UND BREAK-EVEN STRIKT UMSETZEN (30%/40%)",
          updated_at="2026-03-01T00:00:00", lid="be30"),
        L("BREAK-EVEN-UMSETZUNG FÜR ALLE RICHTUNGEN – STRENGER (35%)",
          updated_at="2026-05-20T00:00:00", lid="be35"),
    ]
    out, conflicts = consolidate_conflicts(lessons)
    by_id = {l["id"]: l for l in out}
    assert not by_id["be35"].get("superseded")
    assert by_id["be30"].get("superseded")
    assert conflicts[0]["active"]["id"] == "be35"


def test_ai_lesson_superseded_by_trader_lesson_same_topic():
    lessons = [
        L("AUTO-LEVERAGE-EMPFEHLUNG DER KI", locked=False,
          updated_at="2026-06-05T00:00:00", lid="ai1"),
        L("HEBEL-VORGABE DES TRADERS", locked=True,
          updated_at="2026-05-01T00:00:00", lid="tr1"),
    ]
    out, _ = consolidate_conflicts(lessons)
    by_id = {l["id"]: l for l in out}
    # Trader-Anweisung schlägt KI-Lektion, auch wenn die KI-Lektion neuer ist
    assert not by_id["tr1"].get("superseded")
    assert by_id["ai1"].get("superseded") and by_id["ai1"]["superseded_by"] == "tr1"


def test_unrelated_lessons_untouched():
    lessons = [
        L("REGIME-4-FOKUS", lid="r4"),
        L("VOLUMEN-VALIDIERUNG FÜR EINSTIEGE", lid="vol"),
        L("MONTAG/FREITAG-VOLUMENREDUKTION", lid="mofr"),
    ]
    out, conflicts = consolidate_conflicts(lessons)
    assert conflicts == []
    assert all(not l.get("superseded") for l in out)


def test_ai_only_topic_not_consolidated():
    """Ohne Trader-Anweisung (locked) regelt weiterhin dedupe_lessons –
    keine Themen-Konsolidierung unter reinen KI-Lektionen."""
    lessons = [
        L("COOLDOWN NACH TRADE", locked=False, updated_at="2026-01-01", lid="c1"),
        L("COOLDOWN-DISZIPLIN BEI VERLUSTSERIEN", locked=False,
          updated_at="2026-02-01", lid="c2"),
    ]
    _, conflicts = consolidate_conflicts(lessons)
    assert conflicts == []


def test_winner_in_one_topic_never_superseded_by_another():
    lessons = [
        L("AUTO-LEVERAGE UND BREAK-EVEN KOMBI-REGEL",
          updated_at="2026-06-10T00:00:00", lid="combo"),
        L("STRIKTE NUTZUNG DES AUTO-LEVERAGE", updated_at="2026-05-01T00:00:00", lid="lev"),
        L("BREAK-EVEN-UMSETZUNG", updated_at="2026-05-02T00:00:00", lid="be"),
    ]
    out, _ = consolidate_conflicts(lessons)
    by_id = {l["id"]: l for l in out}
    assert not by_id["combo"].get("superseded")
    assert by_id["lev"].get("superseded") and by_id["be"].get("superseded")


def test_lessons_text_excludes_superseded():
    lessons = [
        L("AUTO-LEVERAGE ALT", updated_at="2026-01-01T00:00:00", lid="old"),
        L("HEBEL 10 STRIKT (NEU)", updated_at="2026-06-01T00:00:00", lid="new"),
    ]
    txt = lessons_text(lessons)
    assert "HEBEL 10 STRIKT (NEU)" in txt
    assert "AUTO-LEVERAGE ALT" not in txt


def test_active_lessons_keeps_flags_out_of_prompt_but_data_stored():
    lessons = [
        L("AUTO-LEVERAGE ALT", updated_at="2026-01-01T00:00:00", lid="old"),
        L("HEBEL 10 STRIKT (NEU)", updated_at="2026-06-01T00:00:00", lid="new"),
    ]
    consolidated, _ = consolidate_conflicts(lessons)
    assert len(consolidated) == 2            # nichts wird gelöscht
    assert len(active_lessons(lessons)) == 1  # aber nur eine ist aktiv


def test_merge_lessons_preserves_superseded_flags():
    consolidated, _ = consolidate_conflicts([
        L("AUTO-LEVERAGE ALT", updated_at="2026-01-01T00:00:00", lid="old"),
        L("HEBEL 10 STRIKT (NEU)", updated_at="2026-06-01T00:00:00", lid="new"),
    ])
    merged = ai_lessons.merge_lessons(consolidated, [], [], 50)
    by_title = {m["title"]: m for m in merged}
    assert by_title["AUTO-LEVERAGE ALT"].get("superseded") is True


# --------------------------------------------------------------------------- #
#  3) Smart-Skip (Kosten: LLM-Lauf nur wenn nötig)
# --------------------------------------------------------------------------- #
def _skip_setup(action="HOLD", price_then=100.0, price_now=100.05,
                ts_offset_min=5):
    from datetime import datetime, timedelta, timezone
    eng = _engine()
    ts = (datetime.now(timezone.utc) - timedelta(minutes=ts_offset_min)).isoformat()
    eng.decisions = {"BTCUSDT": {"action": action, "price": price_then, "ts": ts}}
    snaps = {"BTCUSDT": {"price": price_now}}
    return eng, snaps


def test_smart_skip_when_market_unchanged():
    eng, snaps = _skip_setup()
    assert eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=False)


def test_no_skip_on_manual_run():
    eng, snaps = _skip_setup()
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=True)


def test_no_skip_when_disabled():
    eng, snaps = _skip_setup()
    eng.config["smart_skip"] = False
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=False)


def test_no_skip_with_open_position_in_group():
    eng, snaps = _skip_setup()
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps,
                                      {"BTCUSDT"}, manual=False)


def test_no_skip_when_open_positions_unknown():
    eng, snaps = _skip_setup()
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, None, manual=False)


def test_no_skip_on_significant_price_move():
    eng, snaps = _skip_setup(price_then=100.0, price_now=101.0)  # 1% >> 0.15%
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=False)


def test_no_skip_when_last_decision_wanted_to_trade():
    eng, snaps = _skip_setup(action="LONG")
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=False)


def test_no_skip_without_fresh_decision():
    eng, snaps = _skip_setup(ts_offset_min=600)  # uralt -> nicht mehr frisch
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=False)


def test_max_two_consecutive_skips_then_forced_run():
    eng, snaps = _skip_setup()
    eng._group_skips["Krypto"] = 2
    assert not eng._should_skip_group("Krypto", ["BTCUSDT"], snaps, set(), manual=False)


def test_skip_requires_all_symbols_calm():
    eng, snaps = _skip_setup()
    snaps["ETHUSDT"] = {"price": 2000.0}  # keine Vor-Entscheidung für ETH
    assert not eng._should_skip_group("Krypto", ["BTCUSDT", "ETHUSDT"], snaps,
                                      set(), manual=False)
