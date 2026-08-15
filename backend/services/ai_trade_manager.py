"""KI-Trade-Manager: die KI darf Trades eigenständig eröffnen und im laufenden
Trade steuern.

Erlaubte Aktionen (eine gemeinsame Quelle für KI **und** manuelle Bedienung):
  close          – Trade vorzeitig komplett schließen
  partial_close  – Teilmenge schließen (% der Restmenge)
  adjust_sl      – Stop-Loss verschieben (absolut oder % Abstand zum Kurs)
  adjust_tp      – TP1 / Final-TP verschieben
  add_margin     – Margin hinzufügen (Hebel sinkt, Liquidation rückt weg)
  remove_margin  – Margin entnehmen (Hebel steigt)
  set_leverage   – Hebel ändern, Positionsgröße bleibt erhalten
  hold           – bewusst nichts tun

Die eigentliche Ausführung liegt im AutoTrader (services/bitunix_trade.py) und
gilt identisch für Paper- und Live-Trades. Dieser Service ist die
Sicherheits-/Entscheidungsschicht darüber:
  * harte Limits (max. Aktionen pro Trade, Cooldown, Hebel-/Margin-Obergrenzen),
  * vollständiges Audit (`ai_trade_actions`, Trade-`events`, KI-Chat, Gedächtnis),
  * Kapital und Live/Paper-Modus bleiben für die KI unantastbar.
"""
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from services.ai_memory import memory
from services.ai_master_prompt import master_prompt

logger = logging.getLogger(__name__)

ACTIONS = ("hold", "close", "partial_close", "adjust_sl", "adjust_tp",
           "add_margin", "remove_margin", "set_leverage", "secure_profit")

DEFAULT_SETTINGS = {
    "enabled": True,
    "interval_min": 5,             # wie oft die KI offene Trades prüft
    "allow_open": True,            # darf die KI eigene Custom-Trades eröffnen?
    "allow_margin": True,          # dürfen Margin/Hebel angepasst werden?
    "max_actions_per_trade": 8,
    "cooldown_min": 3,             # Mindestabstand zwischen Aktionen je Trade
    "max_leverage": 50,            # Obergrenze für set_leverage / remove_margin
    "max_margin_add_pct": 100,     # max. Margin-Aufschlag in % der Start-Margin
    # Profit-Lock: bei Gewinn-Trades darf die KI Margin entnehmen (Hebel steigt),
    # um Kapital freizumachen – mit eigenen, höheren Grenzen.
    "profit_lock_enabled": True,
    "profit_lock_max_leverage": 100,   # Hebel-Obergrenze NUR für Gewinn-Trades
    "profit_lock_min_margin_pct": 15,  # min. verbleibende Margin (% der aktuellen)
}

TRADE_MANAGER_SYSTEM = (
    "Du bist der 'Trade-Manager' im KI-Team einer Krypto-Daytrading-Plattform und führst "
    "aktives Trade-Management auf offenen Positionen durch. Du darfst pro Durchlauf mehrere "
    "Aktionen vorschlagen – aber nur, wenn sie durch die Daten begründet sind; sonst 'hold'.\n"
    "Erlaubte Aktionen: hold | close | partial_close | adjust_sl | adjust_tp | add_margin | "
    "remove_margin | set_leverage | secure_profit.\n"
    "Bedeutung von 'value': partial_close = Prozent der Restmenge (1-99); adjust_sl/adjust_tp = "
    "absoluter Preis (alternativ 'pct' = Abstand zum aktuellen Kurs in Prozent); "
    "add_margin/remove_margin = USDT-Betrag; set_leverage = neuer Hebel; "
    "secure_profit = Prozent der gebundenen Margin, die entnommen wird (1-85).\n"
    "PROFIT-LOCK (secure_profit): Bei Trades, die klar im Gewinn sind UND deren SL bereits "
    "über/unter dem Entry abgesichert ist, darfst du einen Großteil der Margin entnehmen – "
    "der Hebel steigt dabei nachträglich, die Positionsgröße bleibt. Effekt: Kapital wird für "
    "andere Positionen frei und das verbleibende Risiko sinkt auf die Rest-Margin. Nutze das "
    "gezielt (nicht reflexartig): sichere erst den SL Richtung/über Entry, dann secure_profit. "
    "Wenn du merkst, dass ein Profit-Lock in der Situation Quatsch wäre (z.B. Liquidation rückt "
    "gefährlich nah, hohe Volatilität, SL noch nicht über Entry), dann lass es und erkläre das "
    "kurz in 'note'.\n"
    "TRADE-HORIZONTE: Trades haben ein 'Horizont'-Label [SWING] oder [SCALP]. SWING-Trades sind "
    "übergeordnete, langfristige Positionen mit niedrigem Hebel und weiten Zielen – manage sie "
    "GEDULDIG (weite Stops respektieren, nicht wegen kurzfristigem Rauschen schließen). "
    "SCALP-Trades sind kurz-/mittelfristig und werden aktiv gemanagt. Ein kurzfristiger "
    "Gegen-Trade (z.B. SHORT-Scalp während ein SWING-LONG läuft) ist ausdrücklich erlaubt "
    "und KEIN Widerspruch.\n"
    "DIVERSIFIKATION (weiche Regel): Viele gleichzeitig offene Trades in dieselbe Richtung mit "
    "derselben Prognose bündeln das Risiko. Prüfe die Exposure-Übersicht im Prompt und "
    "berücksichtige sie bei neuen Trades – kein hartes Verbot, aber begründe bewusst.\n"
    "Bei adjust_tp gibt 'target' an, welches Ziel gemeint ist: 'tp1' oder 'tpf'.\n"
    "SEITEN-REGEL (wichtig): adjust_sl – bei LONG muss der SL UNTER dem aktuellen Kurs liegen, "
    "bei SHORT ÜBER dem Kurs. adjust_tp umgekehrt (LONG über, SHORT unter dem Kurs). "
    "Werte auf der falschen Seite werden automatisch knapp auf die gültige Seite korrigiert – "
    "willst du bei einem SHORT Gewinn sichern, setze den SL knapp ÜBER den aktuellen Kurs, "
    "nicht darunter.\n"
    "SL-MINDESTABSTAND (hart erzwungen): Ein neuer SL muss mindestens 30% der initialen "
    "SL-Distanz (bzw. 0,1% vom Kurs) vom aktuellen Kurs entfernt bleiben. Stops direkt am "
    "Kurs werden blockiert – sie lösen sofort durch Rauschen aus und kosten nur Fees. "
    "Ziehe Stops selten und mit Abstand nach, nicht in jedem Durchlauf ein Stück.\n"
    "Leitlinien: Gewinne sichern statt Verluste vergrößern. Margin NUR hinzufügen, wenn die "
    "Position technisch intakt ist (nie um eine Liquidation hinauszuzögern). Hebel bei "
    "steigender Volatilität senken. Verluste früh schließen, wenn die Signal-Grundlage weg ist.\n"
    "Zusätzlich darfst du neue Trades vorschlagen ('new_trades'), wenn eine klare Chance "
    "besteht, die der reguläre Analyse-Lauf nicht abdeckt.\n"
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"actions": [{"trade_id": "...", "action": "adjust_sl", "value": 0, "pct": null, '
    '"target": "tp1", "reason": "kurze Begründung auf Deutsch"}], '
    '"new_trades": [{"symbol": "BTCUSDT", "side": "LONG", "horizon": "scalp|swing", '
    '"runner": false, "sl_pct": 0.8, "tp1_pct": 1.2, '
    '"tpf_pct": 2.0, "leverage": null, "capital_pct": 50, "confidence": 70, "reason": "..."}], '
    '"note": "1-3 Sätze Gesamteinschätzung"}\n'
    "Zu 'horizon' bei new_trades: 'swing' = übergeordneter, langfristiger Trade mit niedrigem "
    "Hebel und weiten Zielen (sl_pct bis 12, tp bis 60); 'runner': true nur bei swing = nach "
    "TP1 läuft der Rest mit Trailing-Stop weiter (kein festes Endziel). "
    "Zu 'leverage' bei new_trades: null = Hebel aus der Coin-Config des Traders (Standard). "
    "Setze nur dann einen eigenen Hebel, wenn du ihn in 'reason' explizit begründest."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trades_text(trades: List[Dict], prices: Dict[str, float]) -> str:
    """Offene Trades kompakt für den Prompt (rein, testbar)."""
    if not trades:
        return "(keine offenen Trades)"
    lines = []
    for t in trades:
        price = float(prices.get(t.get("symbol")) or t.get("entry") or 0)
        entry = float(t.get("entry") or 0)
        qty = float(t.get("qty_remaining", t.get("qty", 0)) or 0)
        lev = float(t.get("leverage", 1) or 1)
        margin = float(t.get("margin_used") or (qty * entry / lev if lev else 0))
        upnl = ((price - entry) if t.get("side") == "LONG" else (entry - price)) * qty
        upnl_pct = (upnl / margin * 100) if margin else 0.0
        horizon = str(t.get("horizon") or "scalp").upper()
        lines.append(
            f"- {t.get('id')} | {t.get('symbol')} {t.get('side')} ({t.get('mode')}) "
            f"[{horizon}{' · RUNNER' if t.get('runner') else ''}] | "
            f"Entry {round(entry, 6)} | Kurs {round(price, 6)} | "
            f"uPnL {round(upnl, 4)} USDT ({upnl_pct:+.1f}% auf Margin) | "
            f"SL {t.get('sl')} | TP1 {t.get('tp1')}{' (erreicht)' if t.get('tp1_hit') else ''} | "
            f"TPf {t.get('tpf')} | Hebel {lev}x | Margin {round(margin, 2)} USDT | "
            f"Liq {t.get('liq_price')} | Menge {qty} | "
            f"KI-Aktionen bisher {int(t.get('ai_actions', 0) or 0)} | "
            f"seit {str(t.get('opened_at', ''))[:16]}")
        if t.get("events"):
            lines.append(f"    Verlauf: {' ; '.join(str(e)[:70] for e in t['events'][-3:])}")
    return "\n".join(lines)


def exposure_text(trades: List[Dict]) -> str:
    """Kompakte Exposure-Übersicht für die Prompts (rein, testbar)."""
    if not trades:
        return "(keine offenen Trades)"
    longs = [t for t in trades if str(t.get("side")) == "LONG"]
    shorts = [t for t in trades if str(t.get("side")) == "SHORT"]
    per_sym: Dict[str, int] = {}
    for t in trades:
        key = f"{t.get('symbol')} {t.get('side')}"
        per_sym[key] = per_sym.get(key, 0) + 1
    clusters = [f"{k} x{v}" for k, v in sorted(per_sym.items()) if v >= 2]
    out = f"{len(longs)} LONG / {len(shorts)} SHORT offen"
    if clusters:
        out += f" | Häufungen: {', '.join(clusters)}"
    if len(longs) >= 3 and not shorts:
        out += " | ⚠ starke einseitige LONG-Exposure"
    elif len(shorts) >= 3 and not longs:
        out += " | ⚠ starke einseitige SHORT-Exposure"
    return out


def resolve_price(action: str, value: Optional[float], pct: Optional[float],
                  side: str, mark: float) -> Optional[float]:
    """Zielpreis aus absolutem Wert oder Prozent-Abstand (rein, testbar)."""
    if value:
        try:
            v = float(value)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    if pct is None:
        return None
    try:
        p = abs(float(pct)) / 100
    except (TypeError, ValueError):
        return None
    long_side = str(side).upper() == "LONG"
    if action == "adjust_sl":
        return round(mark * (1 - p) if long_side else mark * (1 + p), 8)
    return round(mark * (1 + p) if long_side else mark * (1 - p), 8)


def clamp_level(action: str, side: str, proposed: float, mark: float,
                buffer_pct: float = 0.1) -> Tuple[float, bool]:
    """SL/TP auf die gültige Seite des Kurses korrigieren (rein, testbar).

    Beispiel des Bugs vorher: SHORT auf SOLUSDT, Kurs 73.64, KI will SL 73.62
    (falsche Seite) -> Börse lehnt ab, Chat wird mit FEHLGESCHLAGEN geflutet.
    Jetzt wird der Wert knapp auf die gültige Seite gesetzt (Absicht 'eng
    absichern' bleibt erhalten)."""
    try:
        proposed = float(proposed)
        mark = float(mark)
    except (TypeError, ValueError):
        return proposed, False
    if mark <= 0:
        return proposed, False
    long_side = str(side).upper() == "LONG"
    buf = mark * abs(buffer_pct) / 100
    if action == "adjust_sl":
        if long_side and proposed >= mark:
            return round(mark - buf, 8), True
        if not long_side and proposed <= mark:
            return round(mark + buf, 8), True
    else:
        if long_side and proposed <= mark:
            return round(mark + buf, 8), True
        if not long_side and proposed >= mark:
            return round(mark - buf, 8), True
    return proposed, False


def min_sl_gap(entry, initial_sl, mark, ratio: float = 0.3,
               floor_pct: float = 0.1) -> float:
    """Mindestabstand eines KI-SL zum aktuellen Kurs (rein, testbar)."""
    try:
        mark = float(mark or 0)
    except (TypeError, ValueError):
        return 0.0
    if mark <= 0:
        return 0.0
    gap = mark * floor_pct / 100
    try:
        entry_f, init_f = float(entry or 0), float(initial_sl or 0)
        if entry_f > 0 and init_f > 0:
            gap = max(gap, abs(entry_f - init_f) * ratio)
    except (TypeError, ValueError):
        pass
    return gap


def check_limits(trade: Dict, action: str, value: Optional[float],
                 settings: Dict, now_ts: Optional[float] = None,
                 in_profit: bool = False) -> Tuple[bool, str]:
    """Harte Schutzregeln vor jeder KI-Aktion (rein, testbar)."""
    if action not in ACTIONS:
        return False, f"Unbekannte Aktion '{action}'"
    if action == "hold":
        return True, ""
    if action in ("add_margin", "remove_margin", "set_leverage", "secure_profit") \
            and not settings.get("allow_margin", True):
        return False, "Margin-/Hebel-Anpassungen sind deaktiviert"
    if action == "secure_profit" and not settings.get("profit_lock_enabled", True):
        return False, "Profit-Lock ist deaktiviert"
    if action == "secure_profit" and not in_profit:
        return False, "secure_profit nur bei Trades im Gewinn erlaubt"
    used = int(trade.get("ai_actions", 0) or 0)
    if used >= int(settings.get("max_actions_per_trade", 8)):
        return False, f"Aktions-Limit für diesen Trade erreicht ({used})"
    cooldown = float(settings.get("cooldown_min", 3)) * 60
    last = float(trade.get("ai_last_action_ts", 0) or 0)
    now_ts = now_ts if now_ts is not None else time.time()
    if cooldown and last and (now_ts - last) < cooldown:
        return False, f"Cooldown aktiv ({int((cooldown - (now_ts - last)) / 60) + 1} min)"
    # Hebel-Obergrenze: bei Gewinn-Trades gilt das (höhere) Profit-Lock-Limit
    lev_cap = float(settings.get("max_leverage", 50))
    if in_profit and settings.get("profit_lock_enabled", True):
        lev_cap = max(lev_cap, float(settings.get("profit_lock_max_leverage", 100)))
    if action == "set_leverage":
        try:
            lev = float(value)
        except (TypeError, ValueError):
            return False, "Hebel fehlt"
        if lev < 1 or lev > lev_cap:
            return False, f"Hebel {lev}x außerhalb des erlaubten Bereichs (1-{lev_cap:g})"
    if action in ("add_margin", "remove_margin", "secure_profit"):
        qty = float(trade.get("qty_remaining", trade.get("qty", 0)) or 0)
        lev = float(trade.get("leverage", 1) or 1)
        margin = float(trade.get("margin_used") or (qty * float(trade.get("entry", 0)) / lev
                                                   if lev else 0))
        if action == "secure_profit":
            try:
                pct = float(value)
            except (TypeError, ValueError):
                return False, "Prozentwert fehlt"
            if not 1 <= pct <= 85:
                return False, "secure_profit erwartet 1-85 % der Margin"
            amount = margin * pct / 100
        else:
            try:
                amount = abs(float(value))
            except (TypeError, ValueError):
                return False, "Betrag fehlt"
            if amount <= 0:
                return False, "Betrag muss > 0 sein"
        limit = margin * float(settings.get("max_margin_add_pct", 100)) / 100
        if action == "add_margin" and limit and amount > limit:
            return False, f"Margin-Aufschlag {amount} > Limit {round(limit, 2)} USDT"
        if action in ("remove_margin", "secure_profit"):
            if amount >= margin:
                return False, "Es kann nicht mehr Margin entnommen werden als gebunden ist"
            left = margin - amount
            min_left = margin * float(settings.get("profit_lock_min_margin_pct", 15)) / 100
            if left < min_left:
                return False, (f"Es müssen mind. {settings.get('profit_lock_min_margin_pct', 15)}% "
                               f"der Margin gebunden bleiben (Rest wäre {round(left, 2)} USDT, "
                               f"Minimum {round(min_left, 2)} USDT)")
            notional = qty * float(trade.get("entry", 0))
            if left > 0 and notional > 0:
                new_lev = notional / left
                if new_lev > lev_cap:
                    return False, (f"Resultierender Hebel {round(new_lev, 1)}x über dem "
                                   f"Limit {lev_cap:g}x")
    if action == "partial_close":
        try:
            pct = float(value)
        except (TypeError, ValueError):
            return False, "Prozentwert fehlt"
        if not 1 <= pct <= 99:
            return False, "partial_close erwartet 1-99 %"
    return True, ""


class AITradeManager:
    ROLE = "trade_manager"

    def __init__(self):
        self.engine = None
        self.autotrader = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)
        self.last_run: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_note: Optional[str] = None
        self.running_now = False
        self._next_due = 0.0
        # Anti-Spam: (trade_id, action) -> ts der letzten fehlgeschlagenen Meldung
        self._fail_log: Dict[Tuple[str, str], float] = {}

    def setup(self, engine, autotrader):
        self.engine = engine
        self.autotrader = autotrader

    @property
    def db(self):
        return self.engine.db if self.engine else None

    def _role_cfg(self) -> Dict:
        from services.ai_roles import role_manager
        return role_manager.role_cfg(self.ROLE)

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "ai_trade_manager"})
            if doc:
                for k in DEFAULT_SETTINGS:
                    if k in doc:
                        self.settings[k] = doc[k]
        except Exception as e:
            logger.warning(f"Trade-Manager State laden fehlgeschlagen: {e}")

    async def update_settings(self, updates: Dict) -> Dict:
        for key, caster in (("enabled", bool), ("allow_open", bool), ("allow_margin", bool)):
            if key in updates:
                self.settings[key] = caster(updates[key])
        for key, lo, hi in (("interval_min", 1, 120), ("max_actions_per_trade", 1, 50),
                            ("cooldown_min", 0, 120), ("max_leverage", 1, 200),
                            ("max_margin_add_pct", 10, 500),
                            ("profit_lock_max_leverage", 1, 200),
                            ("profit_lock_min_margin_pct", 5, 90)):
            if key in updates:
                try:
                    self.settings[key] = max(lo, min(hi, int(updates[key])))
                except (TypeError, ValueError):
                    pass
        if "profit_lock_enabled" in updates:
            self.settings["profit_lock_enabled"] = bool(updates["profit_lock_enabled"])
        await self.db.settings.update_one({"_id": "ai_trade_manager"},
                                          {"$set": dict(self.settings)}, upsert=True)
        return dict(self.settings)

    # ---------------- Ausführung einer einzelnen Aktion ----------------
    async def apply_action(self, trade_id: str, action: str, value: Optional[float] = None,
                           pct: Optional[float] = None, target: str = "tp1",
                           reason: str = "", source: str = "ki",
                           enforce_limits: bool = True) -> Dict:
        trade = await self.db.auto_trades.find_one({"id": trade_id, "status": "open"})
        if not trade:
            return {"status": "error", "detail": "Offener Trade nicht gefunden"}
        # Manuelle Bitunix-Positionen ('Manuell (Bitunix)' / vom Watchdog nur zur
        # Sichtbarkeit übernommen) werden von der KI NICHT gemanagt: Close/Adjust
        # schlägt an der Börse fehl (z.B. QQQ nicht per OpenAPI handelbar) und
        # erzeugte Telegram-Spam. Nutzer-Grundsatz: manuelle Trades nicht anfassen.
        if trade.get("manual_trade") or trade.get("external_adopted") \
                or trade.get("strategy_id") == "external":
            return {"status": "blocked",
                    "detail": "Manuelle Bitunix-Position – wird von der KI nicht "
                              "gemanagt (nur direkt bei Bitunix oder manuell auf "
                              "der Website verwalten)"}
        # Datensammel-Trades (Phase 4) laufen für saubere ML-Labels unangetastet
        # bis SL/TP: KI-Micro-Management (SL-Ratchet, Hebel-Reflexe) zerstörte die
        # Label-Qualität und stoppte Trades am Entry aus. Manuell bleibt erlaubt.
        if source == "ki" and trade.get("data_collection"):
            return {"status": "blocked",
                    "detail": "Datensammel-Trade (Paper, Badge DATEN) – läuft für "
                              "saubere ML-Labels unangetastet bis SL/TP"}
        if enforce_limits:
            ok, err = check_limits(trade, action, value, self.settings)
            if not ok:
                return {"status": "blocked", "detail": err}
            if action == "set_leverage":
                ok_m, why = master_prompt.check_trade(
                    trade.get("symbol"), trade.get("side"), leverage=value)
                if not ok_m:
                    return {"status": "blocked", "detail": why}
        if action == "hold":
            return {"status": "ok", "action": "hold"}

        mark = float(await self.autotrader._current_mark(trade["symbol"])
                     or trade.get("entry") or 0)
        result: Optional[Dict] = None
        try:
            if action == "close":
                result = await self.autotrader.manual_close(trade_id, mark)
            elif action == "partial_close":
                result = await self.autotrader.partial_close(trade_id, float(value), mark)
            elif action == "adjust_sl":
                price = resolve_price(action, value, pct, trade["side"], mark)
                if not price:
                    return {"status": "error", "detail": "Kein SL-Preis angegeben"}
                price, clamped = clamp_level("adjust_sl", trade["side"], price, mark)
                if clamped:
                    reason = (str(reason) + f" [auto-korrigiert: SL auf gültige "
                              f"Kursseite gesetzt -> {price}]")[:400]
                # SCHUTZREGEL (Bug-Report: KI setzte SL 0.2023 -> 0.0002 und
                # entfernte damit faktisch den Stop): Die KI darf den SL nur
                # ENGER ziehen (Richtung Kurs), nie das Risiko vergrößern.
                if source == "ki":
                    cur_sl = float(trade.get("sl") or 0)
                    long_side = str(trade.get("side", "")).upper() == "LONG"
                    if cur_sl > 0 and ((long_side and price < cur_sl)
                                       or (not long_side and price > cur_sl)):
                        return {"status": "blocked",
                                "detail": f"SL-Erweiterung blockiert: {price} würde "
                                          f"das Risiko gegenüber dem aktuellen SL "
                                          f"{cur_sl} vergrößern (Stops werden nie "
                                          "vom Kurs weg verschoben)"}
                    # SL-Ratchet-Guard (Bug: KI zog den SL bis 0,007% an den Kurs
                    # -> sofortiger Stop durch Rauschen, Fees fressen den Trade).
                    # Neuer KI-SL muss mind. 30% der initialen SL-Distanz bzw.
                    # 0,1% vom aktuellen Kurs entfernt bleiben.
                    gap = min_sl_gap(trade.get("entry"), trade.get("initial_sl"), mark)
                    if gap > 0 and abs(mark - price) < gap:
                        return {"status": "blocked",
                                "detail": f"SL-Anpassung blockiert: {price} liegt zu "
                                          f"dicht am Kurs {mark} (Mindestabstand "
                                          f"{round(gap, 8)}) – Micro-Stops lösen "
                                          "sofort aus und kosten nur Fees"}
                result = await self.autotrader.adjust_levels(trade_id, sl=price)
            elif action == "adjust_tp":
                price = resolve_price(action, value, pct, trade["side"], mark)
                if not price:
                    return {"status": "error", "detail": "Kein TP-Preis angegeben"}
                price, clamped = clamp_level("adjust_tp", trade["side"], price, mark)
                if clamped:
                    reason = (str(reason) + f" [auto-korrigiert: TP auf gültige "
                              f"Kursseite gesetzt -> {price}]")[:400]
                kwargs = {"tpf": price} if str(target).lower() == "tpf" else {"tp1": price}
                result = await self.autotrader.adjust_levels(trade_id, **kwargs)
            elif action in ("add_margin", "remove_margin"):
                amount = abs(float(value)) * (1 if action == "add_margin" else -1)
                result = await self.autotrader.adjust_margin(trade_id, amount)
            elif action == "set_leverage":
                result = await self.autotrader.adjust_leverage(trade_id, float(value))
        except Exception as e:
            logger.error(f"Trade-Aktion {action} auf {trade_id} fehlgeschlagen: {e}")
            return {"status": "error", "detail": str(e)[:200]}

        if result is None:
            return {"status": "error", "detail": "Aktion nicht ausgeführt"}
        if isinstance(result, dict) and result.get("error"):
            await self._audit(trade, action, value, reason, source, result, ok=False)
            return {"status": "rejected", "detail": result["error"]}

        await self.db.auto_trades.update_one({"id": trade_id}, {"$set": {
            "ai_actions": int(trade.get("ai_actions", 0) or 0) + 1,
            "ai_last_action_ts": time.time()}})
        await self._audit(trade, action, value, reason, source, result, ok=True)
        return {"status": "ok", "action": action, "result": result}

    async def _audit(self, trade: Dict, action: str, value, reason: str,
                     source: str, result: Dict, ok: bool):
        entry = {
            "id": str(uuid.uuid4()), "trade_id": trade.get("id"),
            "symbol": trade.get("symbol"), "side": trade.get("side"),
            "mode": trade.get("mode"), "action": action, "value": value,
            "reason": str(reason)[:400], "source": source, "ok": ok,
            "result": result, "ts": _now_iso(),
        }
        try:
            await self.db.ai_trade_actions.insert_one(dict(entry))
        except Exception as e:
            logger.warning(f"Trade-Aktion Audit fehlgeschlagen: {e}")
        if ok:
            try:
                await self.db.ai_chat.insert_one({
                    "id": str(uuid.uuid4()), "role": "trade",
                    "text": f"{action.upper()} auf {trade.get('symbol')} "
                            f"{trade.get('side')} ({trade.get('mode')}): "
                            f"{reason or '—'}",
                    "action": action, "trade_id": trade.get("id"),
                    "result": result, "source": source, "ts": entry["ts"]})
            except Exception:
                pass
            await memory.remember(
                "trade_action", f"{action} {trade.get('symbol')} {trade.get('side')}",
                f"{reason or ''} | Ergebnis: {result}",
                meta={"trade_id": trade.get("id"), "mode": trade.get("mode")},
                tags=["trade", action], weight=2, source=f"trade_manager/{source}")
        else:
            # Fehlgeschlagene Aktionen sichtbar machen – aber dieselbe
            # Fehlermeldung pro Trade+Aktion höchstens alle 30 min (Anti-Spam).
            key = (str(trade.get("id")), action)
            now_ts = time.time()
            if now_ts - self._fail_log.get(key, 0) < 1800:
                return
            self._fail_log[key] = now_ts
            try:
                await self.db.ai_chat.insert_one({
                    "id": str(uuid.uuid4()), "role": "trade",
                    "text": f"{action.upper()} auf {trade.get('symbol')} "
                            f"{trade.get('side')} ({trade.get('mode')}) FEHLGESCHLAGEN: "
                            f"{(result or {}).get('error') or 'unbekannter Fehler'}",
                    "action": action, "trade_id": trade.get("id"), "failed": True,
                    "result": result, "source": source, "ts": entry["ts"]})
            except Exception:
                pass

    # ---------------- Custom-Trade eröffnen ----------------
    async def open_trade(self, spec: Dict, source: str = "ki") -> Dict:
        if not self.settings.get("allow_open", True) and source == "ki":
            return {"status": "blocked", "detail": "Eigene KI-Trades sind deaktiviert"}
        from core.state import control_state
        if control_state.get("trades_paused"):
            return {"status": "blocked",
                    "detail": "Master-Schalter 'Stop All Trades' ist aktiv"}
        if source == "ki" and not (self.engine.config or {}).get("enabled"):
            return {"status": "blocked",
                    "detail": "KI Trader ist deaktiviert – keine neuen KI-Trades"}
        symbol = str(spec.get("symbol") or "").upper()
        side = str(spec.get("side") or "").upper()
        if side not in ("LONG", "SHORT"):
            return {"status": "error", "detail": "side muss LONG oder SHORT sein"}
        if symbol not in (self.engine.symbols or []):
            return {"status": "error", "detail": f"Symbol {symbol} wird nicht beobachtet"}
        if source == "ki":
            open_cnt = await self.db.auto_trades.count_documents(
                {"status": "open", "strategy_id": "ai_trader"})
            ok, why = master_prompt.check_trade(
                symbol, side, confidence=spec.get("confidence"),
                leverage=spec.get("leverage"), open_trades=open_cnt)
            if not ok:
                logger.info(f"KI-Custom-Trade blockiert: {why}")
                return {"status": "blocked", "detail": why}
        candles = self.engine.scanner.candle_buffer.get(symbol, [])
        price = float(await self.autotrader._current_mark(symbol) or 0) or \
            (float(candles[-1]["close"]) if candles else 0)
        if price <= 0:
            return {"status": "error", "detail": "Kein Kurs verfügbar"}

        def _f(key, default, lo, hi):
            try:
                return max(lo, min(hi, float(spec.get(key, default) or default)))
            except (TypeError, ValueError):
                return default

        horizon = "swing" if str(spec.get("horizon") or "").lower() == "swing" else "scalp"
        runner = bool(spec.get("runner")) and horizon == "swing"
        if horizon == "swing":
            swing_cfg = (self.engine.config or {})
            if not swing_cfg.get("swing_enabled", True):
                return {"status": "blocked", "detail": "Swing-Trades sind deaktiviert"}
            sl_pct = _f("sl_pct", 3.0, 0.5, 12.0) / 100
            tp1_pct = _f("tp1_pct", 5.0, 0.8, 25.0) / 100
            tpf_default = 60.0 if runner else 10.0
            tpf_pct = max(tp1_pct, _f("tpf_pct", tpf_default, 1.0, 60.0) / 100)
            if runner:
                tpf_pct = max(tpf_pct, 0.5)  # Runner: Endziel sehr weit weg, Trailing übernimmt
        else:
            sl_pct = _f("sl_pct", 0.8, 0.15, 5.0) / 100
            tp1_pct = _f("tp1_pct", 1.2, 0.2, 8.0) / 100
            tpf_pct = max(tp1_pct, _f("tpf_pct", 2.0, 0.3, 15.0) / 100)
        sign = 1 if side == "LONG" else -1
        sl = price * (1 - sign * sl_pct)
        tp1 = price * (1 + sign * tp1_pct)
        tpf = price * (1 + sign * tpf_pct)
        now = self.engine.scanner.berlin_now()
        signal = {
            "symbol": symbol, "type": side, "signal_class": "SIGNAL",
            "entry_price": round(price, 6), "stop_loss": round(sl, 6),
            "take_profit_1": round(tp1, 6), "take_profit_full": round(tpf, 6),
            "crv": round(abs(tp1 - price) / abs(price - sl), 2) if price != sl else 0,
            "rsi": 0, "ema_fast": 0, "ema_slow": 0,
            "rules_met": {"ai_custom_trade": True}, "rules_met_count": 1, "rules_total": 1,
            "timestamp": _now_iso(), "trade_date": self.engine.scanner.berlin_date(),
            "hour": now.hour, "weekday": now.weekday(),
            "session": self.engine.scanner.get_current_session(),
            "strategy_id": "ai_trader", "strategy_name": "KI Trader",
            "status": "active",
            "ai_confidence": _f("confidence", 70, 0, 100),
            "ai_reasoning": str(spec.get("reason")
                                or ("" if source != "ki" else "KI-Custom-Trade"))[:600],
            "use_ai_levels": True,
            # Hebel hart auf das Trade-Manager-Limit begrenzen (0 = Coin-Config)
            "ai_leverage": _f("leverage", 0, 0,
                              float(self.settings.get("max_leverage", 50))),
            "ai_capital_pct": _f("capital_pct", 100, 5, 100),
            "ai_custom": True, "ai_source": source,
            "ai_horizon": horizon, "ai_runner": runner,
            "timeframe": "swing" if horizon == "swing" else None,
        }
        # Manuelle Trades: expliziter Modus (Live/Paper) und absolute Margin in
        # USDT erlaubt – für die KI bleiben Modus & Kapital unantastbar.
        if source != "ki":
            # Bug-Report: manuelle Website-Trades erschienen als Signal und als
            # "KI Trader"-Trade. Ab jetzt: kein Signal-Eintrag, Trade zählt als
            # 'Manuell (Website)' (strategy_id external, manual_trade=True).
            signal["manual_trade"] = True
            signal["suppress_signal"] = True
            req_mode = str(spec.get("mode") or "").lower()
            if req_mode in ("live", "paper"):
                signal["force_mode"] = req_mode
            try:
                margin_usdt = float(spec.get("margin_usdt") or 0)
            except (TypeError, ValueError):
                margin_usdt = 0.0
            if margin_usdt > 0:
                signal["ai_max_capital"] = round(margin_usdt, 6)
                signal["ai_capital_pct"] = 100
        # Swing: eigener, niedriger Hebel-Deckel (unabhängig vom Scalp-Limit)
        if horizon == "swing":
            swing_max = float((self.engine.config or {}).get("swing_max_leverage", 8) or 8)
            lev_req = float(signal.get("ai_leverage") or 0)
            signal["ai_leverage"] = min(lev_req, swing_max) if lev_req > 0 else swing_max
        # Max. Kapital pro Trade des KI-Traders gilt auch für Custom-Trades
        # (manuelle Trades mit expliziter margin_usdt bleiben unangetastet)
        try:
            max_cap = float((self.engine.config or {}).get("max_capital_per_trade") or 0)
            if max_cap > 0 and "ai_max_capital" not in signal:
                signal["ai_max_capital"] = max_cap
        except (TypeError, ValueError):
            pass
        try:
            ok = await self.engine.signal_cb(signal)
        except Exception as e:
            logger.error(f"KI-Custom-Trade {symbol} fehlgeschlagen: {e}")
            return {"status": "error", "detail": str(e)[:200]}
        if not ok:
            return {"status": "rejected",
                    "detail": signal.get("_reject_reason")
                    or "Signal wurde von der Trade-Pipeline abgelehnt "
                       "(Coin/Strategie aus, Limit oder Kapital)"}
        # BUGFIX: vorher kam 'ok' zurück, obwohl Guards (Anti-Stacking, Limits,
        # Kapital) den Trade still verworfen hatten – Aufrufer/KI wussten nichts davon.
        if not signal.get("_trade_opened"):
            return {"status": "rejected",
                    "detail": signal.get("_reject_reason")
                    or "Trade nicht eröffnet (Guard, Limit oder Kapital)"}
        await memory.remember("trade_action", f"Custom-Trade {side} {symbol}",
                              str(spec.get("reason") or "")[:500],
                              meta={"spec": spec}, tags=["trade", "custom"],
                              weight=2, source=f"trade_manager/{source}")
        return {"status": "ok", "symbol": symbol, "side": side, "entry": round(price, 6),
                "sl": round(sl, 6), "tp1": round(tp1, 6), "tpf": round(tpf, 6)}

    # ---------------- KI-Durchlauf über offene Trades ----------------
    async def review(self, manual: bool = False) -> Dict:
        if self.running_now:
            return {"status": "busy", "detail": "Trade-Review läuft bereits"}
        if not manual and not self.settings.get("enabled", True):
            return {"status": "skipped", "detail": "Trade-Manager deaktiviert"}
        if not self._role_cfg().get("enabled", True):
            return {"status": "skipped", "detail": "Rolle deaktiviert"}
        self.running_now = True
        try:
            # Manuelle/externe Bitunix-Positionen ausschließen: sie sollen von
            # der KI weder geschlossen noch angepasst werden (s. apply_action).
            trades = await self.db.auto_trades.find(
                {"status": "open", "manual_trade": {"$ne": True},
                 "external_adopted": {"$ne": True},
                 "strategy_id": {"$ne": "external"},
                 "data_collection": {"$ne": True}}) \
                .sort("opened_at", -1).limit(30).to_list(30)
            for t in trades:
                t.pop("_id", None)
            if not trades:
                self.last_run = _now_iso()
                return {"status": "no_trades", "detail": "Keine offenen Trades"}
            prices = {}
            for t in trades:
                sym = t.get("symbol")
                if sym not in prices:
                    prices[sym] = float(await self.autotrader._current_mark(sym)
                                        or t.get("entry") or 0)
            context = ""
            try:
                context = await self.engine._analysis_extra_blocks(purpose="trade_review")
            except Exception:
                pass
            prompt = (
                f"{master_prompt.prompt_block()}\n\n"
                f"Zeitpunkt (UTC): {_now_iso()}\n"
                f"Limits: max. {self.settings['max_actions_per_trade']} Aktionen pro Trade, "
                f"Cooldown {self.settings['cooldown_min']} min, max. Hebel "
                f"{self.settings['max_leverage']}x, Margin-Aufschlag bis "
                f"{self.settings['max_margin_add_pct']}% der Start-Margin, "
                f"Profit-Lock (secure_profit) "
                f"{'erlaubt: max. ' + str(self.settings.get('profit_lock_max_leverage', 100)) + 'x Hebel bei Gewinn-Trades, mind. ' + str(self.settings.get('profit_lock_min_margin_pct', 15)) + '% Margin bleiben gebunden' if self.settings.get('profit_lock_enabled', True) else 'gesperrt'}, "
                f"neue Trades {'erlaubt' if self.settings.get('allow_open') else 'gesperrt'}.\n\n"
                f"=== EXPOSURE-ÜBERSICHT ===\n{exposure_text(trades)}\n\n"
                f"=== OFFENE TRADES ===\n{trades_text(trades, prices)}\n\n"
                + (f"{context[:6000]}\n\n" if context else "")
                + "Entscheide jetzt über Anpassungen. Nur begründete Aktionen, sonst 'hold'."
            )
            text, provider, model = await self.engine.generate_for_role(
                self.ROLE, prompt, TRADE_MANAGER_SYSTEM, temperature=0.25)
            data = self.engine._parse_json(text)
            self.last_note = str(data.get("note", ""))[:600]
            applied, skipped = [], []
            for act in (data.get("actions") or [])[:20]:
                if not isinstance(act, dict):
                    continue
                res = await self.apply_action(
                    str(act.get("trade_id") or ""), str(act.get("action") or "hold"),
                    value=act.get("value"), pct=act.get("pct"),
                    target=str(act.get("target") or "tp1"),
                    reason=str(act.get("reason") or "")[:400], source="ki")
                (applied if res.get("status") == "ok" else skipped).append(
                    {"trade_id": act.get("trade_id"), "action": act.get("action"),
                     "status": res.get("status"), "detail": res.get("detail")})
            opened = []
            # Neue Trades nur, wenn erlaubt, der KI Trader aktiv ist und der
            # Master-Schalter nicht auf 'Stop All Trades' steht.
            from core.state import control_state
            can_open = (self.settings.get("allow_open", True)
                        and bool((self.engine.config or {}).get("enabled"))
                        and not control_state.get("trades_paused"))
            if can_open:
                for spec in (data.get("new_trades") or [])[:3]:
                    if isinstance(spec, dict):
                        opened.append(await self.open_trade(spec, source="ki"))
            self.last_run = _now_iso()
            self.last_error = None
            logger.info(f"Trade-Manager Review ({model}): {len(applied)} Aktionen, "
                        f"{len(skipped)} verworfen, {len(opened)} neue Trades")
            return {"status": "ok", "model": f"{provider}/{model}", "note": self.last_note,
                    "trades_reviewed": len(trades), "applied": applied,
                    "skipped": skipped, "opened": opened, "ts": self.last_run}
        except Exception as e:
            self.last_error = str(e)[:300]
            logger.error(f"Trade-Manager Review fehlgeschlagen: {e}")
            return {"status": "error", "detail": self.last_error}
        finally:
            self.running_now = False

    RETHINK_COOLDOWN_MIN = 15

    async def review_single(self, trade_id: str) -> Dict:
        """Einzel-Trade-Review auf Nutzer-Klick ('Trade überdenken', 15.06.).

        Gleiche geprüfte Ausführungs-Schicht wie das normale Review
        (apply_action mit allen Limits/Guards), aber nur EIN Trade und ein
        kleiner fokussierter Prompt. Cooldown 15 min pro Trade."""
        if self.running_now:
            return {"status": "error", "detail": "Trade-Review läuft bereits – kurz warten"}
        trade = await self.db.auto_trades.find_one({"id": trade_id, "status": "open"})
        if not trade:
            return {"status": "error", "detail": "Offener Trade nicht gefunden"}
        trade.pop("_id", None)
        last = trade.get("rethink_ts")
        if last:
            try:
                dt = datetime.fromisoformat(str(last))
                wait = self.RETHINK_COOLDOWN_MIN * 60 - (
                    datetime.now(timezone.utc) - dt).total_seconds()
                if wait > 0:
                    return {"status": "cooldown",
                            "detail": f"Schon vor kurzem überdacht – wieder möglich "
                                      f"in {int(wait // 60) + 1} min"}
            except Exception:
                pass
        self.running_now = True
        try:
            sym = trade.get("symbol")
            price = float(await self.autotrader._current_mark(sym)
                          or trade.get("entry") or 0)
            market = ""
            try:
                from services.ai_market_observer import market_observer, snapshot_to_text
                snap = market_observer.entry_snapshot(sym)
                if snap and snap.get("features"):
                    market = snapshot_to_text({"symbol": sym,
                                               "features": snap["features"]})
            except Exception:
                pass
            prompt = (
                f"Zeitpunkt (UTC): {_now_iso()}\n"
                "EINZEL-REVIEW auf Nutzer-Klick: Überdenke NUR diesen einen offenen "
                "Trade kritisch neu. Ist die Einstiegs-These noch intakt? Sei ehrlich – "
                "'hold' nur, wenn der Trade wirklich noch Sinn ergibt.\n\n"
                f"=== DER TRADE ===\n{trades_text([trade], {sym: price})}\n\n"
                + (f"=== MARKT JETZT ===\n{market}\n\n" if market else "")
                + 'Antworte NUR mit validem JSON und HÖCHSTENS EINER Aktion: '
                  '{"actions": [{"trade_id": "' + trade_id + '", '
                  '"action": "hold|close|partial_close|adjust_sl|adjust_tp|secure_profit", '
                  '"value": Zahl|null, "pct": Zahl|null, "target": "tp1|tpf", '
                  '"reason": "1 Satz Deutsch"}], '
                  '"note": "max. 2 Sätze: These noch intakt? Empfehlung + Kernargument"}')
            text, provider, model = await self.engine.generate_for_role(
                self.ROLE, prompt, TRADE_MANAGER_SYSTEM, temperature=0.25)
            data = self.engine._parse_json(text)
            note = str(data.get("note", ""))[:600]
            applied, skipped = [], []
            for act in (data.get("actions") or [])[:1]:
                if not isinstance(act, dict):
                    continue
                action = str(act.get("action") or "hold").lower()
                if action == "hold":
                    continue
                res = await self.apply_action(
                    trade_id, action, value=act.get("value"), pct=act.get("pct"),
                    target=str(act.get("target") or "tp1"),
                    reason=("[Überdenken] " + str(act.get("reason") or ""))[:400],
                    source="ki")
                entry = {"action": action, "status": res.get("status"),
                         "detail": res.get("detail"),
                         "reason": str(act.get("reason") or "")[:200]}
                (applied if res.get("status") == "ok" else skipped).append(entry)
            await self.db.auto_trades.update_one(
                {"id": trade_id},
                {"$set": {"rethink_ts": _now_iso(), "rethink_note": note}})
            logger.info(f"Trade-Überdenken {sym} ({provider}/{model}): "
                        f"{len(applied)} Aktionen, {len(skipped)} verworfen")
            return {"status": "ok", "note": note, "model": f"{provider}/{model}",
                    "applied": applied, "skipped": skipped}
        except Exception as e:
            logger.error(f"Trade-Überdenken fehlgeschlagen: {e}")
            return {"status": "error", "detail": str(e)[:300]}
        finally:
            self.running_now = False

    async def recent_actions(self, limit: int = 30) -> List[Dict]:
        rows = await self.db.ai_trade_actions.find().sort("ts", -1) \
            .limit(max(1, min(200, limit))).to_list(200)
        for r in rows:
            r.pop("_id", None)
        return rows

    def status(self) -> Dict:
        return {"settings": dict(self.settings), "role_enabled": self._role_cfg().get("enabled", True),
                "last_run": self.last_run, "last_error": self.last_error,
                "last_note": self.last_note, "running_now": self.running_now,
                "actions": list(ACTIONS)}

    async def tick(self):
        if not self.settings.get("enabled", True) or self.db is None:
            return
        if not self.engine or not self.engine.key:
            return
        # Kein automatisches Trade-Management, wenn der KI Trader deaktiviert
        # ist – vorher lief der Trade-Manager (inkl. neuer Custom-Trades)
        # unabhängig vom KI-Trader-Schalter weiter. Manuelles Review bleibt möglich.
        if not (self.engine.config or {}).get("enabled"):
            return
        now = time.time()
        if now < self._next_due:
            return
        self._next_due = now + max(1, int(self.settings.get("interval_min", 5))) * 60
        if await self.db.auto_trades.count_documents({"status": "open"}):
            await self.review()


trade_manager = AITradeManager()
