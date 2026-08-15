"""Regressionstests: KI-Team (Rollen), Provider-Schicht, Backup-Keys, Gewichte.

Deckt die neuen Bausteine ab, ohne echte LLM-Calls:
  - ai_providers: Kataloge, Key-Reihenfolge (primär -> backup), Gewichte
  - ai_roles: Ketten-Bildung, Handelszeiten, Fallback-KI, Sanitize
  - ai_engine: Rückwärtskompatible Aliase & neue Methoden vorhanden
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import ai_providers
from services.ai_roles import AIRoleManager, in_active_hours, DEFAULT_ROLES_CONFIG

BERLIN = ZoneInfo("Europe/Berlin")


# ---------------- ai_providers ----------------
def test_new_providers_in_catalog():
    for prov in ("gemini", "groq", "openrouter", "mistral", "github", "cerebras"):
        assert prov in ai_providers.ALLOWED_MODELS
        assert ai_providers.ALLOWED_MODELS[prov], f"{prov} hat keine Modelle"
        assert prov in ai_providers.FALLBACK_ORDER


def test_model_weights_complete():
    for prov, models in ai_providers.ALLOWED_MODELS.items():
        for m in models:
            assert m in ai_providers.MODEL_WEIGHTS, f"Gewicht fehlt für {m}"
            assert ai_providers.MODEL_WEIGHTS[m] in (1, 2, 3)


def test_provider_for_model():
    assert ai_providers.provider_for_model("gemini-3.5-flash") == "gemini"
    assert ai_providers.provider_for_model("openai/gpt-4.1") == "github"
    assert ai_providers.provider_for_model("gpt-oss-120b") == "cerebras"
    assert ai_providers.provider_for_model("unbekannt") is None


def test_openrouter_backup_key_order(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "primary-key")
    monkeypatch.setenv("OPENROUTER_API_KEY_BACKUP", "backup-key")
    keys = ai_providers.provider_keys("openrouter")
    assert keys == ["primary-key", "backup-key"]
    assert ai_providers.primary_key("openrouter") == "primary-key"


def test_backup_key_only_when_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "primary-key")
    monkeypatch.delenv("OPENROUTER_API_KEY_BACKUP", raising=False)
    assert ai_providers.provider_keys("openrouter") == ["primary-key"]
    info = ai_providers.backup_keys_info()
    assert info["openrouter"] is False
    monkeypatch.setenv("OPENROUTER_API_KEY_BACKUP", "backup-key")
    assert ai_providers.backup_keys_info()["openrouter"] is True


def test_is_rate_limit_error():
    assert ai_providers.is_rate_limit_error(RuntimeError("429 Too Many Requests"))
    assert ai_providers.is_rate_limit_error(RuntimeError("RESOURCE_EXHAUSTED quota"))
    assert not ai_providers.is_rate_limit_error(RuntimeError("connection refused"))


def test_same_provider_chain_preferred_first():
    chain = ai_providers.same_provider_chain("gemini", "gemini-3.5-flash")
    assert chain[0] == ("gemini", "gemini-3.5-flash")
    assert len(chain) == len(ai_providers.ALLOWED_MODELS["gemini"])
    # unbekanntes Modell -> erstes erlaubtes Modell
    chain2 = ai_providers.same_provider_chain("groq", "nicht-existent")
    assert chain2[0][1] == ai_providers.ALLOWED_MODELS["groq"][0]


def test_generate_chain_no_keys_raises(monkeypatch):
    import asyncio
    for env in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY_BACKUP"):
        monkeypatch.delenv(env, raising=False)
    async def _run():
        with pytest.raises(RuntimeError, match="Kein API-Key"):
            await ai_providers.generate_chain([("gemini", "gemini-3.5-flash")], "p", "s")
    asyncio.run(_run())


# ---------------- active hours ----------------
def _dt(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 6, 15, int(h), int(m), tzinfo=BERLIN)


def test_in_active_hours_normal_window():
    ah = {"start": "08:00", "end": "22:00"}
    assert in_active_hours(ah, _dt("12:00"))
    assert in_active_hours(ah, _dt("08:00"))
    assert not in_active_hours(ah, _dt("22:00"))
    assert not in_active_hours(ah, _dt("03:00"))


def test_in_active_hours_over_midnight():
    ah = {"start": "22:00", "end": "06:00"}
    assert in_active_hours(ah, _dt("23:30"))
    assert in_active_hours(ah, _dt("03:00"))
    assert not in_active_hours(ah, _dt("12:00"))


def test_in_active_hours_none_or_invalid():
    assert in_active_hours(None, _dt("12:00"))
    assert in_active_hours({"start": "kaputt", "end": "22:00"}, _dt("03:00"))


# ---------------- role manager ----------------
ENGINE_CFG = {"provider": "gemini", "model": "gemini-3.5-flash"}


def test_role_chain_inherits_engine_default():
    rm = AIRoleManager()
    chain = rm.chain("analyst", ENGINE_CFG)
    assert chain[0] == ("gemini", "gemini-3.5-flash")
    # Primär-Kette (gemini) steht vorn; danach dürfen Provider mit gesetztem
    # API-Key als letzte Rettung folgen (gewolltes Verhalten von chain()).
    non_gemini_seen = False
    for p, _ in chain:
        if p != "gemini":
            non_gemini_seen = True
        else:
            assert not non_gemini_seen, "gemini-Eintrag nach fremdem Provider"


def test_role_chain_custom_model_and_fallback():
    rm = AIRoleManager()
    rm.config["deep_analyst"].update({
        "provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "fallback_provider": "groq", "fallback_model": "llama-3.3-70b-versatile",
    })
    chain = rm.chain("deep_analyst", ENGINE_CFG)
    assert chain[0] == ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")
    assert ("groq", "llama-3.3-70b-versatile") in chain
    # Fallback kommt NACH allen OpenRouter-Modellen
    or_idx = max(i for i, (p, _) in enumerate(chain) if p == "openrouter")
    gq_idx = min(i for i, (p, _) in enumerate(chain) if p == "groq")
    assert gq_idx > or_idx


def test_role_chain_outside_hours_uses_fallback_first():
    rm = AIRoleManager()
    rm.config["analyst"].update({
        "provider": "gemini", "model": "gemini-3.1-pro-preview",
        "active_hours": {"start": "08:00", "end": "22:00"},
        "fallback_provider": "groq", "fallback_model": "llama-3.1-8b-instant",
    })
    inside = rm.chain("analyst", ENGINE_CFG, now=_dt("12:00"))
    outside = rm.chain("analyst", ENGINE_CFG, now=_dt("03:00"))
    assert inside[0] == ("gemini", "gemini-3.1-pro-preview")
    assert outside[0] == ("groq", "llama-3.1-8b-instant")
    # Primär-Modell bleibt als spätere Stufe erhalten (Fallback der Fallback-KI)
    assert ("gemini", "gemini-3.1-pro-preview") in outside


def test_role_sanitize_rejects_invalid():
    rm = AIRoleManager()
    out = rm._sanitize("analyst", {
        "model": "nicht-existent",
        "active_hours": {"start": "25:99", "end": "22:00"},
        "fallback_model": "auch-nicht-existent",
    })
    assert "model" not in out
    assert out.get("active_hours") is None
    assert out.get("fallback_model") is None


def test_role_sanitize_deep_times_and_news_interval():
    rm = AIRoleManager()
    out = rm._sanitize("deep_analyst", {"schedule_times": ["08:00", "kaputt", "20:00", "20:00"]})
    assert out["schedule_times"] == ["08:00", "20:00"]
    out2 = rm._sanitize("news_watcher", {"interval_min": 999, "auto_analysis": 0})
    assert out2["interval_min"] == 120
    assert out2["auto_analysis"] is False


def test_default_roles_complete():
    for role in ("analyst", "deep_analyst", "news_watcher", "chat", "learner", "summarizer"):
        assert role in DEFAULT_ROLES_CONFIG


# ---------------- ai_engine backward compat ----------------
def test_engine_backward_compat_aliases():
    from services import ai_engine as eng
    assert eng.ALLOWED_MODELS is ai_providers.ALLOWED_MODELS
    assert eng.FALLBACK_ORDER is ai_providers.FALLBACK_ORDER
    assert eng.OPENAI_COMPAT_PROVIDERS is ai_providers.OPENAI_COMPAT_PROVIDERS
    e = eng.ai_engine
    assert hasattr(e, "run_deep_analysis")
    assert hasattr(e, "generate_for_role")
    assert hasattr(e, "_strategy_performance_text")
    assert hasattr(e, "_check_deep_schedule")


def test_news_watcher_module():
    from services.ai_news_watcher import news_watcher
    assert hasattr(news_watcher, "run_loop")
    assert hasattr(news_watcher, "run_check")
    st = news_watcher.status()
    assert "last_run" in st and "running" in st
