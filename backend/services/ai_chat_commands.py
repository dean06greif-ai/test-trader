"""Chat-Kommandos des KI Traders: Anweisungen im Chat REAL ausführen.

Problem vorher: der Chat war reine Text-Generierung. Sagte der Trader
"schließe die Paper-Positionen", hat die KI das nur behauptet – auf der
Website blieb alles offen. Jetzt gilt:

  * Vor der Chat-Antwort extrahiert ein kleiner LLM-Lauf ausführbare
    Anweisungen (Positionen schließen, Trade anpassen, Lektionen
    anlegen/ändern/löschen, Einstellungen ändern, Trade eröffnen).
  * Die Aktionen laufen über die BESTEHENDEN Sicherheits-/Audit-Wege
    (`ai_trade_manager`, `lesson_store`, `_handle_config_changes`) mit
    source="user" – Trader-Anweisungen gelten sofort, ohne Validierung.
  * Die echten Ergebnisse werden der KI in den Antwort-Kontext gegeben,
    damit sie exakt berichtet, was wirklich passiert ist.

Reine Hilfsfunktionen (Erkennung, Matching) sind testbar ohne DB/LLM.
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Grobe Vorerkennung: nur bei diesen Begriffen wird der Extraktions-Lauf gestartet
_COMMAND_PATTERN = re.compile(
    r"(schlie[ßs]|close|beende|lektion|lesson|l[öo]sch|entfern|delete|"
    r"[äa]nder|setze|passe\s+an|anpass|stell\s|stelle\s|"
    r"stop.?loss|\bsl\b|take.?profit|\btp1?\b|\btpf\b|hebel|leverage|margin|"
    r"\bcrv\b|konfidenz|confidence|cooldown|einstellung|"
    r"er[öo]ffne|kaufe|verkaufe|\blong\b|\bshort\b|teilverkauf|partial|"
    r"break.?even|gewinn\s*mitnehmen|absicher)",
    re.IGNORECASE)

EXTRACT_SYSTEM = (
    "Du bist der Kommando-Parser des 'KI Traders' einer Krypto-Daytrading-Plattform. "
    "Du bekommst eine Chat-Nachricht des Traders plus den aktuellen Zustand (offene Trades, "
    "Lektionen). Extrahiere NUR eindeutige, ausführbare Anweisungen – Fragen, Meinungen und "
    "vage Aussagen ergeben eine leere Liste. Erfinde nichts, rate keine IDs.\n"
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"commands": ['
    '{"type": "close_positions", "mode": "paper|live|all", "symbol": "BTCUSDT|ALL", '
    '"side": "LONG|SHORT|ALL"},\n'
    '{"type": "trade_action", "trade_id": "...", "symbol": "BTCUSDT", '
    '"action": "close|partial_close|adjust_sl|adjust_tp|add_margin|remove_margin|set_leverage", '
    '"value": 0, "pct": null, "target": "tp1|tpf"},\n'
    '{"type": "open_trade", "spec": {"symbol": "BTCUSDT", "side": "LONG", "sl_pct": 0.8, '
    '"tp1_pct": 1.2, "tpf_pct": 2.0, "leverage": 10, "capital_pct": 50, "confidence": 70, '
    '"reason": "..."}},\n'
    '{"type": "lesson_add", "title": "...", "detail": "...", "weight": 3},\n'
    '{"type": "lesson_update", "match": "Lektions-id oder Titel", "title": "...", "detail": "..."},\n'
    '{"type": "lesson_delete", "match": "Lektions-id oder Titel"},\n'
    '{"type": "config_change", "symbol": "ENGINE|BTCUSDT|cand_xxxx", '
    '"changes": {"sl_fixed_percent": 0.8}, "reason": "kurz"}\n'
    ']}\n'
    "Regeln: close_positions für pauschale Aufträge ('schließe alle Paper-Positionen'), "
    "trade_action für einen konkreten Trade (trade_id aus der Liste, sonst symbol). "
    "Bei Lektionen 'match' exakt aus der Lektionsliste übernehmen. "
    "Bei Einstellungen nur die erlaubten Keys verwenden. "
    "Keine Anweisung erkennbar => {\"commands\": []}."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def looks_like_command(text: str) -> bool:
    """Grobe Keyword-Vorerkennung (rein, testbar)."""
    return bool(_COMMAND_PATTERN.search(text or ""))


def match_lesson(lessons: List[Dict], needle: str) -> Optional[Dict]:
    """Lektion per id, exaktem Titel oder eindeutigem Teil-Titel finden (rein, testbar)."""
    needle = str(needle or "").strip().lower()
    if not needle:
        return None
    for l in lessons:
        if str(l.get("id", "")).lower() == needle:
            return l
    for l in lessons:
        if str(l.get("title", "")).strip().lower() == needle:
            return l
    part = [l for l in lessons if needle in str(l.get("title", "")).lower()]
    return part[0] if len(part) == 1 else None


def match_trades(trades: List[Dict], mode: str, symbol: str, side: str) -> List[Dict]:
    """Offene Trades nach Modus/Symbol/Richtung filtern (rein, testbar)."""
    mode = str(mode or "all").lower()
    symbol = str(symbol or "ALL").upper()
    side = str(side or "ALL").upper()
    out = []
    for t in trades:
        if mode in ("paper", "live") and t.get("mode") != mode:
            continue
        if symbol not in ("ALL", "") and str(t.get("symbol", "")).upper() != symbol:
            continue
        if side in ("LONG", "SHORT") and str(t.get("side", "")).upper() != side:
            continue
        out.append(t)
    return out


class ChatCommandExecutor:
    """Extrahiert und führt Trader-Anweisungen aus dem Chat aus."""

    async def run(self, engine, text: str) -> Optional[Dict]:
        """Gibt None zurück, wenn keine Anweisung erkannt wurde. Sonst
        {"results": [...], "results_text": "..."} mit den ECHTEN Ergebnissen."""
        if not looks_like_command(text):
            return None
        commands = await self._extract(engine, text)
        if not commands:
            return None
        results: List[Dict] = []
        for cmd in commands[:10]:
            if not isinstance(cmd, dict):
                continue
            try:
                res = await self._execute(engine, cmd, text)
            except Exception as e:
                logger.error(f"Chat-Kommando fehlgeschlagen ({cmd.get('type')}): {e}")
                res = [{"ok": False, "what": str(cmd.get("type")), "detail": str(e)[:200]}]
            results.extend(res or [])
        if not results:
            return None
        lines = [("✅" if r.get("ok") else "❌") + f" {r.get('what')}"
                 + (f" – {r.get('detail')}" if r.get("detail") else "")
                 for r in results]
        results_text = "\n".join(lines)
        try:
            await engine.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "action",
                "text": "Ausgeführte Trader-Anweisungen:\n" + results_text,
                "results": results, "ts": _now_iso()})
        except Exception:
            pass
        return {"results": results, "results_text": results_text}

    # ---------------- Extraktion ----------------
    async def _extract(self, engine, text: str) -> List[Dict]:
        from services.ai_knowledge import tunable_spec_text
        from services.ai_lessons import lesson_store
        trades = await engine.db.auto_trades.find({"status": "open"}).limit(200).to_list(200)
        trade_lines = "\n".join(
            f"- id={t.get('id')} {t.get('symbol')} {t.get('side')} "
            f"[{t.get('mode')}/{t.get('strategy_name') or t.get('strategy_id') or '?'}] "
            f"Entry {t.get('entry')} SL {t.get('sl')} TP1 {t.get('tp1')} TPf {t.get('tpf')} "
            f"Hebel {t.get('leverage')}x Qty {t.get('qty_remaining', t.get('qty'))}/{t.get('qty')}"
            for t in trades) or "(keine offenen Trades)"
        lessons = await lesson_store.all()
        lesson_lines = "\n".join(
            f"- id={l.get('id')} „{l.get('title')}“" for l in lessons) or "(keine Lektionen)"
        prompt = (
            f"=== OFFENE TRADES ===\n{trade_lines}\n\n"
            f"=== GESPEICHERTE LEKTIONEN ===\n{lesson_lines}\n\n"
            f"=== ERLAUBTE EINSTELLUNGS-KEYS ===\n{tunable_spec_text()}\n\n"
            f"=== NACHRICHT DES TRADERS ===\n{text}\n\n"
            "Extrahiere die ausführbaren Anweisungen."
        )
        try:
            raw, _prov, _model = await engine.generate_for_role(
                "chat", prompt, EXTRACT_SYSTEM, temperature=0.1)
            data = engine._parse_json(raw)
        except Exception as e:
            logger.warning(f"Chat-Kommando-Extraktion fehlgeschlagen: {e}")
            return []
        cmds = data.get("commands")
        return [c for c in cmds if isinstance(c, dict)] if isinstance(cmds, list) else []

    # ---------------- Ausführung ----------------
    async def _execute(self, engine, cmd: Dict, user_text: str) -> List[Dict]:
        from services.ai_trade_manager import trade_manager
        ctype = str(cmd.get("type") or "")
        reason = f"Trader-Anweisung im Chat: {user_text[:150]}"

        if ctype == "close_positions":
            open_trades = await engine.db.auto_trades.find({"status": "open"}).to_list(100)
            targets = match_trades(open_trades, cmd.get("mode"), cmd.get("symbol"),
                                   cmd.get("side"))
            if not targets:
                return [{"ok": False, "what": "Positionen schließen",
                         "detail": "Keine passende offene Position gefunden"}]
            out = []
            for t in targets:
                res = await trade_manager.apply_action(
                    t["id"], "close", reason=reason, source="user", enforce_limits=False)
                still_open = await engine.db.auto_trades.find_one(
                    {"id": t["id"], "status": "open"})
                ok = res.get("status") == "ok" and not still_open
                out.append({"ok": ok,
                            "what": f"CLOSE {t.get('symbol')} {t.get('side')} ({t.get('mode')})",
                            "detail": res.get("detail") if not ok else
                            f"geschlossen, PnL im Trade-Verlauf"})
            return out

        if ctype == "trade_action":
            trade = None
            tid = str(cmd.get("trade_id") or "").strip()
            if tid:
                trade = await engine.db.auto_trades.find_one({"id": tid, "status": "open"})
            if not trade and cmd.get("symbol"):
                sym = str(cmd["symbol"]).upper()
                cands = await engine.db.auto_trades.find(
                    {"status": "open", "symbol": sym}).to_list(10)
                trade = cands[0] if len(cands) == 1 else None
                if not trade and cands:
                    return [{"ok": False, "what": f"Trade-Aktion {sym}",
                             "detail": f"{len(cands)} offene Trades auf {sym} – bitte eindeutig benennen"}]
            if not trade:
                return [{"ok": False, "what": "Trade-Aktion",
                         "detail": "Offener Trade nicht gefunden"}]
            action = str(cmd.get("action") or "hold")
            res = await trade_manager.apply_action(
                trade["id"], action, value=cmd.get("value"), pct=cmd.get("pct"),
                target=str(cmd.get("target") or "tp1"), reason=reason,
                source="user", enforce_limits=False)
            return [{"ok": res.get("status") == "ok",
                     "what": f"{action.upper()} {trade.get('symbol')} {trade.get('side')} ({trade.get('mode')})",
                     "detail": res.get("detail")}]

        if ctype == "open_trade":
            res = await trade_manager.open_trade(dict(cmd.get("spec") or {}), source="user")
            spec = cmd.get("spec") or {}
            return [{"ok": res.get("status") == "ok",
                     "what": f"OPEN {spec.get('side')} {spec.get('symbol')}",
                     "detail": res.get("detail") or
                     (f"Entry {res.get('entry')}, SL {res.get('sl')}, TP1 {res.get('tp1')}"
                      if res.get("status") == "ok" else None)}]

        if ctype in ("lesson_add", "lesson_update", "lesson_delete"):
            return [await self._lesson_cmd(engine, ctype, cmd)]

        if ctype == "config_change":
            props = await engine._handle_config_changes(
                [{"symbol": cmd.get("symbol"), "changes": cmd.get("changes") or {},
                  "reason": reason}], source="user")
            if not props:
                return [{"ok": False, "what": "Einstellungs-Änderung",
                         "detail": "Kein gültiger/geänderter Wert (Whitelist prüfen)"}]
            out = []
            for p in props:
                ok = p.get("status") == "auto_applied"
                out.append({"ok": ok,
                            "what": f"EINSTELLUNG {p.get('symbol')}: "
                            + ", ".join(f"{k}={v}" for k, v in (p.get('changes') or {}).items()),
                            "detail": p.get("error") or (None if ok else p.get("status"))})
            return out

        return []

    async def _lesson_cmd(self, engine, ctype: str, cmd: Dict) -> Dict:
        from services.ai_lessons import lesson_store
        if ctype == "lesson_add":
            title = str(cmd.get("title") or "").strip()
            detail = str(cmd.get("detail") or "").strip()
            if not title or not detail:
                return {"ok": False, "what": "Lektion anlegen",
                        "detail": "Titel/Inhalt fehlt"}
            lesson = await lesson_store.create(title, detail,
                                               weight=int(cmd.get("weight") or 3))
            if engine.learning:
                engine.learning.invalidate_lessons()
            return {"ok": True, "what": f"LEKTION angelegt: „{lesson['title']}“",
                    "detail": "sofort aktiv, für die KI unveränderlich, im Lernen-Reiter sichtbar"}
        lessons = await lesson_store.all()
        target = match_lesson(lessons, cmd.get("match"))
        if not target:
            return {"ok": False,
                    "what": f"Lektion „{cmd.get('match')}“",
                    "detail": "Nicht eindeutig gefunden – Titel exakt angeben"}
        if ctype == "lesson_delete":
            await lesson_store.delete(target["id"])
            if engine.learning:
                engine.learning.invalidate_lessons()
            return {"ok": True, "what": f"LEKTION gelöscht: „{target['title']}“", "detail": None}
        fields = {}
        if str(cmd.get("title") or "").strip():
            fields["title"] = cmd["title"]
        if str(cmd.get("detail") or "").strip():
            fields["detail"] = cmd["detail"]
        if not fields:
            return {"ok": False, "what": f"Lektion „{target['title']}“",
                    "detail": "Keine Änderung angegeben"}
        updated = await lesson_store.update(target["id"], fields)
        if engine.learning:
            engine.learning.invalidate_lessons()
        return {"ok": bool(updated), "what": f"LEKTION geändert: „{(updated or target)['title']}“",
                "detail": "sofort aktiv, für die KI unveränderlich"}


chat_commands = ChatCommandExecutor()
