"""KI-Gedächtnis – austauschbare Wissens-/Analyse-Speicherschicht.

Alle KI-Rollen legen ihre Erkenntnisse hier ab (Forschungs-Erkenntnisse,
ML-Befunde, Markt-Beobachtungen, Lektionen). Primärspeicher ist immer MongoDB
(`db.ai_knowledge`), damit die Plattform ohne Zusatzdienste voll funktioniert.
Ist eine Supabase-Instanz konfiguriert, wird jeder Eintrag zusätzlich dorthin
gespiegelt (Dual-Write) – als langlebiger, KI-eigener Wissensspeicher.

ENV:
  SUPABASE_URL                 z.B. https://xyz.supabase.co  (auch .../rest/v1 erlaubt)
  SUPABASE_SERVICE_ROLE_KEY    Service-/Secret-Key (Alternativen: SUPABASE_SECRET_KEY, SUPABASE_KEY)
  AI_MEMORY_TABLE              Tabellenname (Default: ai_knowledge)

Fehlen die Keys, arbeitet der Store transparent nur mit MongoDB weiter.
Schema für Supabase: backend/scripts/supabase_schema.sql
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Bekannte Wissensarten (frei erweiterbar – nur für Labels/Filter im UI).
KINDS = {
    "research_insight": "Forschungs-Erkenntnis (Backtest/Optimizer/Regime)",
    "ml_finding": "ML-Befund (Optuna/XGBoost)",
    "market_observation": "Markt-Beobachtung",
    "lesson": "Lektion aus echten Ergebnissen",
    "trade_action": "KI-Trade-Steuerung (Aktion an einem Trade)",
    "idea": "Neue Strategie-/Handels-Idee",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def supabase_config() -> Optional[Dict[str, str]]:
    """(base_url, key, table) oder None, wenn Supabase nicht konfiguriert ist."""
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
           or os.environ.get("SUPABASE_SECRET_KEY")
           or os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return None
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]
    return {"base": url, "key": key,
            "table": os.environ.get("AI_MEMORY_TABLE", "ai_knowledge")}


class MongoMemory:
    """Primärspeicher: MongoDB-Collection `ai_knowledge`."""
    name = "mongodb"

    def __init__(self, db):
        self.db = db

    async def insert(self, entry: Dict):
        await self.db.ai_knowledge.insert_one(dict(entry))

    async def query(self, kind: Optional[str], tags: Optional[List[str]], limit: int) -> List[Dict]:
        q: Dict = {}
        if kind:
            q["kind"] = kind
        if tags:
            q["tags"] = {"$in": list(tags)}
        rows = await self.db.ai_knowledge.find(q).sort("ts", -1).limit(limit).to_list(limit)
        for r in rows:
            r.pop("_id", None)
        return rows

    async def count(self, kind: Optional[str] = None) -> int:
        return await self.db.ai_knowledge.count_documents({"kind": kind} if kind else {})

    async def prune(self, keep: int) -> int:
        total = await self.count()
        if total <= keep:
            return 0
        old = await self.db.ai_knowledge.find().sort("ts", 1).limit(total - keep).to_list(total)
        ids = [o.get("id") for o in old if o.get("id")]
        if not ids:
            return 0
        res = await self.db.ai_knowledge.delete_many({"id": {"$in": ids}})
        return res.deleted_count


class SupabaseMemory:
    """Spiegel-Speicher: Supabase (PostgREST). Best-effort, blockiert nie."""
    name = "supabase"

    def __init__(self, cfg: Dict[str, str]):
        self.base = cfg["base"]
        self.key = cfg["key"]
        self.table = cfg["table"]
        self.last_error: Optional[str] = None
        self.writes = 0

    @property
    def _url(self) -> str:
        return f"{self.base}/rest/v1/{self.table}"

    def _headers(self, prefer: Optional[str] = None) -> Dict[str, str]:
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
             "Content-Type": "application/json"}
        if prefer:
            h["Prefer"] = prefer
        return h

    async def insert(self, entry: Dict):
        import aiohttp
        payload = {k: v for k, v in entry.items() if k != "_id"}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(self._url, json=payload,
                              headers=self._headers("return=minimal")) as r:
                if r.status >= 300:
                    raise RuntimeError(f"Supabase {r.status}: {(await r.text())[:200]}")
        self.writes += 1
        self.last_error = None

    async def query(self, kind: Optional[str], tags: Optional[List[str]], limit: int) -> List[Dict]:
        import aiohttp
        params = {"order": "ts.desc", "limit": str(limit), "select": "*"}
        if kind:
            params["kind"] = f"eq.{kind}"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(self._url, params=params, headers=self._headers()) as r:
                if r.status >= 300:
                    raise RuntimeError(f"Supabase {r.status}: {(await r.text())[:200]}")
                return await r.json()

    async def ping(self) -> bool:
        try:
            await self.query(None, None, 1)
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)[:200]
            return False


class KnowledgeStore:
    """Fassade über die Speicher-Backends. Mongo = Wahrheit, Supabase = Spiegel."""

    MAX_ENTRIES = 4000

    def __init__(self):
        self.db = None
        self.primary: Optional[MongoMemory] = None
        self.mirror: Optional[SupabaseMemory] = None
        self.mirror_errors = 0
        self._pruned_at = 0.0

    def setup(self, db):
        self.db = db
        self.primary = MongoMemory(db)
        cfg = supabase_config()
        self.mirror = SupabaseMemory(cfg) if cfg else None
        if self.mirror:
            logger.info(f"KI-Gedächtnis: Supabase-Spiegel aktiv ({self.mirror.table})")
        else:
            logger.info("KI-Gedächtnis: nur MongoDB (kein SUPABASE_URL/KEY gesetzt)")

    # ---------------- write ----------------
    async def remember(self, kind: str, title: str, content: str,
                       meta: Optional[Dict] = None, tags: Optional[List[str]] = None,
                       weight: int = 2, source: str = "ai") -> Dict:
        entry = {
            "id": str(uuid.uuid4()),
            "kind": str(kind),
            "title": str(title)[:200],
            "content": str(content)[:6000],
            "meta": meta or {},
            "tags": [str(t)[:40] for t in (tags or [])][:12],
            "weight": int(max(1, min(3, weight))),
            "source": str(source)[:60],
            "ts": _now_iso(),
        }
        if self.primary:
            try:
                await self.primary.insert(entry)
            except Exception as e:
                logger.warning(f"KI-Gedächtnis (Mongo) Schreibfehler: {e}")
        if self.mirror:
            try:
                await self.mirror.insert(entry)
            except Exception as e:
                self.mirror_errors += 1
                self.mirror.last_error = str(e)[:200]
                logger.warning(f"KI-Gedächtnis (Supabase) Schreibfehler: {str(e)[:160]}")
        return entry

    async def remember_many(self, kind: str, items: List[Dict], source: str = "ai",
                            weight: int = 2, tags: Optional[List[str]] = None) -> int:
        n = 0
        for it in items or []:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            await self.remember(kind, it["title"], it.get("detail") or it.get("content") or "",
                                meta=it.get("meta") or {k: v for k, v in it.items()
                                                        if k not in ("title", "detail", "content")},
                                tags=(tags or []) + list(it.get("tags") or []),
                                weight=int(it.get("weight", weight) or weight), source=source)
            n += 1
        return n

    # ---------------- read ----------------
    async def recall(self, kind: Optional[str] = None, tags: Optional[List[str]] = None,
                     limit: int = 10) -> List[Dict]:
        if not self.primary:
            return []
        try:
            return await self.primary.query(kind, tags, max(1, min(200, limit)))
        except Exception as e:
            logger.warning(f"KI-Gedächtnis Lesefehler: {e}")
            return []

    async def context_text(self, kinds: Optional[List[str]] = None,
                           per_kind: int = 3, max_chars: int = 2500) -> str:
        """Kompakter Prompt-Block mit dem jüngsten Wissen je Art."""
        kinds = kinds or ["research_insight", "ml_finding", "idea"]
        blocks: List[str] = []
        for kind in kinds:
            rows = await self.recall(kind=kind, limit=per_kind)
            if not rows:
                continue
            label = KINDS.get(kind, kind)
            lines = [f"[{label}]"]
            for r in rows:
                lines.append(f"- {r.get('title')}: {str(r.get('content', ''))[:280]}")
            blocks.append("\n".join(lines))
        text = "\n".join(blocks)
        return text[:max_chars]

    async def stats(self) -> Dict:
        out: Dict = {"backend": "mongodb", "mirror": None, "total": 0, "by_kind": {}}
        if not self.primary:
            return out
        try:
            out["total"] = await self.primary.count()
            for k in KINDS:
                c = await self.primary.count(k)
                if c:
                    out["by_kind"][k] = c
        except Exception as e:
            out["error"] = str(e)[:150]
        if self.mirror:
            out["mirror"] = {"backend": "supabase", "table": self.mirror.table,
                             "writes": self.mirror.writes,
                             "errors": self.mirror_errors,
                             "last_error": self.mirror.last_error}
        return out

    async def health(self) -> Dict:
        st = await self.stats()
        if self.mirror:
            st["mirror"]["reachable"] = await self.mirror.ping()
            st["mirror"]["last_error"] = self.mirror.last_error
        return st

    async def housekeeping(self):
        """Hält die Mongo-Collection auf MAX_ENTRIES (Supabase bleibt Langzeit-Archiv)."""
        import time
        now = time.time()
        if now - self._pruned_at < 3600 or not self.primary:
            return
        self._pruned_at = now
        try:
            removed = await self.primary.prune(self.MAX_ENTRIES)
            if removed:
                logger.info(f"KI-Gedächtnis: {removed} alte Einträge lokal entfernt")
        except Exception as e:
            logger.warning(f"KI-Gedächtnis Housekeeping fehlgeschlagen: {e}")


memory = KnowledgeStore()
