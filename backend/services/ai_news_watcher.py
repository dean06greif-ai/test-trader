"""News-Wächter ("news_watcher"-Rolle des KI-Teams).

Überwacht 24/7 Krypto-News (RSS) + den Weltwirtschaftskalender
(macro_context.macro_calendar) und lässt ein leichtes LLM die Relevanz
bewerten. Bei hoher Markt-Relevanz:
  - Alert im KI-Chat (role="news_alert")
  - optional Sofort-Analyse der Haupt-Engine (auto_analysis)

Erkenntnisse werden in db.ai_news_events persistiert und fließen als
Kontext-Block in die regulären Analysen ein.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.news_feed import news_feed
from services import macro_context

logger = logging.getLogger(__name__)

WATCHER_SYSTEM = (
    "Du bist der 'News-Wächter' im KI-Team einer Krypto-Daytrading-Plattform. "
    "Du überwachst News-Schlagzeilen und den Weltwirtschaftskalender rund um die Uhr. "
    "Bewerte NUR die Markt-Relevanz für Krypto-Daytrading (BTC/ETH/SOL & Co.) – keine Trades. "
    "Sei streng: alert=true NUR bei Ereignissen mit klarem, kurzfristigem Markt-Einfluss "
    "(z.B. CPI/FOMC in <2h, Hack/Insolvenz einer großen Börse, ETF-Entscheidung, Regulierungs-Schock). "
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"alert": true|false, "severity": "low|medium|high", '
    '"summary": "1-3 Sätze auf Deutsch: was ist relevant und warum", '
    '"events": [{"title": "kurz", "impact": "low|medium|high", "affects": ["BTCUSDT"], '
    '"time_utc": "optional ISO oder leer"}]}'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NewsWatcher:
    def __init__(self):
        self.engine = None          # AIEngine (setup())
        self.running = False
        self.last_run: Optional[str] = None
        self.last_alert: Optional[Dict] = None
        self.last_error: Optional[str] = None
        self._next_due = 0.0
        self._seen_titles: set = set()

    def setup(self, engine):
        self.engine = engine

    @property
    def db(self):
        return self.engine.db if self.engine else None

    def _cfg(self) -> Dict:
        from services.ai_roles import role_manager
        return role_manager.role_cfg("news_watcher")

    async def _calendar_block(self) -> str:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                cal = await macro_context.macro_calendar(session)
        except Exception as e:
            logger.debug(f"news watcher calendar failed: {e}")
            return "(Wirtschaftskalender nicht erreichbar)"
        lines = []
        for w in (cal.get("no_trade_windows_utc") or [])[:5]:
            lines.append(f"⛔ HIGH-IMPACT: {w.get('event')} {w.get('start_utc')} → {w.get('end_utc')}")
        for u in (cal.get("upcoming") or [])[:8]:
            lines.append(f"- {u.get('event')} ({u.get('importance')}) {u.get('time_utc')}")
        return "\n".join(lines) or "(keine anstehenden Termine)"

    async def run_check(self, manual: bool = False) -> Dict:
        if not self.engine:
            return {"status": "error", "detail": "Engine nicht initialisiert"}
        headlines = await news_feed.get_headlines(20)
        fresh = [h for h in headlines if h.get("title") and h["title"] not in self._seen_titles]
        calendar_block = await self._calendar_block()
        if not fresh and not manual:
            self.last_run = _now_iso()
            return {"status": "ok", "detail": "keine neuen Schlagzeilen", "alert": False}

        news_block = "\n".join(f"- {h['title']} ({h['source']})" for h in (fresh or headlines)[:18]) \
            or "(keine News verfügbar)"
        prompt = (
            f"Zeit (UTC): {_now_iso()}\n\n"
            f"=== NEUE SCHLAGZEILEN ===\n{news_block}\n\n"
            f"=== WELTWIRTSCHAFTSKALENDER (nächste 48h, UTC) ===\n{calendar_block}\n\n"
            "Bewerte die Markt-Relevanz und antworte als JSON."
        )
        try:
            text, provider, model = await self.engine.generate_for_role(
                "news_watcher", prompt, WATCHER_SYSTEM, temperature=0.2)
            data = self.engine._parse_json(text)
        except Exception as e:
            self.last_error = str(e)[:200]
            logger.warning(f"news watcher LLM failed: {e}")
            return {"status": "error", "detail": self.last_error}

        for h in fresh:
            self._seen_titles.add(h["title"])
        if len(self._seen_titles) > 400:
            self._seen_titles = set(list(self._seen_titles)[-200:])

        alert = bool(data.get("alert"))
        severity = str(data.get("severity", "low")).lower()
        events = [e for e in (data.get("events") or []) if isinstance(e, dict)][:8]
        doc = {
            "id": str(uuid.uuid4()),
            "ts": _now_iso(),
            "alert": alert,
            "severity": severity if severity in ("low", "medium", "high") else "low",
            "summary": str(data.get("summary", ""))[:600],
            "events": [{"title": str(e.get("title", ""))[:160],
                        "impact": str(e.get("impact", "low")),
                        "affects": [str(a).upper() for a in (e.get("affects") or [])][:6],
                        "time_utc": str(e.get("time_utc", ""))[:32]} for e in events],
            "model": f"{provider}/{model}",
            "manual": manual,
        }
        try:
            await self.db.ai_news_events.insert_one(dict(doc))
        except Exception as e:
            logger.warning(f"news event persist failed: {e}")

        self.last_run = doc["ts"]
        self.last_error = None
        if alert and doc["severity"] in ("medium", "high"):
            self.last_alert = doc
            try:
                await self.db.ai_chat.insert_one({
                    "id": str(uuid.uuid4()), "role": "news_alert",
                    "text": doc["summary"], "severity": doc["severity"],
                    "events": doc["events"], "model": doc["model"], "ts": doc["ts"],
                })
            except Exception:
                pass
            if self._cfg().get("auto_analysis", True) and doc["severity"] == "high" \
                    and self.engine.config.get("enabled"):
                logger.info("news watcher: HIGH-Impact -> Sofort-Analyse ausgelöst")
                asyncio.create_task(self.engine.run_analysis(manual=False))
        return {"status": "ok", "alert": alert, "severity": doc["severity"],
                "summary": doc["summary"], "events": len(doc["events"])}

    async def latest_events(self, limit: int = 15) -> List[Dict]:
        if self.db is None:
            return []
        rows = await self.db.ai_news_events.find().sort("ts", -1).limit(limit).to_list(limit)
        for r in rows:
            r.pop("_id", None)
        return rows

    async def context_text(self, limit: int = 5) -> str:
        """Kompakter Block für die Analyse-Prompts der anderen Rollen."""
        rows = await self.latest_events(limit)
        relevant = [r for r in rows if r.get("alert") or r.get("severity") in ("medium", "high")]
        if not relevant:
            return ""
        lines = ["=== NEWS-WÄCHTER (24/7, relevante Ereignisse) ==="]
        for r in relevant[:limit]:
            lines.append(f"- [{str(r.get('ts', ''))[:16]}] ({r.get('severity')}) {r.get('summary', '')[:220]}")
            for e in (r.get("events") or [])[:3]:
                aff = ",".join(e.get("affects") or []) or "-"
                lines.append(f"    · {e.get('title')} [Impact {e.get('impact')}, betrifft {aff}]")
        return "\n".join(lines)

    def status(self) -> Dict:
        return {
            "running": self.running,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "last_alert": self.last_alert,
        }

    async def run_loop(self):
        self.running = True
        logger.info("News watcher loop started (24/7)")
        while self.running:
            await asyncio.sleep(10)
            try:
                cfg = self._cfg()
                if not cfg.get("enabled", True) or not self.engine or self.engine.db is None:
                    continue
                now = time.time()
                if now >= self._next_due:
                    interval = max(5, int(cfg.get("interval_min", 15))) * 60
                    self._next_due = now + interval
                    await self.run_check()
            except Exception as e:
                logger.error(f"news watcher loop error: {e}")


news_watcher = NewsWatcher()
