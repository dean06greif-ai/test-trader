"""Aufsicht des Haupt-Modells über das gesamte KI-Team.

Das Haupt-KI-Modell (Engine-Modell, NICHT eine Rolle) prüft stichprobenweise, ob
jede Rolle des KI-Teams ihre Arbeit zuverlässig macht:

  * arbeitet die Rolle überhaupt (letzter Lauf, Fehler, Rate-Limits)?
  * ist die Qualität der Ausgaben plausibel (Stichproben aus dem KI-Feed)?
  * sollte das Modell der Rolle gewechselt werden (zu fehlerhaft/ungenau)?

Der Prüflauf ist bewusst manuell startbar (Button im KI-Team-Tab). Die Bewertung
wird als Bericht gespeichert (`settings/ai_supervisor_report`) und als
Feed-Eintrag (`role="supervisor"`) im KI-Chat abgelegt. Angewendet wird NICHTS
automatisch – Modellwechsel bleiben eine Entscheidung des Traders.

Reine Aufbereitungs-Funktionen (`evidence_text`, `normalize_report`) sind ohne
DB/LLM testbar.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DOC_ID = "ai_supervisor_report"
SETTINGS_ID = "ai_supervisor_settings"
HIST_COLL = "ai_supervisor_reports"

DEFAULT_SETTINGS = {
    "auto_enabled": False,      # täglich automatisch prüfen
    "interval_hours": 24,
    "auto_switch": False,       # bei "schwach" automatisch auf Fallback umschalten
}

# Rollen, die geprüft werden (Reihenfolge = Anzeige-Reihenfolge im UI)
SUPERVISED_ROLES = ("analyst", "deep_analyst", "research_analyst", "market_observer",
                    "trade_manager", "news_watcher", "chat", "learner", "summarizer")

VERDICTS = ("gut", "auffällig", "schwach", "inaktiv")
ACTIONS = ("keine", "modell_wechseln", "einstellungen_pruefen", "deaktivieren")

# Feed-Rollen, aus denen Stichproben der jeweiligen KI-Rolle gezogen werden
SAMPLE_ROLES = {
    "analyst": ["analysis"],
    "deep_analyst": ["deep_analysis"],
    "research_analyst": ["research"],
    "news_watcher": ["news_alert"],
    "chat": ["assistant"],
    "learner": ["learning"],
    "summarizer": ["summary"],
    "trade_manager": ["trade"],
}

SUPERVISOR_SYSTEM = (
    "Du bist das HAUPT-MODELL einer Krypto-Daytrading-Plattform und führst die Aufsicht über "
    "dein KI-Team. Du bekommst pro Rolle: eingesetztes Modell, Fallback 1 & Fallback 2, letzte "
    "Aktivität, Fehler, Kennzahlen und STICHPROBEN echter Ausgaben. Prüfe kritisch und datenbasiert:\n"
    "- Arbeitet die Rolle überhaupt (Aktivität, Fehler, Rate-Limits)?\n"
    "- Sind die Stichproben inhaltlich brauchbar, konkret und widerspruchsfrei? Prüfe auf "
    "Halluzinationen, generische Floskeln, wiederholte identische Ausgaben und Widersprüche "
    "zwischen Begründung und Entscheidung.\n"
    "- Ist das Modell für die Aufgabe zu schwach/ungenau -> Modellwechsel empfehlen?\n"
    "- Ist die Fallback-Kette der Rolle sinnvoll (Fallback 1/2 gesetzt, andere Provider als das "
    "Hauptmodell, damit ein Provider-Ausfall nicht die ganze Kette lahmlegt)? Fehlende oder "
    "schlecht gewählte Fallbacks gehören in die recommendations.\n"
    "Bei verdict='schwach' schaltet das System automatisch kaskadierend um: erst auf Fallback 1, "
    "dann auf Fallback 2 – vergib 'schwach' daher nur bei klaren Belegen.\n"
    "Sei ehrlich: fehlende Daten sind KEIN Mangel der Rolle – dann verdict='gut' oder "
    "'inaktiv' mit klarer Begründung. Empfehle einen Modellwechsel NUR, wenn die Belege ihn "
    "stützen, und ausschliesslich Modelle aus dem erlaubten Katalog.\n"
    "recommendations: 2-5 KONKRETE, priorisierte und sofort umsetzbare Vorschläge (wichtigste "
    "zuerst), jeweils mit Rolle, Massnahme und 1-Satz-Begründung aus den Belegen – keine "
    "Allgemeinplätze wie 'weiter beobachten'.\n"
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"summary": "3-6 Sätze Gesamtbild auf Deutsch", '
    '"roles": [{"role": "analyst", "verdict": "gut|auffällig|schwach|inaktiv", '
    '"score": 0-100, "reason": "kurze Begründung mit Bezug auf die Belege", '
    '"action": "keine|modell_wechseln|einstellungen_pruefen|deaktivieren", '
    '"suggested_provider": "gemini|groq|openrouter|mistral|github|cerebras oder null", '
    '"suggested_model": "Modellname aus dem Katalog oder null"}], '
    '"recommendations": ["Rolle: konkrete Massnahme – Begründung"]}'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(v, limit: int = 220) -> str:
    return str(v if v is not None else "—")[:limit]


# ---------------- reine Aufbereitung (testbar) ----------------
def evidence_text(evidence: Dict) -> str:
    """Belege pro Rolle in kompakten Prompt-Text übersetzen (rein, testbar)."""
    lines: List[str] = []
    for role in SUPERVISED_ROLES:
        ev = (evidence or {}).get(role)
        if not ev:
            continue
        lines.append(f"--- ROLLE: {role} ---")
        lines.append(f"  Modell: {_short(ev.get('model'), 80)} "
                     f"(Fallback 1: {_short(ev.get('fallback_model'), 80)} · "
                     f"Fallback 2: {_short(ev.get('fallback2_model'), 80)}) · "
                     f"aktiv: {'ja' if ev.get('enabled', True) else 'NEIN'}")
        lines.append(f"  Letzte Aktivität: {_short(ev.get('last_run'), 40)} · "
                     f"Letzter Fehler: {_short(ev.get('last_error'))}")
        metrics = ev.get("metrics") or {}
        if metrics:
            lines.append("  Kennzahlen: " + ", ".join(
                f"{k}={_short(v, 60)}" for k, v in list(metrics.items())[:8]))
        samples = ev.get("samples") or []
        if samples:
            lines.append("  Stichproben:")
            for s in samples[:3]:
                lines.append(f"    · [{_short(s.get('ts'), 16)}] {_short(s.get('text'), 320)}")
        else:
            lines.append("  Stichproben: (keine Ausgaben im Zeitfenster)")
    return "\n".join(lines) or "(keine Belege)"


def normalize_report(data: Dict, allowed_models: Dict) -> Dict:
    """LLM-Antwort in einen stabilen Bericht überführen (rein, testbar)."""
    roles_out: List[Dict] = []
    for item in (data or {}).get("roles") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in SUPERVISED_ROLES:
            continue
        verdict = str(item.get("verdict") or "gut").lower()
        if verdict not in VERDICTS:
            verdict = "gut"
        action = str(item.get("action") or "keine").lower()
        if action not in ACTIONS:
            action = "keine"
        provider = item.get("suggested_provider") or None
        model = item.get("suggested_model") or None
        if not (provider and model and model in (allowed_models.get(provider) or [])):
            provider, model = None, None
            if action == "modell_wechseln":
                action = "einstellungen_pruefen"
        try:
            score = max(0, min(100, int(item.get("score"))))
        except (TypeError, ValueError):
            score = {"gut": 85, "auffällig": 55, "schwach": 25, "inaktiv": 40}[verdict]
        roles_out.append({
            "role": role, "verdict": verdict, "score": score,
            "reason": str(item.get("reason") or "")[:400],
            "action": action,
            "suggested_provider": provider, "suggested_model": model,
        })
    return {
        "summary": str((data or {}).get("summary") or "")[:1500],
        "roles": roles_out,
        "recommendations": [str(r)[:300] for r in ((data or {}).get("recommendations") or [])
                            if isinstance(r, str)][:8],
    }


class TeamSupervisor:
    """Prüft stichprobenweise die Arbeit aller KI-Team-Rollen."""

    def __init__(self):
        self.engine = None
        self.report: Optional[Dict] = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)
        self.last_switches: List[Dict] = []
        self.running_now: bool = False
        self.last_error: Optional[str] = None

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": DOC_ID})
            if doc:
                doc.pop("_id", None)
                self.report = doc
            cfg = await self.db.settings.find_one({"_id": SETTINGS_ID})
            if cfg:
                cfg.pop("_id", None)
                self.last_switches = cfg.pop("last_switches", []) or []
                self.settings.update(self._sanitize_settings(cfg))
        except Exception as e:
            logger.warning(f"Supervisor-State laden fehlgeschlagen: {e}")

    # ---------------- Einstellungen ----------------
    @staticmethod
    def _sanitize_settings(updates: Dict) -> Dict:
        out: Dict = {}
        if "auto_enabled" in updates:
            out["auto_enabled"] = bool(updates["auto_enabled"])
        if "auto_switch" in updates:
            out["auto_switch"] = bool(updates["auto_switch"])
        if "interval_hours" in updates:
            try:
                out["interval_hours"] = max(6, min(168, int(updates["interval_hours"])))
            except (TypeError, ValueError):
                pass
        return out

    async def update_settings(self, updates: Dict) -> Dict:
        self.settings.update(self._sanitize_settings(updates or {}))
        await self.db.settings.update_one({"_id": SETTINGS_ID},
                                          {"$set": dict(self.settings)}, upsert=True)
        return dict(self.settings)

    async def history(self, limit: int = 10) -> List[Dict]:
        """Verlauf der Prüfberichte (neueste zuerst)."""
        limit = max(1, min(50, limit))
        try:
            rows = await self.db[HIST_COLL].find().sort("ts", -1).limit(limit).to_list(limit)
        except Exception as e:
            logger.warning(f"Supervisor-Historie nicht ladbar: {e}")
            return []
        for r in rows:
            r.pop("_id", None)
        return rows

    def _due(self) -> bool:
        if not self.settings.get("auto_enabled"):
            return False
        ts = (self.report or {}).get("ts")
        if not ts:
            return True
        try:
            last = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last >= timedelta(
            hours=int(self.settings.get("interval_hours", 24)))

    async def run_loop(self):
        """Täglicher Selbstlauf (nur wenn eingeschaltet)."""
        await asyncio.sleep(120)
        while True:
            try:
                if self._due() and not self.running_now:
                    logger.info("Supervisor: automatische KI-Team-Prüfung fällig")
                    await self.run_review(manual=False)
            except Exception as e:
                logger.error(f"Supervisor-Loop: {e}")
            await asyncio.sleep(600)

    # ---------------- Modellwechsel bei "schwach" ----------------
    async def _auto_switch(self, roles: List[Dict]) -> List[Dict]:
        """Schwache Rollen (Urteil "schwach" oder ausdrückliche Empfehlung
        "modell_wechseln", z. B. bei 503-/Rate-Limit-Ausfällen) kaskadierend
        umstellen: zuerst auf Fallback 1 der Rolle, ist dieses bereits aktiv
        auf Fallback 2, danach auf das von der Aufsicht empfohlene Modell.
        Wird protokolliert und ist per Rollback umkehrbar."""
        if not self.settings.get("auto_switch"):
            return []
        from services.ai_roles import role_manager
        switches: List[Dict] = []
        for r in roles:
            if r.get("verdict") != "schwach" and r.get("action") != "modell_wechseln":
                continue
            cfg = role_manager.role_cfg(r["role"])
            current = cfg.get("model")
            # Kaskade: Fallback 1 -> Fallback 2 -> Empfehlung der Aufsicht.
            candidates = []
            if cfg.get("fallback_model"):
                candidates.append({"provider": cfg.get("fallback_provider"),
                                   "model": cfg["fallback_model"],
                                   "via": "Fallback 1 der Rolle"})
            if cfg.get("fallback2_model"):
                candidates.append({"provider": cfg.get("fallback2_provider"),
                                   "model": cfg["fallback2_model"],
                                   "via": "Fallback 2 der Rolle"})
            if r.get("suggested_model"):
                candidates.append({"provider": r.get("suggested_provider"),
                                   "model": r["suggested_model"],
                                   "via": "Empfehlung der Aufsicht"})
            target = next((c for c in candidates if c["model"] != current), None)
            if not target:
                continue
            prev = {"provider": cfg.get("provider"), "model": cfg.get("model")}
            try:
                await role_manager.update(self.db, {r["role"]: {
                    "provider": target["provider"], "model": target["model"]}})
            except Exception as e:
                logger.warning(f"Auto-Modellwechsel für {r['role']} fehlgeschlagen: {e}")
                continue
            switches.append({"role": r["role"], "from": prev, "to": target,
                             "reason": r.get("reason", ""), "ts": _now_iso()})
            logger.warning(f"Supervisor: Rolle {r['role']} von "
                           f"{prev.get('model') or 'Haupt-Modell'} auf {target['model']} "
                           f"umgestellt ({target['via']})")
        if switches:
            self.last_switches = switches
            await self.db.settings.update_one(
                {"_id": SETTINGS_ID},
                {"$set": {**self.settings, "last_switches": switches}}, upsert=True)
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "supervisor",
                "text": ("Automatischer Modellwechsel: "
                         + "; ".join(f"{s['role']} → {s['to']['model']}" for s in switches)
                         + ". Rückgängig über „Umschaltung zurücknehmen“."),
                "switches": switches, "ts": _now_iso()})
        return switches

    async def rollback_switches(self) -> Dict:
        """Letzte automatische Umschaltung(en) zurücknehmen."""
        if not self.last_switches:
            return {"status": "error", "detail": "Keine automatische Umschaltung vorhanden"}
        from services.ai_roles import role_manager
        restored = []
        for s in self.last_switches:
            prev = s.get("from") or {}
            try:
                await role_manager.update(self.db, {s["role"]: {
                    "provider": prev.get("provider"), "model": prev.get("model")}})
                restored.append(s["role"])
            except Exception as e:
                logger.warning(f"Rollback für {s.get('role')} fehlgeschlagen: {e}")
        self.last_switches = []
        await self.db.settings.update_one({"_id": SETTINGS_ID},
                                          {"$set": {"last_switches": []}}, upsert=True)
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "supervisor",
            "text": f"Automatische Umschaltung zurückgenommen für: {', '.join(restored) or '—'}",
            "ts": _now_iso()})
        return {"status": "ok", "restored": restored}

    # ---------------- Belege sammeln ----------------
    async def _samples(self, feed_roles: List[str], limit: int = 3) -> List[Dict]:
        if self.db is None or not feed_roles:
            return []
        try:
            rows = await self.db.ai_chat.find({"role": {"$in": feed_roles}}) \
                .sort("ts", -1).limit(limit).to_list(limit)
        except Exception:
            return []
        return [{"ts": r.get("ts"), "text": r.get("text") or ""} for r in rows]

    async def collect_evidence(self) -> Dict:
        """Belege pro Rolle einsammeln (Modell, Aktivität, Fehler, Stichproben)."""
        from services.ai_roles import role_manager
        from services import ai_providers

        engine = self.engine
        status = engine.status() if engine else {}
        roles_cfg = role_manager.snapshot()
        health = status.get("providers_health") or {}

        def _role_model(role: str) -> str:
            cfg = roles_cfg.get(role) or {}
            if cfg.get("model"):
                return f"{cfg.get('provider')}/{cfg['model']}"
            return (f"{status.get('config', {}).get('provider')}/"
                    f"{status.get('config', {}).get('model')} (geerbt)")

        ev: Dict[str, Dict] = {}
        for role in SUPERVISED_ROLES:
            cfg = roles_cfg.get(role) or {}
            ev[role] = {
                "model": _role_model(role),
                "fallback_model": (f"{cfg.get('fallback_provider')}/{cfg['fallback_model']}"
                                   if cfg.get("fallback_model") else None),
                "fallback2_model": (f"{cfg.get('fallback2_provider')}/{cfg['fallback2_model']}"
                                    if cfg.get("fallback2_model") else None),
                "enabled": cfg.get("enabled", True),
                "last_run": None,
                "last_error": None,
                "metrics": {},
                "samples": await self._samples(SAMPLE_ROLES.get(role) or []),
            }

        ev["analyst"]["last_run"] = status.get("last_run")
        ev["analyst"]["last_error"] = status.get("last_error")
        ev["analyst"]["metrics"] = {
            "entscheidungen_im_cache": len(status.get("decisions") or {}),
            "intervall_min": (status.get("config") or {}).get("interval_min"),
        }
        ev["deep_analyst"]["last_run"] = status.get("deep_last")
        ev["deep_analyst"]["last_error"] = status.get("deep_last_error")

        def _plug(role: str, st: Dict, metric_keys: List[str]):
            if not isinstance(st, dict):
                return
            ev[role]["last_run"] = st.get("last_run") or st.get("last_report") or \
                st.get("last_check") or st.get("last_tick")
            ev[role]["last_error"] = st.get("last_error")
            ev[role]["metrics"].update({k: st.get(k) for k in metric_keys if k in st})

        try:
            from services.ai_research import research_analyst
            _plug("research_analyst", research_analyst.status(),
                  ["counts", "insights", "running_now"])
        except Exception as e:
            logger.debug(f"Supervisor: research status {e}")
        try:
            from services.ai_market_observer import market_observer
            _plug("market_observer", market_observer.status(),
                  ["symbols_tracked", "interval_min", "last_summary"])
        except Exception as e:
            logger.debug(f"Supervisor: observer status {e}")
        try:
            from services.ai_trade_manager import trade_manager
            _plug("trade_manager", trade_manager.status(), ["last_note", "role_enabled"])
        except Exception as e:
            logger.debug(f"Supervisor: trade manager status {e}")
        try:
            from services.ai_news_watcher import news_watcher
            _plug("news_watcher", news_watcher.status(), ["last_alert", "running"])
        except Exception as e:
            logger.debug(f"Supervisor: news status {e}")

        learning = status.get("learning") or {}
        ev["learner"]["metrics"] = {k: learning.get(k) for k in
                                    ("lessons_count", "last_learn", "trigger") if k in learning}
        ev["learner"]["last_run"] = learning.get("last_learn")

        # Provider-Gesundheit betrifft alle Rollen -> als globaler Beleg mitgeben
        ev["_providers"] = {
            "rate_limited": [f"{m.get('provider')}/{m.get('model')}"
                             for m in (health.get("rate_limited") or [])][:6],
            "errors": [f"{m.get('provider')}/{m.get('model')}: {_short(m.get('detail'), 90)}"
                       for m in (health.get("errors") or [])][:4],
            "fallback_active": bool(health.get("fallback_active")),
            "keys": ai_providers.available_providers(),
        }
        return ev

    # ---------------- Prüflauf ----------------
    async def run_review(self, manual: bool = True) -> Dict:
        if self.running_now:
            return {"status": "busy", "detail": "Team-Prüfung läuft bereits"}
        if not self.engine or self.db is None:
            return {"status": "error", "detail": "Engine nicht initialisiert"}
        if not self.engine.key:
            return {"status": "error", "detail": "Kein API-Key für das Haupt-Modell"}
        from services import ai_providers
        self.running_now = True
        try:
            evidence = await self.collect_evidence()
            providers = evidence.pop("_providers", {})
            catalogue = "\n".join(
                f"- {prov}: {', '.join(models)}"
                for prov, models in ai_providers.ALLOWED_MODELS.items()
                if providers.get("keys", {}).get(prov))
            prompt = (
                f"Zeitpunkt: {_now_iso()}\n\n"
                "=== ERLAUBTER MODELL-KATALOG (nur Provider mit gültigem API-Key) ===\n"
                f"{catalogue or '(keine Provider mit Key)'}\n\n"
                "=== PROVIDER-GESUNDHEIT ===\n"
                f"Rate-Limits: {', '.join(providers.get('rate_limited') or []) or 'keine'}\n"
                f"Fehler: {'; '.join(providers.get('errors') or []) or 'keine'}\n"
                f"Fallback aktiv: {'ja' if providers.get('fallback_active') else 'nein'}\n\n"
                "=== BELEGE JE ROLLE (Stichproben) ===\n"
                f"{evidence_text(evidence)}\n\n"
                "Bewerte jetzt JEDE Rolle einzeln und gib das JSON zurück."
            )
            text, provider, model = await self.engine.generate_for_role(
                "supervisor", prompt, SUPERVISOR_SYSTEM, temperature=0.2)
            data = self.engine._parse_json(text)
            report = normalize_report(data, ai_providers.ALLOWED_MODELS)
            report.update({
                "id": str(uuid.uuid4()),
                "ts": _now_iso(),
                "model": f"{provider}/{model}",
                "trigger": "manual" if manual else "auto",
                "checked_roles": len(report["roles"]),
            })
            report["switches"] = await self._auto_switch(report["roles"])
            await self.db.settings.update_one({"_id": DOC_ID}, {"$set": dict(report)},
                                              upsert=True)
            try:
                await self.db[HIST_COLL].insert_one(dict(report))
            except Exception as e:
                logger.warning(f"Supervisor-Historie nicht gespeichert: {e}")
            self.report = report
            self.last_error = None
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "supervisor",
                "text": report["summary"],
                "roles": report["roles"],
                "recommendations": report["recommendations"],
                "model": report["model"], "ts": report["ts"],
            })
            try:
                from services.ai_memory import memory
                await memory.remember(
                    "research_insight", "Aufsicht über das KI-Team",
                    report["summary"], meta={"roles": report["roles"]},
                    tags=["team", "supervision"], weight=2, source="supervisor")
            except Exception as e:
                logger.debug(f"Supervisor-Memory: {e}")
            logger.info(f"KI-Team-Prüfung fertig ({report['model']}): "
                        f"{len(report['roles'])} Rollen bewertet")
            return {"status": "ok", "report": report}
        except Exception as e:
            self.last_error = str(e)[:250]
            logger.error(f"KI-Team-Prüfung fehlgeschlagen: {e}")
            return {"status": "error", "detail": self.last_error}
        finally:
            self.running_now = False

    async def start_review(self, manual: bool = True) -> Dict:
        """Prüflauf im Hintergrund starten (dauert je Modell >60s – der Client
        pollt anschliessend `GET /api/ai/supervisor`)."""
        if self.running_now:
            return {"status": "busy", "detail": "Team-Prüfung läuft bereits"}
        if not self.engine or self.db is None:
            return {"status": "error", "detail": "Engine nicht initialisiert"}
        if not self.engine.key:
            return {"status": "error", "detail": "Kein API-Key für das Haupt-Modell"}
        self.running_now = True   # sofort sperren, damit Doppelklicks nichts starten

        async def _run():
            try:
                self.running_now = False   # run_review setzt die Sperre selbst
                await self.run_review(manual=manual)
            except Exception as e:
                self.last_error = str(e)[:250]
                self.running_now = False

        asyncio.create_task(_run())
        return {"status": "started"}

    def status(self) -> Dict:
        return {"report": self.report, "running": self.running_now,
                "last_error": self.last_error, "roles": list(SUPERVISED_ROLES),
                "settings": dict(self.settings), "last_switches": self.last_switches}


supervisor = TeamSupervisor()
