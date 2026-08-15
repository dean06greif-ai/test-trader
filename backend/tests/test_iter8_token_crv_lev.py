"""Regressionstests Iteration 8 – Token-Sparmaßnahmen, Trade-Rahmen (CRV/Hebel),
Master-Schalter & Fallback-Anzeige.

User-Wünsche dieser Iteration:
  1. Token-Verbrauch pro Prompt senken ohne Qualitätsverlust:
     - kompakter Analyse-Systemprompt (ANALYSIS_SYSTEM_LEAN) bei lean_prompt=AN
     - Liquiditäts-Block nur noch für die Krypto-Gruppe
     - Trade-Manager-Review nutzt schlanken Kontext (purpose="trade_review")
     - Markt-Beobachter verwendet alte LLM-Einschätzung wieder, wenn sich der
       Markt kaum bewegt hat ("alten Text kopieren statt KI neu arbeiten lassen")
  2. Trade-Rahmen im AI-Panel: CRV min/max pro Trade + Hebel-Modus
     (Coin-Settings | Auto bis Max | Fest) – technisch erzwungen.
  3. Master-Schalter "Stop All Trades" verhindert NUR neue Trades und
     schließt KEINE offenen Positionen mehr.
  4. Aktive Fallbacks pro Assistent im Modell-Status sichtbar.
"""
import asyncio
import inspect
import time

from services import ai_providers
from services.ai_engine import (AIEngine, ANALYSIS_SYSTEM, ANALYSIS_SYSTEM_LEAN,
                                DEFAULT_AI_CONFIG)


class _FakeSettings:
    async def update_one(self, *a, **k):
        return None


class _FakeDB:
    settings = _FakeSettings()


def _engine() -> AIEngine:
    eng = AIEngine()
    eng.db = _FakeDB()
    return eng


# --------------------------------------------------------------------------- #
#  1) Trade-Rahmen: Defaults & Validierung
# --------------------------------------------------------------------------- #
def test_default_trade_frame_backwards_compatible():
    # Defaults ändern das bisherige Verhalten NICHT: crv_max 0 = keine
    # Obergrenze, lev_mode "coin" = Coin-Trade-Settings entscheiden.
    assert DEFAULT_AI_CONFIG["crv_min"] == 1.2
    assert DEFAULT_AI_CONFIG["crv_max"] == 0
    assert DEFAULT_AI_CONFIG["lev_mode"] == "coin"
    assert DEFAULT_AI_CONFIG["lev_auto_max"] == 25
    assert DEFAULT_AI_CONFIG["lev_fixed"] == 10


def test_update_config_accepts_and_clamps_trade_frame():
    eng = _engine()
    cfg = asyncio.run(eng.update_config({
        "crv_min": 1.5, "crv_max": 3.5, "lev_mode": "auto",
        "lev_auto_max": 30, "lev_fixed": 12}))
    assert cfg["crv_min"] == 1.5 and cfg["crv_max"] == 3.5
    assert cfg["lev_mode"] == "auto" and cfg["lev_auto_max"] == 30
    assert cfg["lev_fixed"] == 12
    # Clamps
    cfg = asyncio.run(eng.update_config({
        "crv_min": 0.2, "lev_auto_max": 999, "lev_fixed": 0}))
    assert cfg["crv_min"] == 1.0
    assert cfg["lev_auto_max"] == 100
    assert cfg["lev_fixed"] == 1
    # crv_max unter crv_min wird auf crv_min gehoben; 0 = aus
    asyncio.run(eng.update_config({"crv_min": 2.0}))
    cfg = asyncio.run(eng.update_config({"crv_max": 1.2}))
    assert cfg["crv_max"] == 2.0
    cfg = asyncio.run(eng.update_config({"crv_max": 0}))
    assert cfg["crv_max"] == 0
    # Ungültiger Modus wird ignoriert
    cfg = asyncio.run(eng.update_config({"lev_mode": "yolo"}))
    assert cfg["lev_mode"] == "auto"


# --------------------------------------------------------------------------- #
#  2) Trade-Rahmen: CRV-Klemmung & Hebel-Modus (technisch erzwungen)
# --------------------------------------------------------------------------- #
def test_crv_frame_raises_tp1_to_min():
    eng = _engine()
    eng.config["crv_min"], eng.config["crv_max"] = 2.0, 0
    tp1, tpf = eng._apply_crv_frame(0.01, 0.012, 0.02, is_swing=False)
    assert abs(tp1 - 0.02) < 1e-9          # 1% SL * CRV 2.0
    assert tpf >= tp1


def test_crv_frame_caps_tp1_at_max():
    eng = _engine()
    eng.config["crv_min"], eng.config["crv_max"] = 1.2, 2.0
    tp1, tpf = eng._apply_crv_frame(0.01, 0.05, 0.06, is_swing=False)
    assert abs(tp1 - 0.02) < 1e-9          # gedeckelt auf SL * 2.0
    assert tpf >= tp1


def test_crv_frame_no_cap_when_max_zero():
    eng = _engine()
    eng.config["crv_min"], eng.config["crv_max"] = 1.2, 0
    tp1, _ = eng._apply_crv_frame(0.01, 0.05, 0.06, is_swing=False)
    assert abs(tp1 - 0.05) < 1e-9          # unverändert (keine Obergrenze)


def test_lev_mode_coin_keeps_previous_behavior():
    eng = _engine()
    eng.config["lev_mode"] = "coin"
    assert eng._frame_leverage({"leverage": 15}, is_swing=False) == 0.0
    # Swing behält den bisherigen Deckel
    assert eng._frame_leverage({}, is_swing=True) == eng.config["swing_max_leverage"]


def test_lev_mode_fixed_forces_leverage():
    eng = _engine()
    eng.config["lev_mode"], eng.config["lev_fixed"] = "fixed", 12
    assert eng._frame_leverage({"leverage": 50}, is_swing=False) == 12.0


def test_lev_mode_auto_caps_ai_choice():
    eng = _engine()
    eng.config["lev_mode"], eng.config["lev_auto_max"] = "auto", 20
    assert eng._frame_leverage({"leverage": 15}, is_swing=False) == 15.0
    assert eng._frame_leverage({"leverage": 80}, is_swing=False) == 20.0
    # KI gibt keinen Hebel an -> Coin-Settings entscheiden (kein Zwang)
    assert eng._frame_leverage({}, is_swing=False) == 0.0


def test_lev_auto_swing_still_capped():
    eng = _engine()
    eng.config.update({"lev_mode": "auto", "lev_auto_max": 50,
                       "swing_max_leverage": 8})
    assert eng._frame_leverage({"leverage": 40}, is_swing=True) == 8.0


def test_safe_lev_parsing():
    assert AIEngine._safe_lev("12") == 12
    assert AIEngine._safe_lev(None) == 0
    assert AIEngine._safe_lev("abc") == 0
    assert AIEngine._safe_lev(9999) == 200


def test_trade_frame_block_in_analysis_prompt():
    src = inspect.getsource(AIEngine.run_analysis)
    assert "TRADE-RAHMEN" in src
    assert "crv_min" in src and "lev_mode" in src


# --------------------------------------------------------------------------- #
#  3) Token-Sparmaßnahmen
# --------------------------------------------------------------------------- #
def test_lean_system_prompt_same_schema_but_shorter():
    assert len(ANALYSIS_SYSTEM_LEAN) < len(ANALYSIS_SYSTEM) * 0.75
    for key in ('"market_overview"', '"decisions"', '"confidence"', '"horizon"',
                '"sl_pct"', '"tp1_pct"', '"tpf_pct"', '"capital_pct"',
                '"new_strategies"', '"config_changes"',
                '"strategy_candidate_id"'):
        assert key in ANALYSIS_SYSTEM_LEAN, f"Schema-Feld {key} fehlt im Lean-Prompt"


def test_lean_prompt_selects_lean_system():
    src = inspect.getsource(AIEngine.run_analysis)
    assert "ANALYSIS_SYSTEM_LEAN" in src and 'lean_prompt' in src


def test_hold_reason_instruction_in_both_prompts():
    # Analyst soll bei HOLD den Grund nennen (UI zeigt "kein Edge"-Kennzeichen)
    for sysp in (ANALYSIS_SYSTEM, ANALYSIS_SYSTEM_LEAN):
        assert "Marktphase" in sysp
        assert "Handelsfenster" in sysp


def test_liquidity_block_only_for_crypto_group():
    src = inspect.getsource(AIEngine.run_analysis)
    assert 'purpose="analysis_base"' in src
    assert '"Krypto"' in src and "g_liq" in src


def test_trade_manager_uses_lean_review_context():
    from services import ai_trade_manager
    src = inspect.getsource(ai_trade_manager.AITradeManager.review)
    assert 'purpose="trade_review"' in src


def test_extra_blocks_review_purpose_documented():
    src = inspect.getsource(AIEngine._analysis_extra_blocks)
    assert "trade_review" in src and "analysis_base" in src


def test_market_observer_reuses_summary_when_unchanged():
    from services.ai_market_observer import MarketObserver
    mo = MarketObserver()
    mo.last_summary = {"summary": "alter Text", "regime": "range"}
    mo._summary_prices = {"BTCUSDT": 100000.0}
    mo._summary_ts = time.time()
    snaps = [{"symbol": "BTCUSDT", "features": {"price": 100010.0}}]
    # engine=None: würde der LLM-Pfad laufen, gäbe es None statt "alter Text"
    out = asyncio.run(mo._llm_summary(snaps))
    assert out == "alter Text"


def test_market_observer_regenerates_on_move():
    from services.ai_market_observer import MarketObserver
    mo = MarketObserver()
    mo.last_summary = {"summary": "alter Text"}
    mo._summary_prices = {"BTCUSDT": 100000.0}
    mo._summary_ts = time.time()
    snaps = [{"symbol": "BTCUSDT", "features": {"price": 101000.0}}]  # +1%
    out = asyncio.run(mo._llm_summary(snaps))
    assert out != "alter Text"     # LLM-Pfad (engine=None -> None)


def test_learner_skips_daily_run_without_new_results():
    # Lern-Assistent (Rolle "learner", ~125k Tokens/Tag): geplante Läufe
    # (daily/daily_summary) ohne neue geschlossene Trades sparen den LLM-Call.
    from services.ai_learning import AILearning

    class _Trades:
        async def count_documents(self, q):
            return 0

    class _Db:
        auto_trades = _Trades()

    class _Eng:
        key = "x"
        config = dict(DEFAULT_AI_CONFIG)
        db = _Db()

    learn = AILearning(_Eng())
    learn._last_learn_ts = time.time() - 3600
    res = asyncio.run(learn.run_learning(trigger="daily"))
    assert res["status"] == "skipped"
    # Manuelle Läufe und Trade-Close-Läufe werden NICHT übersprungen
    src = inspect.getsource(AILearning.run_learning)
    assert '("daily", "daily_summary")' in src


def test_learner_prompt_lean_gates_research_and_ml():
    from services.ai_learning import AILearning
    src = inspect.getsource(AILearning.run_learning)
    assert "if not lean:" in src           # Forschungs-/ML-Blöcke nur ohne Lean
    assert "15 if lean else 25" in src     # kürzere Trade-Historie bei Lean


# --------------------------------------------------------------------------- #
#  4) Master-Schalter: nur neue Trades verhindern, nichts mehr schließen
# --------------------------------------------------------------------------- #
def test_stop_trades_no_longer_closes_positions():
    from routers import control
    assert not hasattr(control, "_close_all_open_auto_trades")
    src = inspect.getsource(control.toggle_stop_trades)
    assert "manual_close" not in src
    assert "closed_trades" not in src


# --------------------------------------------------------------------------- #
#  5) Aktive Fallbacks pro Assistent (Warnfenster im AI-Panel)
# --------------------------------------------------------------------------- #
def test_active_fallback_tracked_and_cleared():
    ai_providers._role_fallbacks.clear()
    ai_providers.record_result("groq", "llama-3.1-8b-instant", "ok",
                               role="analyst", requested="openai/gpt-oss-120b")
    hs = ai_providers.health_status()
    fbs = hs.get("active_fallbacks") or []
    assert any(f["role"] == "analyst"
               and f["model"] == "llama-3.1-8b-instant"
               and f["requested_model"] == "openai/gpt-oss-120b" for f in fbs)
    # Erfolg mit Wunsch-Modell -> Fallback-Eintrag der Rolle verschwindet
    ai_providers.record_result("groq", "openai/gpt-oss-120b", "ok",
                               role="analyst", requested="openai/gpt-oss-120b")
    hs = ai_providers.health_status()
    assert not any(f["role"] == "analyst"
                   for f in (hs.get("active_fallbacks") or []))


def test_backup_key_counts_as_fallback():
    ai_providers._role_fallbacks.clear()
    ai_providers.record_result("groq", "openai/gpt-oss-120b", "ok",
                               role="news_watcher", requested="openai/gpt-oss-120b",
                               key_index=1)
    hs = ai_providers.health_status()
    fbs = hs.get("active_fallbacks") or []
    assert any(f["role"] == "news_watcher" and f["key_index"] == 1 for f in fbs)
    ai_providers._role_fallbacks.clear()
