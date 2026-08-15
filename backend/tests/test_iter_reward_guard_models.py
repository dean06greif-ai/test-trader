"""Regressionstests für die Verbesserungs-Iteration (Juni 2026):

  * Modell-Katalog: keine toten Slugs, Presets/Fallbacks/Gewichte konsistent
  * Modell-Migration: tote Slugs (DeepSeek R1 free, Qwen3-235B free, GitHub
    Models nach Retirement, …) wandern auf verifizierte Nachfolger
  * Token-Budget: zu große Prompts überspringen Modelle statt 413 zu erzeugen
  * Belohnungssystem: deterministischer Reward-Score pro Trade
  * Kill-Switch-Zwangs-Lernphase: Sperr-/Freigabe-Logik
"""
import asyncio

import pytest

from services import ai_providers, ai_rewards, trade_guard
from services.ai_roles import DEFAULT_ROLES_CONFIG, ROLE_PRESETS


# ---------------- Modell-Katalog ----------------
DEAD_SLUGS = {
    "qwen/qwen3-32b", "deepseek/deepseek-r1:free", "qwen/qwen3-235b-a22b:free",
    "open-mistral-nemo", "llama-3.3-70b", "qwen-3-32b",
    "openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4o-mini",
}


def test_catalog_contains_no_dead_slugs():
    for prov, models in ai_providers.ALLOWED_MODELS.items():
        assert not DEAD_SLUGS.intersection(models), f"tote Slugs in {prov}"
    assert "github" not in ai_providers.ALLOWED_MODELS  # GitHub Models eingestellt (410)


def test_fallback_order_and_weights_consistent():
    for prov, order in ai_providers.FALLBACK_ORDER.items():
        allowed = set(ai_providers.ALLOWED_MODELS[prov])
        # Bezahlte Modelle stehen bewusst NICHT in der Fallback-Kette
        expected = allowed - ai_providers.PAID_MODELS_NO_FALLBACK
        assert set(order) == expected, f"FALLBACK_ORDER != ALLOWED_MODELS für {prov}"
    for models in ai_providers.ALLOWED_MODELS.values():
        for m in models:
            assert m in ai_providers.MODEL_WEIGHTS, f"Gewicht fehlt für {m}"


def test_role_presets_reference_valid_models():
    for role, preset in ROLE_PRESETS.items():
        for pk, mk in (("provider", "model"), ("fallback_provider", "fallback_model"),
                       ("fallback2_provider", "fallback2_model")):
            prov, mod = preset.get(pk), preset.get(mk)
            if mod:
                assert mod in ai_providers.ALLOWED_MODELS.get(prov, []), \
                    f"{role}: {prov}/{mod} nicht im Katalog"
    assert set(DEFAULT_ROLES_CONFIG) == set(ROLE_PRESETS)


def test_migrations_map_to_valid_models():
    for old, (prov, new) in ai_providers.MODEL_MIGRATIONS.items():
        assert new in ai_providers.ALLOWED_MODELS.get(prov, []), \
            f"Migration {old} -> {prov}/{new} ungültig"
    assert ai_providers.migrate_model("groq", "qwen/qwen3-32b") == ("groq", "qwen/qwen3.6-27b")
    assert ai_providers.migrate_model("github", "openai/gpt-4.1")[0] in ai_providers.ALLOWED_MODELS
    # gültiges Modell bleibt unverändert (Provider wird ggf. korrigiert)
    assert ai_providers.migrate_model("gemini", "gemini-3.5-flash") == ("gemini", "gemini-3.5-flash")


# ---------------- Token-Budget / 413-Skip ----------------
def test_too_large_error_detection():
    assert ai_providers.is_too_large_error(Exception(
        "Error code: 413 - {'error': {'message': 'Request too large for model "
        "`llama-3.1-8b-instant` on tokens per minute (TPM)'}}"))
    assert not ai_providers.is_too_large_error(Exception("429 rate limit"))


def test_learned_budget_shrinks_only():
    ai_providers._learned_budget.pop("groq/test-model", None)
    ai_providers.learn_budget("groq", "test-model", 10000)
    assert ai_providers.input_budget("groq", "test-model") == 8500
    ai_providers.learn_budget("groq", "test-model", 20000)   # größerer Wert: kein Anheben
    assert ai_providers.input_budget("groq", "test-model") == 8500
    ai_providers._learned_budget.pop("groq/test-model", None)


def test_generate_chain_skips_over_budget_model(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    called = []

    async def fake_oai(provider, model, key, prompt, system, temperature, json_mode):
        called.append(f"{provider}/{model}")
        return "ok"

    monkeypatch.setattr(ai_providers, "_oai_generate", fake_oai)
    big_prompt = "x" * (6000 * 3)  # ~6000 Tokens > Budget 5000 des 8B-Modells
    chain = [("groq", "llama-3.1-8b-instant"), ("groq", "llama-3.3-70b-versatile")]
    text, prov, model = asyncio.run(
        ai_providers.generate_chain(chain, big_prompt, "sys"))
    assert text == "ok" and model == "llama-3.3-70b-versatile"
    assert "groq/llama-3.1-8b-instant" not in called  # übersprungen, nicht aufgerufen
    health = ai_providers.health_status()
    skipped = {f"{m['provider']}/{m['model']}" for m in health["skipped_too_large"]}
    assert "groq/llama-3.1-8b-instant" in skipped


def test_oai_generate_guards_empty_choices(monkeypatch):
    class FakeResp:
        choices = None

    class FakeCompletions:
        async def create(self, **kw):
            return FakeResp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(ai_providers, "_oai_client", lambda p, k: FakeClient())
    with pytest.raises(RuntimeError, match="Leere Antwort"):
        asyncio.run(ai_providers._oai_generate(
            "openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
            "key", "p", "s", 0.4, False))


# ---------------- Belohnungssystem ----------------
def _trade(pnl, result, minutes=60, cap=100.0):
    return {"realized_pnl": pnl, "max_capital": cap, "result": result,
            "opened_at": "2026-06-01T10:00:00+00:00",
            "closed_at": f"2026-06-01T{10 + minutes // 60:02d}:{minutes % 60:02d}:00+00:00"}


def test_reward_win_beats_loss():
    win = ai_rewards.compute_reward(_trade(5.0, "win"), {"confidence": 85})
    loss = ai_rewards.compute_reward(_trade(-5.0, "loss"), {"confidence": 70})
    assert win["score"] > 0 > loss["score"]


def test_reward_quick_stopout_and_low_confidence_penalized():
    slow = ai_rewards.compute_reward(_trade(-3.0, "loss", minutes=90), {"confidence": 85})
    fast = ai_rewards.compute_reward(_trade(-3.0, "loss", minutes=5), {"confidence": 70})
    assert fast["score"] < slow["score"]
    labels = [c["label"] for c in fast["components"]]
    assert any("Sofort-Stop-Out" in l for l in labels)
    assert any("Konfidenz" in l for l in labels)


def test_reward_crv_bonus():
    d = {"confidence": 85, "sl_pct": 0.5, "tp1_pct": 1.2}
    r = ai_rewards.compute_reward(_trade(4.0, "win"), d)
    assert any("CRV-Disziplin" in c["label"] for c in r["components"])


def test_reward_pnl_base_clamped():
    r = ai_rewards.compute_reward(_trade(1000.0, "win", cap=100.0), None)
    base = [c for c in r["components"] if c["label"] == "PnL-Basis"][0]
    assert base["value"] == 4.0  # Deckel gegen Ausreißer


# ---------------- Kill-Switch Zwangs-Lernphase ----------------
class MiniCollection:
    def __init__(self):
        self.docs = {}

    async def find_one(self, q):
        return self.docs.get(q.get("_id"))

    async def update_one(self, q, u, upsert=False):
        doc = self.docs.setdefault(q["_id"], {"_id": q["_id"]})
        doc.update(u.get("$set", {}))


class MiniDB:
    def __init__(self):
        self.settings = MiniCollection()


def test_guard_state_reports_learning_required():
    db = MiniDB()

    async def run():
        await db.settings.update_one(
            {"_id": trade_guard.STATE_ID},
            {"$set": {"paused_until": None, "learning_required": True}}, upsert=True)
        return await trade_guard.get_state(db)

    st = asyncio.run(run())
    assert st["learning_required"] is True and st["paused"] is False


def test_resume_clears_learning_required():
    db = MiniDB()

    async def run():
        await db.settings.update_one(
            {"_id": trade_guard.STATE_ID},
            {"$set": {"learning_required": True}}, upsert=True)
        return await trade_guard.resume(db)

    st = asyncio.run(run())
    assert st["learning_required"] is False


def test_forced_learning_default_enabled():
    assert trade_guard.DEFAULT_CONFIG["forced_learning_enabled"] is True
