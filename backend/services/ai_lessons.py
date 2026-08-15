"""Lektionen-Speicher des KI Traders (settings/ai_lessons.lessons).

Bisher konnte nur die KI selbst Lektionen schreiben. Jetzt gilt:
  * Der Trader kann Lektionen anlegen, bearbeiten (Stift) und löschen.
  * Vom Trader angelegte/bearbeitete Lektionen sind `locked` – die KI darf sie
    weder überschreiben noch verwerfen; sie sieht die Markierung im Prompt und
    weiß dadurch, dass der Trader eingegriffen hat.
  * Lektionen, die dem MasterPrompt widersprechen, werden nicht gespeichert.

Alle Funktionen sind bewusst schlank und ohne Seiteneffekte auf den Engine-State,
damit sie sowohl vom Lernlauf (`services/ai_learning.py`) als auch von den
Endpunkten (`routers/ai_governance.py`) genutzt werden können.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DOC_ID = "ai_lessons"
WEIGHT_LABELS = {1: "basis", 2: "mittel", 3: "hoch", 4: "sehr hoch"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(lesson: Dict) -> Dict:
    """Lektion auf das aktuelle Schema bringen (Altbestand hat keine id/origin)."""
    out = dict(lesson or {})
    out.setdefault("id", f"les_{uuid.uuid4().hex[:10]}")
    out["title"] = str(out.get("title", ""))[:120]
    out["detail"] = str(out.get("detail", ""))[:600]
    try:
        out["weight"] = int(out.get("weight", 2) or 2)
    except (TypeError, ValueError):
        out["weight"] = 2
    out.setdefault("weight_label", WEIGHT_LABELS.get(out["weight"], "mittel"))
    out.setdefault("origin", "ai")
    out["locked"] = bool(out.get("locked"))
    out.setdefault("updated_at", _now_iso())
    return out


def normalize_all(lessons) -> List[Dict]:
    return [normalize(l) for l in (lessons or []) if isinstance(l, dict) and l.get("title")]


_STOPWORDS = {
    "der", "die", "das", "und", "oder", "im", "in", "bei", "beim", "mit",
    "von", "für", "auf", "zu", "zur", "zum", "den", "dem", "des", "ein",
    "eine", "einer", "einem", "am", "an", "als", "ist", "sind", "wird",
    "werden", "über", "unter", "nach", "vor", "aus", "bis", "es", "sich",
    "auch", "noch", "sehr", "wenn", "dann", "wie", "the", "and", "of",
}


def _tokens(text: str):
    import re
    return {w for w in re.findall(r"[a-zä-üß0-9]+", str(text).lower())
            if w not in _STOPWORDS}


def _similar(a: Dict, b: Dict) -> bool:
    """Nahezu identische Titel erkennen (Wort-Überschneidung)."""
    ta, tb = _tokens(a.get("title", "")), _tokens(b.get("title", ""))
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    if inter < 2:
        return False
    if inter / len(ta | tb) >= 0.65:
        return True
    # Titel-Enthaltensein: 'SOL Shorts meiden' vs 'SOL Shorts meiden im Uptrend'
    return inter == min(len(ta), len(tb)) and inter >= 3


def _validation_rank(l: Dict):
    """Sortierschlüssel: am besten validiert zuerst
    (vom Trader gesperrt > Bestätigungen > Modell-Gewicht > Aktualität)."""
    return (1 if l.get("locked") else 0,
            int(l.get("confirmations", 0) or 0),
            int(l.get("weight", 2) or 2),
            str(l.get("updated_at", "")))


def dedupe_lessons(lessons: List[Dict]):
    """Doppelte/nahezu identische Lektionen zusammenführen.

    Bug-Report: manche Lektionen waren doppelt bzw. widersprachen sich.
    Es bleibt nur die am besten VALIDIERTE Version (locked > Bestätigungen >
    Gewicht > Aktualität); vom Trader gesperrte Lektionen werden nie entfernt.
    Rückgabe: (bereinigte Liste, Liste der entfernten Duplikate)."""
    ordered = sorted(normalize_all(lessons), key=_validation_rank, reverse=True)
    kept: List[Dict] = []
    dropped: List[Dict] = []
    for l in ordered:
        dup = next((k for k in kept if _similar(k, l)), None)
        if dup is None or l.get("locked"):
            kept.append(l)
        else:
            dropped.append({"title": l.get("title"), "kept": dup.get("title")})
    return kept, dropped


# --------------------------------------------------------------------------- #
#  Themen-Konsolidierung: widersprüchliche Lektionen zum GLEICHEN Parameter
#  (z.B. Auto-Leverage 200x vs. fester Hebel 10; Break-Even 30/35/40%).
#  Regel: Die NEUESTE vom Trader festgelegte (locked) Lektion eines Themas
#  gilt – ältere zum selben Thema werden als `superseded` markiert: sie
#  bleiben gespeichert, fließen aber NICHT mehr in den Prompt ein.
# --------------------------------------------------------------------------- #
TOPIC_KEYWORDS = {
    "leverage": ("auto-leverage", "auto_leverage", "auto_lev", "hebel", "leverage"),
    "break_even": ("break-even", "breakeven", "be_trigger", "be_mode"),
    "cooldown": ("cooldown",),
    "news_pause": ("makro-news", "handelsverbot", "news-events"),
}


def lesson_topics(lesson: Dict) -> set:
    """Themen einer Lektion – bewusst nur über den TITEL, damit beiläufige
    Erwähnungen im Detail-Text keine falschen Konflikte erzeugen."""
    title = str(lesson.get("title", "")).lower()
    return {t for t, kws in TOPIC_KEYWORDS.items() if any(k in title for k in kws)}


def _recency(l: Dict) -> str:
    return str(l.get("user_edited_at") or l.get("updated_at") or "")


def consolidate_conflicts(lessons: List[Dict]):
    """Widersprüchliche Lektionen pro Thema auflösen (neueste Trader-Anweisung
    gewinnt). Rückgabe: (Lektionen mit superseded-Flags, Konflikt-Liste)."""
    out = [dict(l) for l in normalize_all(lessons)]
    for l in out:
        l.pop("superseded", None)
        l.pop("superseded_by", None)
    by_topic: Dict[str, List[Dict]] = {}
    for l in out:
        for t in lesson_topics(l):
            by_topic.setdefault(t, []).append(l)
    plan = []
    winner_ids = set()
    for topic, group in sorted(by_topic.items()):
        locked = [l for l in group if l.get("locked")]
        if not locked or len(group) < 2:
            continue  # ohne Trader-Anweisung regelt weiterhin dedupe_lessons
        winner = max(locked, key=_recency)
        winner_ids.add(winner["id"])
        plan.append((topic, winner, group))
    conflicts = []
    for topic, winner, group in plan:
        losers = [l for l in group
                  if l["id"] != winner["id"] and l["id"] not in winner_ids]
        if not losers:
            continue
        for l in losers:
            l["superseded"] = True
            l["superseded_by"] = winner["id"]
        conflicts.append({
            "topic": topic,
            "active": {"id": winner["id"], "title": winner.get("title"),
                       "updated_at": _recency(winner)},
            "superseded": [{"id": l["id"], "title": l.get("title"),
                            "updated_at": _recency(l)} for l in losers],
        })
    return out, conflicts


def active_lessons(lessons: List[Dict]) -> List[Dict]:
    """Nur die aktuell gültigen Lektionen (ohne superseded)."""
    consolidated, _ = consolidate_conflicts(lessons)
    return [l for l in consolidated if not l.get("superseded")]


def merge_lessons(old: List[Dict], new: List[Dict], removed: List[str],
                  max_lessons: int) -> List[Dict]:
    """Lernlauf-Ergebnis mit dem Wissensstand zusammenführen.

    Bestehende Lektionen bleiben erhalten, solange die KI sie nicht ausdrücklich
    verwirft. NEU: `locked` (vom Trader angelegte/bearbeitete) Lektionen sind
    unantastbar – sie können von der KI weder ersetzt noch entfernt werden und
    zählen nicht gegen das Limit weg.
    """
    old_n = normalize_all(old)
    locked = [l for l in old_n if l.get("locked")]
    locked_titles = {l["title"].strip().lower() for l in locked}
    drop = {str(t).strip().lower() for t in (removed or []) if str(t).strip()}
    drop -= locked_titles

    merged: List[Dict] = []
    seen = set(locked_titles)
    for lesson in normalize_all(new) + old_n:
        if lesson.get("locked"):
            continue
        key = lesson["title"].strip().lower()
        if not key or key in seen or key in drop:
            continue
        seen.add(key)
        merged.append(lesson)
    merged.sort(key=lambda l: (int(l.get("weight", 2) or 2), str(l.get("updated_at", ""))),
                reverse=True)
    deduped, dropped = dedupe_lessons(locked + merged)
    if dropped:
        logger.info("Lektionen-Dedupe: " + "; ".join(
            f"'{d['title']}' entfernt (behalten: '{d['kept']}')" for d in dropped[:5]))
    locked_out = [l for l in deduped if l.get("locked")]
    ai_out = [l for l in deduped if not l.get("locked")]
    limit = max(1, int(max_lessons))
    return locked_out + ai_out[:limit]


def prompt_order(lessons: List[Dict]) -> List[Dict]:
    """Einheitliche Reihenfolge (wie im KI-Prompt): locked zuerst, dann Gewicht.
    Vergibt fortlaufende Nummern (`no`) für aktive Lektionen – dieselben
    Nummern, von denen die KI spricht ('Lektion 6')."""
    ordered = sorted(lessons,
                     key=lambda l: (1 if l.get("locked") else 0, int(l.get("weight", 2))),
                     reverse=True)
    for i, l in enumerate(ordered):
        l["no"] = i + 1
    return ordered


def lessons_text(lessons: List[Dict]) -> str:
    """Prompt-Block: Lektionen inkl. Herkunfts-Markierung.

    Widersprüchliche Lektionen zum gleichen Thema werden vorher konsolidiert –
    nur die jeweils NEUESTE Trader-Anweisung fließt in den Prompt ein."""
    lessons = active_lessons(lessons)
    if not lessons:
        return "(noch keine Lektionen – zu wenige abgeschlossene Ergebnisse)"
    ordered = prompt_order(normalize_all(lessons))
    out = []
    for l in ordered:
        label = WEIGHT_LABELS.get(int(l.get("weight", 2)), "mittel")
        if l.get("locked"):
            mark = ("[VOM TRADER FESTGELEGT/ANGEPASST – unveränderlich, befolgen]"
                    if l.get("origin") != "user" else "[VOM TRADER SELBST GESCHRIEBEN – befolgen]")
        else:
            mark = f"[Gewicht: {label}]"
        out.append(f"{l['no']}. {mark} {l.get('title')}: {l.get('detail')}")
    return "\n".join(out)


class LessonStore:
    """CRUD auf settings/ai_lessons – gemeinsame Quelle für UI und Lernlauf."""

    def __init__(self):
        self.db = None

    def setup(self, db):
        self.db = db

    async def _doc(self) -> Dict:
        return await self.db.settings.find_one({"_id": DOC_ID}) or {}

    async def all(self) -> List[Dict]:
        """Alle Lektionen – Themen-Konflikte werden immer on-the-fly
        konsolidiert; aktive Lektionen tragen dieselbe Nummer (`no`) wie im
        KI-Prompt, superseded Lektionen haben keine Nummer."""
        lessons = normalize_all((await self._doc()).get("lessons"))
        consolidated, _ = consolidate_conflicts(lessons)
        prompt_order([l for l in consolidated if not l.get("superseded")])
        for l in consolidated:
            l.setdefault("no", None)
        return consolidated

    async def save_all(self, lessons: List[Dict]) -> List[Dict]:
        lessons = normalize_all(lessons)
        await self.db.settings.update_one(
            {"_id": DOC_ID}, {"$set": {"lessons": lessons, "updated_at": _now_iso()}},
            upsert=True)
        return lessons

    async def create(self, title: str, detail: str, weight: int = 3) -> Dict:
        lesson = normalize({
            "id": f"les_{uuid.uuid4().hex[:10]}", "title": title, "detail": detail,
            "weight": max(1, min(4, int(weight or 3))), "origin": "user", "locked": True,
            "updated_at": _now_iso(), "user_edited_at": _now_iso(),
        })
        lessons = await self.all()
        lessons.insert(0, lesson)
        await self.save_all(lessons)
        return lesson

    async def update(self, lesson_id: str, fields: Dict) -> Optional[Dict]:
        lessons = await self.all()
        found = None
        for l in lessons:
            if l.get("id") != lesson_id:
                continue
            if "title" in fields and str(fields["title"]).strip():
                l["title"] = str(fields["title"])[:120]
            if "detail" in fields:
                l["detail"] = str(fields["detail"])[:600]
            if "weight" in fields:
                try:
                    l["weight"] = max(1, min(4, int(fields["weight"])))
                    l["weight_label"] = WEIGHT_LABELS.get(l["weight"], "mittel")
                except (TypeError, ValueError):
                    pass
            if "locked" in fields:
                l["locked"] = bool(fields["locked"])
            else:
                l["locked"] = True
            l["origin"] = "user_edited" if l.get("origin") == "ai" else l.get("origin", "user")
            l["user_edited_at"] = _now_iso()
            l["updated_at"] = _now_iso()
            found = l
            break
        if not found:
            return None
        await self.save_all(lessons)
        return found

    async def delete(self, lesson_id: str) -> bool:
        lessons = await self.all()
        rest = [l for l in lessons if l.get("id") != lesson_id]
        if len(rest) == len(lessons):
            return False
        await self.save_all(rest)
        return True

    async def conflicts(self, persist: bool = True) -> Dict:
        """Themen-Konflikte (z.B. Auto-Lev vs. fester Hebel) erkennen und die
        superseded-Flags optional persistieren. Für GET /api/ai/lessons/conflicts."""
        lessons = await self.all()
        consolidated, conflicts = consolidate_conflicts(lessons)
        if persist and conflicts:
            await self.save_all(consolidated)
        return {"conflicts": conflicts,
                "active": sum(1 for l in consolidated if not l.get("superseded")),
                "superseded": sum(1 for l in consolidated if l.get("superseded"))}

    async def audit_against_master(self) -> Dict:
        """ALLE gespeicherten Lektionen gegen den MasterPrompt prüfen.

        Der MasterPrompt steht über allem (Vorgabe des Traders): Lektionen,
        die ihm widersprechen, werden GELÖSCHT – auch gesperrte/vom Trader
        angelegte (der Trader hat den MasterPrompt bewusst geändert).
        Wird nach jeder MasterPrompt-Änderung und vor jedem Lernlauf ausgeführt."""
        from services.ai_master_prompt import master_prompt
        lessons = await self.all()
        kept, removed = [], []
        for l in lessons:
            ok, why = master_prompt.check_lesson(l.get("title", ""), l.get("detail", ""))
            if ok:
                kept.append(l)
            else:
                removed.append({"id": l.get("id"), "title": l.get("title"),
                                "locked": bool(l.get("locked")), "why": why})
        # Zusätzlich: Doppelungen bereinigen – nur die am besten validierte
        # Version einer Lektion bleibt (Bug-Report: doppelte/widersprüchliche
        # Lektionen im Bestand).
        kept, dropped = dedupe_lessons(kept)
        for d in dropped:
            removed.append({"id": None, "title": d["title"], "locked": False,
                            "why": f"Doppelung – besser validierte Version bleibt: "
                                   f"'{d['kept']}'"})
        if removed:
            await self.save_all(kept)
            logger.info(f"Lektionen-Audit: {len(removed)} Lektionen entfernt "
                        f"(MasterPrompt-Verstoß oder Doppelung): "
                        f"{[r['title'] for r in removed]}")
        return {"checked": len(lessons), "removed": removed}


lesson_store = LessonStore()
