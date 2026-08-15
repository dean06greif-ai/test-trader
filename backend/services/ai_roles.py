"""KI-Rollen-Verwaltung ("KI-Team").

Das KI-Ökosystem besteht aus spezialisierten Rollen, die zusammenarbeiten:
  - analyst          : regelmäßige Markt-Analysen (bestehender Analyse-Loop)
  - deep_analyst     : sehr tiefe Analysen zu konfigurierbaren Uhrzeiten
  - research_analyst : wertet Backtests/Optimizer/Regime-Lab aus und gibt das
                       gewonnene Wissen an das Team weiter
  - market_observer  : sammelt laufend den gemessenen Marktzustand (Trainingsdaten)
  - news_watcher     : überwacht News + Wirtschaftskalender 24/7
  - chat             : beantwortet User-Anfragen im KI-Chat
  - learner          : Lernläufe (Lektionen aus echten Ergebnissen)
  - summarizer       : Tages-Zusammenfassung um Mitternacht

Jede Rolle kann ein eigenes Modell, aktive Handelszeiten (Europe/Berlin) und
ein Fallback-Modell haben. Für jede Rolle ist ein sinnvolles, kostengünstiges
Modell VOREINGESTELLT (ROLE_PRESETS) – sobald der Trader im UI eine eigene Wahl
trifft, wird diese dauerhaft respektiert (`user_configured`). Rollen ohne
Voreinstellung erben das Haupt-Modell der Engine.
"""
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from services import ai_providers

logger = logging.getLogger(__name__)

BERLIN_TZ = ZoneInfo("Europe/Berlin")

ROLE_LABELS = {
    "analyst": "Analyst – regelmäßige Analysen",
    "deep_analyst": "Tiefen-Analyst – geplante Deep-Analysen",
    "research_analyst": "Forschungs-Analyst – Backtests, Optimizer & Regime-Lab auswerten",
    "market_observer": "Markt-Beobachter – sammelt Marktzustände als Trainingsdaten",
    "trade_manager": "Trade-Manager – eröffnet und steuert Trades (SL/TP, Margin, Hebel, Teil-Close)",
    "news_watcher": "News-Wächter – News + Wirtschaftskalender 24/7",
    "chat": "Chat-Assistent – User-Anfragen",
    "learner": "Lern-Modul – Lektionen aus Ergebnissen",
    "summarizer": "Tages-Reporter – Mitternachts-Zusammenfassung",
}

# Voreinstellungen: pro Rolle das beste preis-/leistungsstarke Modell aus dem
# bestehenden Katalog (services/ai_providers.ALLOWED_MODELS) inkl. Fallback-KI.
# Im UI jederzeit änderbar – eine eigene Auswahl überschreibt die Voreinstellung.
ROLE_PRESETS: Dict[str, Dict] = {
    # Läuft am häufigsten -> bestes Gratis-Reasoning-Modell, starke Gratis-Fallbacks
    "analyst": {"provider": "groq", "model": "openai/gpt-oss-120b",
                "fallback_provider": "cerebras", "fallback_model": "gpt-oss-120b",
                "fallback2_provider": "gemini", "fallback2_model": "gemini-3.5-flash-lite"},
    # Wenige Läufe pro Tag -> stärkstes verifiziertes Free-Reasoning-Modell
    "deep_analyst": {"provider": "openrouter",
                     "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                     "fallback_provider": "groq",
                     "fallback_model": "openai/gpt-oss-120b"},
    # Muss große Datenmengen sauber auswerten -> starkes Modell mit hohem Kontext
    "research_analyst": {"provider": "groq", "model": "openai/gpt-oss-120b",
                         "fallback_provider": "openrouter",
                         "fallback_model": "nvidia/nemotron-3-super-120b-a12b:free",
                         "fallback2_provider": "cerebras", "fallback2_model": "gpt-oss-120b"},
    # Reine Datensammlung, LLM nur optional -> günstigstes Modell
    "market_observer": {"provider": "groq", "model": "llama-3.1-8b-instant",
                        "fallback_provider": "gemini", "fallback_model": "gemini-3.1-flash-lite"},
    # KRITISCH: muss zuverlässig rechnen -> starkes Gratis-Modell + 2 Fallbacks
    "trade_manager": {"provider": "groq", "model": "openai/gpt-oss-120b",
                      "fallback_provider": "cerebras", "fallback_model": "gpt-oss-120b",
                      "fallback2_provider": "gemini", "fallback2_model": "gemini-3.5-flash"},
    # 24/7-Betrieb -> billigstes Modell (gratis), günstiger Fallback
    "news_watcher": {"provider": "groq", "model": "llama-3.1-8b-instant",
                     "fallback_provider": "gemini", "fallback_model": "gemini-3.1-flash-lite"},
    "chat": {"provider": "groq", "model": "openai/gpt-oss-120b",
             "fallback_provider": "gemini", "fallback_model": "gemini-3.5-flash"},
    # KRITISCH: Lektionen wirken dauerhaft -> starkes Gratis-Reasoning primär,
    # Qualitäts-Fallback auf Gemini Pro (selten -> Cent-Beträge)
    "learner": {"provider": "groq", "model": "openai/gpt-oss-120b",
                "fallback_provider": "gemini", "fallback_model": "gemini-3.1-pro-preview",
                "fallback2_provider": "openrouter",
                "fallback2_model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    "summarizer": {"provider": "gemini", "model": "gemini-3.1-flash-lite",
                   "fallback_provider": "mistral", "fallback_model": "mistral-small-latest"},
}

# Basis-Felder jeder Rolle. provider/model = None => erbt Haupt-Modell.
_BASE_ROLE = {
    "enabled": True,
    "provider": None,
    "model": None,
    "active_hours": None,           # {"start": "08:00", "end": "22:00"} Berlin oder None (=immer)
    "fallback_provider": None,      # greift außerhalb active_hours oder wenn Primär komplett scheitert
    "fallback_model": None,
    "fallback2_provider": None,     # zweite Fallback-Stufe: greift, wenn auch Fallback 1 scheitert
    "fallback2_model": None,
    "user_configured": False,       # True, sobald der Trader die Rolle selbst konfiguriert hat
}

DEFAULT_ROLES_CONFIG: Dict[str, Dict] = {
    "analyst": {**_BASE_ROLE, **ROLE_PRESETS["analyst"]},
    "deep_analyst": {**_BASE_ROLE, **ROLE_PRESETS["deep_analyst"],
                     "schedule_times": ["08:00", "20:00"]},
    "research_analyst": {**_BASE_ROLE, **ROLE_PRESETS["research_analyst"],
                         "schedule_times": ["06:30", "18:30"],
                         "interval_hours": 12,
                         "auto_on_new_results": True,
                         "trigger_after_results": 1},
    "market_observer": {**_BASE_ROLE, **ROLE_PRESETS["market_observer"],
                        "interval_min": 15, "llm_summary": False},
    "trade_manager": {**_BASE_ROLE, **ROLE_PRESETS["trade_manager"]},
    "news_watcher": {**_BASE_ROLE, **ROLE_PRESETS["news_watcher"],
                     "interval_min": 15, "auto_analysis": True},
    "chat": {**_BASE_ROLE, **ROLE_PRESETS["chat"]},
    "learner": {**_BASE_ROLE, **ROLE_PRESETS["learner"]},
    "summarizer": {**_BASE_ROLE, **ROLE_PRESETS["summarizer"]},
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _valid_time(t) -> bool:
    return isinstance(t, str) and bool(_TIME_RE.match(t))


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def in_active_hours(active_hours: Optional[Dict], now: Optional[datetime] = None) -> bool:
    """True, wenn `now` (Berlin) im Fenster liegt. Fenster über Mitternacht erlaubt."""
    if not active_hours:
        return True
    start, end = active_hours.get("start"), active_hours.get("end")
    if not (_valid_time(start) and _valid_time(end)):
        return True
    now = now or datetime.now(BERLIN_TZ)
    cur = now.hour * 60 + now.minute
    s, e = _minutes(start), _minutes(end)
    if s == e:
        return True
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e  # über Mitternacht


class AIRoleManager:
    def __init__(self):
        self.config: Dict[str, Dict] = {r: dict(c) for r, c in DEFAULT_ROLES_CONFIG.items()}

    async def load(self, db):
        try:
            doc = await db.settings.find_one({"_id": "ai_roles_config"})
            # Einmalige Kosten-Migration (Juni 2026, Wunsch des Traders):
            # ALLE Rollen auf die neuen Preis-/Leistungs-Presets setzen
            # (Gratis-Modelle primär). Danach greifen gespeicherte Configs
            # wieder normal; eigene Änderungen im UI bleiben ab dann erhalten.
            if doc is not None and not doc.get("_cost_migration_v1"):
                self.config = {r: dict(c) for r, c in DEFAULT_ROLES_CONFIG.items()}
                payload = {r: dict(c) for r, c in self.config.items()}
                payload["_cost_migration_v1"] = True
                await db.settings.update_one(
                    {"_id": "ai_roles_config"}, {"$set": payload}, upsert=True)
                logger.info("KI-Rollen: einmalige Kosten-Migration auf "
                            "Preis-/Leistungs-Presets durchgeführt")
                return
            if doc:
                doc.pop("_id", None)
                doc.pop("_cost_migration_v1", None)
                for role, cfg in doc.items():
                    if role not in self.config or not isinstance(cfg, dict):
                        continue
                    # Migration: tote/umbenannte Modell-Slugs auf verifizierte
                    # Nachfolger mappen, bevor die Sanitize-Prüfung sie verwirft.
                    for pk, mk in (("provider", "model"),
                                   ("fallback_provider", "fallback_model"),
                                   ("fallback2_provider", "fallback2_model")):
                        old_m = cfg.get(mk)
                        if old_m and old_m not in ai_providers.ALLOWED_MODELS.get(
                                cfg.get(pk) or "", []):
                            new_p, new_m = ai_providers.migrate_model(cfg.get(pk), old_m)
                            if new_m != old_m or new_p != cfg.get(pk):
                                logger.info(f"AI role {role}: Modell migriert "
                                            f"{cfg.get(pk)}/{old_m} -> {new_p}/{new_m}")
                                cfg[pk], cfg[mk] = new_p, new_m
                    clean = self._sanitize(role, cfg)
                    # Voreinstellungen nur überschreiben, wenn der Trader die
                    # Rolle selbst konfiguriert hat (oder – Altbestand – ein
                    # eigenes Modell gespeichert ist).
                    owned = bool(cfg.get("user_configured")) or bool(cfg.get("model"))
                    if not owned:
                        for k in ("provider", "model", "fallback_provider", "fallback_model",
                                  "fallback2_provider", "fallback2_model"):
                            clean.pop(k, None)
                    clean["user_configured"] = owned
                    self.config[role].update(clean)
        except Exception as e:
            logger.warning(f"AI roles load failed: {e}")

    def _sanitize(self, role: str, updates: Dict) -> Dict:
        out = {}
        if "enabled" in updates:
            out["enabled"] = bool(updates["enabled"])
        prov, mod = updates.get("provider"), updates.get("model")
        if prov is None and mod is None and ("provider" in updates or "model" in updates):
            out["provider"] = None
            out["model"] = None
        elif mod:
            p = prov if prov in ai_providers.ALLOWED_MODELS else ai_providers.provider_for_model(mod)
            if p and mod in ai_providers.ALLOWED_MODELS[p]:
                out["provider"], out["model"] = p, mod
        if "active_hours" in updates:
            ah = updates["active_hours"]
            if ah and isinstance(ah, dict) and _valid_time(ah.get("start")) and _valid_time(ah.get("end")):
                out["active_hours"] = {"start": ah["start"], "end": ah["end"]}
            else:
                out["active_hours"] = None
        for prefix in ("fallback", "fallback2"):
            fp, fm = updates.get(f"{prefix}_provider"), updates.get(f"{prefix}_model")
            if f"{prefix}_model" in updates:
                if fm:
                    p = fp if fp in ai_providers.ALLOWED_MODELS else ai_providers.provider_for_model(fm)
                    if p and fm in ai_providers.ALLOWED_MODELS[p]:
                        out[f"{prefix}_provider"], out[f"{prefix}_model"] = p, fm
                else:
                    out[f"{prefix}_provider"] = None
                    out[f"{prefix}_model"] = None
        if role in ("deep_analyst", "research_analyst") and "schedule_times" in updates:
            times = [t for t in (updates["schedule_times"] or []) if _valid_time(t)]
            out["schedule_times"] = sorted(set(times))[:6]
        if role == "research_analyst":
            if "interval_hours" in updates:
                try:
                    out["interval_hours"] = max(1, min(168, int(updates["interval_hours"])))
                except (TypeError, ValueError):
                    pass
            if "auto_on_new_results" in updates:
                out["auto_on_new_results"] = bool(updates["auto_on_new_results"])
            if "trigger_after_results" in updates:
                try:
                    out["trigger_after_results"] = max(1, min(50, int(updates["trigger_after_results"])))
                except (TypeError, ValueError):
                    pass
        if role in ("news_watcher", "market_observer") and "interval_min" in updates:
            try:
                out["interval_min"] = max(5, min(120, int(updates["interval_min"])))
            except (TypeError, ValueError):
                pass
        if role == "market_observer" and "llm_summary" in updates:
            out["llm_summary"] = bool(updates["llm_summary"])
        if role == "news_watcher" and "auto_analysis" in updates:
            out["auto_analysis"] = bool(updates["auto_analysis"])
        return out

    async def update(self, db, updates: Dict) -> Dict:
        for role, cfg in (updates or {}).items():
            if role in self.config and isinstance(cfg, dict):
                clean = self._sanitize(role, cfg)
                if any(k in clean for k in ("provider", "model", "fallback_provider",
                                            "fallback_model", "fallback2_provider",
                                            "fallback2_model")):
                    clean["user_configured"] = True
                self.config[role].update(clean)
        await db.settings.update_one(
            {"_id": "ai_roles_config"},
            {"$set": {r: dict(c) for r, c in self.config.items()}}, upsert=True)
        return self.snapshot()

    async def reset_role(self, db, role: str) -> Dict:
        """Rolle auf die Voreinstellung zurücksetzen (UI: 'Voreinstellung')."""
        if role in DEFAULT_ROLES_CONFIG:
            self.config[role] = dict(DEFAULT_ROLES_CONFIG[role])
            await db.settings.update_one(
                {"_id": "ai_roles_config"},
                {"$set": {role: dict(self.config[role])}}, upsert=True)
        return self.snapshot()

    def snapshot(self) -> Dict:
        return {r: dict(c) for r, c in self.config.items()}

    def role_cfg(self, role: str) -> Dict:
        return self.config.get(role) or dict(_BASE_ROLE)

    def chain(self, role: str, engine_cfg: Dict,
              now: Optional[datetime] = None) -> List[Tuple[str, str]]:
        """(provider, model)-Kette für eine Rolle.

        Primär = Rollen-Modell (oder Haupt-Modell der Engine) + Provider-interne
        Fallbacks. Außerhalb der aktiven Handelszeiten übernimmt direkt die
        Fallback-KI. Fallback 1 und Fallback 2 hängen immer als letzte Stufen an
        der Kette (Fallback 2 greift, wenn Primär UND Fallback 1 scheitern oder
        leere/unbrauchbare Antworten liefern).
        Zuletzt werden alle Provider angehängt, für die überhaupt ein API-Key
        gesetzt ist – so bleibt eine Rolle auch dann arbeitsfähig, wenn ihre
        Voreinstellung einen Provider ohne Key nutzt."""
        cfg = self.role_cfg(role)
        provider = cfg.get("provider") or engine_cfg.get("provider", "gemini")
        model = cfg.get("model") or (engine_cfg.get("model")
                                     if not cfg.get("provider") else None)
        primary = ai_providers.same_provider_chain(provider, model)

        fallback: List[Tuple[str, str]] = []
        for prefix in ("fallback", "fallback2"):
            fm = cfg.get(f"{prefix}_model")
            if fm:
                fb_prov = cfg.get(f"{prefix}_provider") or \
                    ai_providers.provider_for_model(fm)
                if fb_prov:
                    fallback += ai_providers.same_provider_chain(fb_prov, fm)

        active = in_active_hours(cfg.get("active_hours"), now)
        chain = (fallback + primary) if (not active and fallback) else (primary + fallback)
        chain = chain + self._available_chain()
        seen, out = set(), []
        for pm in chain:
            if pm not in seen:
                seen.add(pm)
                out.append(pm)
        return out

    @staticmethod
    def _available_chain() -> List[Tuple[str, str]]:
        """Letzte Rettung: alle Provider mit gesetztem API-Key."""
        out: List[Tuple[str, str]] = []
        for prov, has_key in ai_providers.available_providers().items():
            if has_key:
                out.extend(ai_providers.same_provider_chain(prov, None))
        return out


role_manager = AIRoleManager()
