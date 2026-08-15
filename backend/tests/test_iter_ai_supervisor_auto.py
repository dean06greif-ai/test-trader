"""Regressionstests für Iteration 4 (Folgeausbau):

  * Supervisor-Einstellungen (automatische Prüfung, Intervall, Auto-Umschaltung)
  * Fälligkeit des Selbstlaufs (`_due`)
  * automatische Umschaltung schwacher Rollen auf die Fallback-KI + Rollback
  * Übernahme der KI-Verbesserungen in eine Strategie (`apply_assist`)
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ai_roles import role_manager, DEFAULT_ROLES_CONFIG
from services.ai_strategy_lab import StrategyLab
from services.ai_supervisor import TeamSupervisor
from tests.test_iter_ai_supervisor_autonomy import FakeCollection, FakeDB


class LabFakeDB(FakeDB):
    """FakeDB, die jede Collection bei Bedarf anlegt (Attribut- und db[..]-Zugriff)."""

    def __init__(self):
        super().__init__()
        self.ai_strategy_candidates = FakeCollection()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        coll = FakeCollection()
        setattr(self, name, coll)
        return coll

    def __getitem__(self, name):
        return getattr(self, name)


class FakeEngine:
    def __init__(self, db):
        self.db = db
        self.key = "test-key"
        self.config = {"provider": "gemini", "model": "gemini-3.5-flash"}


def _sup():
    db = LabFakeDB()
    s = TeamSupervisor()
    s.setup(FakeEngine(db))
    return s


# ---------------- Einstellungen ----------------
def test_settings_are_sanitized_and_persisted():
    s = _sup()
    cfg = asyncio.run(s.update_settings({"auto_enabled": True, "interval_hours": 999,
                                         "auto_switch": "ja", "quatsch": 1}))
    assert cfg == {"auto_enabled": True, "interval_hours": 168, "auto_switch": True}
    assert "quatsch" not in cfg
    assert asyncio.run(s.update_settings({"interval_hours": 1}))["interval_hours"] == 6
    doc = asyncio.run(s.db.settings.find_one({"_id": "ai_supervisor_settings"}))
    assert doc["auto_enabled"] is True


def test_due_only_when_enabled_and_old_enough():
    s = _sup()
    assert s._due() is False                     # standardmässig aus
    s.settings["auto_enabled"] = True
    assert s._due() is True                      # noch kein Bericht
    s.report = {"ts": datetime.now(timezone.utc).isoformat()}
    assert s._due() is False
    s.report = {"ts": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()}
    assert s._due() is True
    s.settings["interval_hours"] = 48
    assert s._due() is False


# ---------------- automatische Umschaltung ----------------
def _reset_roles():
    role_manager.config = {r: dict(c) for r, c in DEFAULT_ROLES_CONFIG.items()}


def test_auto_switch_disabled_by_default():
    _reset_roles()
    s = _sup()
    roles = [{"role": "analyst", "verdict": "schwach", "action": "modell_wechseln",
              "reason": "Fehler", "suggested_provider": "groq",
              "suggested_model": "llama-3.3-70b-versatile"}]
    assert asyncio.run(s._auto_switch(roles)) == []


def test_auto_switch_uses_role_fallback_and_rollback_restores():
    _reset_roles()
    s = _sup()
    s.settings["auto_switch"] = True
    role_manager.config["analyst"].update({
        "provider": "gemini", "model": "gemini-3.5-flash",
        "fallback_provider": "groq", "fallback_model": "llama-3.3-70b-versatile"})
    roles = [{"role": "analyst", "verdict": "schwach", "reason": "503-Fehler",
              "suggested_provider": None, "suggested_model": None},
             {"role": "chat", "verdict": "gut", "action": "keine", "reason": "ok"}]
    switches = asyncio.run(s._auto_switch(roles))
    assert len(switches) == 1
    assert switches[0]["role"] == "analyst"
    assert switches[0]["from"]["model"] == "gemini-3.5-flash"
    assert switches[0]["to"]["model"] == "llama-3.3-70b-versatile"
    assert role_manager.config["analyst"]["model"] == "llama-3.3-70b-versatile"
    # Rollback stellt das vorherige Modell wieder her
    res = asyncio.run(s.rollback_switches())
    assert res["status"] == "ok" and res["restored"] == ["analyst"]
    assert role_manager.config["analyst"]["model"] == "gemini-3.5-flash"
    assert s.last_switches == []
    assert asyncio.run(s.rollback_switches())["status"] == "error"
    _reset_roles()


def test_auto_switch_falls_back_to_suggested_model():
    _reset_roles()
    s = _sup()
    s.settings["auto_switch"] = True
    role_manager.config["chat"].update({"fallback_provider": None, "fallback_model": None})
    roles = [{"role": "chat", "verdict": "schwach", "reason": "ungenau",
              "suggested_provider": "mistral", "suggested_model": "mistral-small-latest"}]
    switches = asyncio.run(s._auto_switch(roles))
    assert switches[0]["to"]["model"] == "mistral-small-latest"
    assert role_manager.config["chat"]["model"] == "mistral-small-latest"
    _reset_roles()


def test_auto_switch_also_on_explicit_recommendation():
    """Ausfälle (503/Rate-Limit) werden häufig als 'inaktiv' mit ausdrücklicher
    Empfehlung 'modell_wechseln' bewertet – die sollen auch greifen."""
    _reset_roles()
    s = _sup()
    s.settings["auto_switch"] = True
    role_manager.config["summarizer"].update({
        "provider": "gemini", "model": "gemini-3.5-flash",
        "fallback_provider": None, "fallback_model": None})
    roles = [{"role": "summarizer", "verdict": "inaktiv", "action": "modell_wechseln",
              "reason": "503-Fehler beim Provider",
              "suggested_provider": "groq", "suggested_model": "llama-3.3-70b-versatile"}]
    switches = asyncio.run(s._auto_switch(roles))
    assert switches[0]["to"]["model"] == "llama-3.3-70b-versatile"
    _reset_roles()


def test_auto_switch_skips_when_already_active():
    _reset_roles()
    s = _sup()
    s.settings["auto_switch"] = True
    role_manager.config["learner"].update({"provider": "groq",
                                           "model": "llama-3.3-70b-versatile"})
    roles = [{"role": "learner", "verdict": "schwach", "reason": "x",
              "suggested_provider": "groq", "suggested_model": "llama-3.3-70b-versatile"}]
    assert asyncio.run(s._auto_switch(roles)) == []
    _reset_roles()


# ---------------- Verbesserungen übernehmen ----------------
def _lab_with_candidate(last_assist):
    lab = StrategyLab()
    lab.engine = FakeEngine(LabFakeDB())
    lab.db.ai_strategy_candidates.docs.append({
        "id": "cand_x", "name": "Test", "thesis": "alt", "rules_text": "alte Regeln",
        "symbols": ["BTCUSDT"], "stage": "ghost", "last_assist": last_assist})
    return lab


_RD = {"timeframe": "5m", "indicators": {"rsi_period": 14},
       "long_rules": [{"indicator": "rsi", "op": "<", "value": 30}], "short_rules": []}


def test_apply_assist_applies_selected_fields():
    lab = _lab_with_candidate({"improved_thesis": "neue Idee",
                               "improved_rules_text": "neue Regeln",
                               "rule_definition": _RD})
    res = asyncio.run(lab.apply_assist("cand_x", ["thesis"]))
    assert res["status"] == "ok" and res["applied"] == ["thesis"]
    cand = asyncio.run(lab.get("cand_x"))
    assert cand["thesis"] == "neue Idee"
    assert cand["rules_text"] == "alte Regeln"          # nicht angetastet
    assert "rule_definition" not in cand


def test_apply_assist_all_fields_and_registers():
    lab = _lab_with_candidate({"improved_thesis": "neue Idee",
                               "improved_rules_text": "neue Regeln",
                               "rule_definition": _RD})
    res = asyncio.run(lab.apply_assist("cand_x", None))
    assert set(res["applied"]) == {"thesis", "rules_text", "rule_definition"}
    assert res["registered"]["status"] == "ok"
    cand = asyncio.run(lab.get("cand_x"))
    assert cand["rule_definition"]["timeframe"] == "5m"
    assert cand["custom_strategy_id"]


def test_apply_assist_without_assist_errors():
    lab = _lab_with_candidate({})
    assert asyncio.run(lab.apply_assist("cand_x"))["status"] == "error"
    assert asyncio.run(lab.apply_assist("gibt-es-nicht"))["status"] == "error"


def test_apply_assist_ignores_invalid_rule_definition():
    lab = _lab_with_candidate({"rule_definition": {"timeframe": "5m"}})
    assert asyncio.run(lab.apply_assist("cand_x", ["rule_definition"]))["status"] == "error"
