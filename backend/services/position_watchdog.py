"""Positions-Watchdog: prüft periodisch ALLE offenen Bitunix-Positionen.

Hintergrund (Bug-Report): ADA-/DOT-Positionen liefen an der Börse ohne
Stop-Loss in die Liquidation und waren auf der Website nicht sichtbar
(Order-Antwort ging verloren -> Trade wurde lokal verworfen).

Der Watchdog ist die letzte Verteidigungslinie, unabhängig vom Order-Flow:
  1. Börsen-Positionen OHNE lokalen Trade werden als 'Extern (Watchdog)'
     übernommen und erscheinen damit in den offenen Trades der Website.
  2. Für JEDE offene Position wird geprüft, ob an der Börse ein Stop-Loss
     aktiv ist. Fehlt er, wird er nachgezogen (aus dem lokalen Trade oder
     als Notfall-SL relativ zum Einstieg). Nach `max_sl_retries` Fehlzyklen
     wird die Position notfallgeschlossen (Nutzer-Vorgabe: Retry -> Close).

Konfiguration in settings['position_watchdog'], Status für die UI in
settings['position_watchdog_status'] (GET /api/autotrade/watchdog/status).
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "enabled": True,
    "interval_sec": 120,
    "fallback_sl_percent": 2.0,   # Notfall-SL-Abstand, wenn kein lokaler SL existiert
    "max_sl_retries": 3,          # Fehlzyklen bis zum Notfall-Close
    "emergency_close": True,      # nach max_sl_retries Position schließen
    "adopt_unknown": True,        # fremde Positionen lokal sichtbar machen
    # Manuelle Bitunix-Positionen (nicht über die Website eröffnet) NICHT
    # anfassen: kein SL-Zwang, kein Dust-Close, kein Notfall-Close. Sie werden
    # nur zur Sichtbarkeit übernommen (adopt_unknown). Default AUS = nur
    # Website-Trades werden gemanagt (Vorgabe des Traders).
    "manage_external": False,
}

_PRICE_KEYS = ("avgOpenPrice", "avgPrice", "entryPrice", "openPrice")
_QTY_KEYS = ("qty", "positionAmt", "amount", "size", "total", "available")


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_positions(payload) -> List[Dict]:
    """Bitunix get_pending_positions -> normalisierte Liste (rein, testbar)."""
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        return []
    data = payload.get("data")
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    out: List[Dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_side = str(row.get("side") or row.get("positionSide") or "").upper()
        side = "LONG" if raw_side in ("BUY", "LONG") else \
            ("SHORT" if raw_side in ("SELL", "SHORT") else "")
        qty = 0.0
        for k in _QTY_KEYS:
            if row.get(k) not in (None, ""):
                qty = abs(_f(row[k]))
                if qty:
                    break
        entry = 0.0
        for k in _PRICE_KEYS:
            entry = _f(row.get(k))
            if entry > 0:
                break
        pid = row.get("positionId") or row.get("id")
        sym = row.get("symbol")
        if not side or not sym or qty <= 0 or not pid:
            continue
        out.append({"bitunix_symbol": str(sym), "side": side, "qty": qty,
                    "entry": entry, "position_id": str(pid),
                    "leverage": _f(row.get("leverage")),
                    "margin": _f(row.get("margin") or row.get("im"))})
    return out


def emergency_sl(side: str, entry: float, mark: float, pct: float,
                 local_sl: Optional[float] = None) -> Optional[float]:
    """SL-Preis für eine Position ohne Börsen-SL bestimmen (rein, testbar).
    Bevorzugt den lokalen Trade-SL; sonst `pct` Abstand zum Entry. Der Wert
    wird immer auf die gültige Seite des aktuellen Kurses korrigiert."""
    base = entry if entry > 0 else mark
    if base <= 0:
        return None
    ref = mark if mark > 0 else base
    p = max(abs(_f(pct)), 0.1) / 100
    sl = _f(local_sl) if local_sl else 0.0
    if sl <= 0:
        sl = base * (1 - p) if side == "LONG" else base * (1 + p)
    if side == "LONG" and sl >= ref:
        sl = ref * (1 - p)
    if side == "SHORT" and sl <= ref:
        sl = ref * (1 + p)
    return round(sl, 8)


class PositionWatchdog:
    def __init__(self):
        self.db = None
        self.client = None
        self.autotrader = None
        self.telegram = None
        self.settings: Dict = dict(DEFAULT_SETTINGS)
        # positionId -> Anzahl fehlgeschlagener SL-Zyklen
        self._sl_fail: Dict[str, int] = {}

    def setup(self, db, client, autotrader, telegram):
        self.db = db
        self.client = client
        self.autotrader = autotrader
        self.telegram = telegram

    async def load_state(self):
        try:
            doc = await self.db.settings.find_one({"_id": "position_watchdog"})
            if doc:
                for k in DEFAULT_SETTINGS:
                    if k in doc:
                        self.settings[k] = doc[k]
        except Exception as e:
            logger.warning(f"Watchdog-Settings laden fehlgeschlagen: {e}")
        # Einmalige Migration: alte 'Extern (Watchdog)'-Trades klar als manuelle
        # Bitunix-Trades kennzeichnen (Nutzer-Vorgabe: eindeutige Markierung).
        try:
            res = await self.db.auto_trades.update_many(
                {"strategy_id": "external", "manual_trade": {"$ne": True}},
                {"$set": {"strategy_name": "Manuell (Bitunix)", "manual_trade": True}})
            if getattr(res, "modified_count", 0):
                logger.info(f"Watchdog: {res.modified_count} Extern-Trade(s) als "
                            f"'Manuell (Bitunix)' markiert")
        except Exception as e:
            logger.debug(f"Watchdog: Manuell-Migration übersprungen: {e}")

    async def update_settings(self, updates: Dict) -> Dict:
        for key in ("enabled", "emergency_close", "adopt_unknown", "manage_external"):
            if key in updates:
                self.settings[key] = bool(updates[key])
        for key, lo, hi in (("interval_sec", 30, 3600), ("max_sl_retries", 1, 10)):
            if key in updates:
                try:
                    self.settings[key] = max(lo, min(hi, int(updates[key])))
                except (TypeError, ValueError):
                    pass
        if "fallback_sl_percent" in updates:
            try:
                self.settings["fallback_sl_percent"] = max(
                    0.1, min(10.0, float(updates["fallback_sl_percent"])))
            except (TypeError, ValueError):
                pass
        await self.db.settings.update_one({"_id": "position_watchdog"},
                                          {"$set": dict(self.settings)}, upsert=True)
        return dict(self.settings)

    async def clear_data(self) -> Dict:
        """Verlauf & Statistik löschen: Status-Report, Fail-Zähler und die vom
        Watchdog übernommenen 'Manuell (Bitunix)'-Trades."""
        deleted = 0
        try:
            res = await self.db.auto_trades.delete_many({"strategy_id": "external"})
            deleted = res.deleted_count
        except Exception as e:
            logger.warning(f"Watchdog: Extern-Trades löschen fehlgeschlagen: {e}")
        try:
            await self.db.settings.delete_one({"_id": "position_watchdog_status"})
        except Exception as e:
            logger.warning(f"Watchdog: Status löschen fehlgeschlagen: {e}")
        self._sl_fail.clear()
        logger.info(f"Watchdog-Daten gelöscht ({deleted} Extern-Trades entfernt)")
        return {"deleted_trades": deleted}

    def _reverse_map(self) -> Dict[str, str]:
        from core.instruments import SYMBOL_MAP
        return {v: k for k, v in SYMBOL_MAP.items()}

    async def _notify(self, text: str):
        try:
            from services import notifications
            await notifications.telegram_notify(self.db, self.telegram, "watchdog", text)
        except Exception as e:
            logger.warning(f"Watchdog notify failed: {e}")

    async def run_loop(self):
        logger.info("Positions-Watchdog gestartet "
                    f"(alle {self.settings.get('interval_sec', 120)}s)")
        while True:
            try:
                # Sichtbarkeits-Sync (adopt_unknown) läuft auch bei ausgeschaltetem
                # Watchdog weiter – nur das MANAGEMENT (SL/Notfall-Close) ist dann aus.
                # Bug-Report: manuelle Bitunix-Trades verschwanden von der Website,
                # sobald der Watchdog deaktiviert war.
                manage = self.settings.get("enabled", True)
                adopt = self.settings.get("adopt_unknown", True)
                if (manage or adopt) and self.client and self.client.configured():
                    await self.check(manage=manage)
            except Exception as e:
                logger.error(f"Positions-Watchdog Fehler: {e}")
            await asyncio.sleep(max(30, int(self.settings.get("interval_sec", 120))))

    async def check(self, manage: bool = True) -> Dict:
        """Ein kompletter Prüf-Zyklus. Gibt den Status-Report zurück.
        manage=False: nur Sichtbarkeits-Sync (unbekannte Positionen übernehmen),
        kein SL-/Dust-/Notfall-Management."""
        status = {"last_run_at": datetime.now(timezone.utc).isoformat(),
                  "positions": 0, "adopted": 0, "sl_fixed": 0, "sl_missing": 0,
                  "emergency_closed": 0, "dust_closed": 0, "errors": [],
                  "mode": "full" if manage else "sync-only"}
        if not (self.client and self.client.configured()):
            status["errors"].append("Bitunix nicht konfiguriert")
            await self._record(status)
            return status
        try:
            res = await self.client.get_positions()
        except Exception as e:
            status["errors"].append(f"get_positions: {str(e)[:120]}")
            await self._record(status)
            return status
        positions = parse_positions(res)
        status["positions"] = len(positions)
        seen = set()
        for pos in positions:
            seen.add(pos["position_id"])
            try:
                await self._check_position(pos, status, manage=manage)
            except Exception as e:
                logger.error(f"Watchdog check {pos['bitunix_symbol']} fehlgeschlagen: {e}")
                status["errors"].append(f"{pos['bitunix_symbol']}: {str(e)[:120]}")
        # Fail-Zähler verschwundener Positionen aufräumen
        for pid in list(self._sl_fail):
            if pid not in seen:
                self._sl_fail.pop(pid, None)
        await self._record(status)
        return status

    async def _find_local(self, internal: str, pos: Dict) -> Optional[Dict]:
        """Lokalen Website-Trade zur Börsen-Position finden – so präzise wie
        möglich: erst über die Bitunix-Position-ID, dann Symbol+Seite
        (Website-Trades vor Extern-Übernahmen)."""
        q_base = {"status": "open", "mode": "live"}
        local = await self.db.auto_trades.find_one(
            {**q_base, "bitunix_position_id": pos["position_id"]})
        if local is None:
            local = await self.db.auto_trades.find_one(
                {**q_base, "symbol": internal, "side": pos["side"],
                 "external_adopted": {"$ne": True},
                 "strategy_id": {"$ne": "external"}})
        if local is None:
            local = await self.db.auto_trades.find_one(
                {**q_base, "symbol": internal, "side": pos["side"]})
        return local

    async def _check_position(self, pos: Dict, status: Dict, manage: bool = True):
        internal = self._reverse_map().get(pos["bitunix_symbol"], pos["bitunix_symbol"])
        local = await self._find_local(internal, pos)
        # Manuelle Bitunix-Trades (nicht über die Website eröffnet) werden NICHT
        # gemanagt: nur sichtbar machen, dann Finger weg (kein Dust-Close, kein
        # SL-Zwang, kein Notfall-Close) – außer manage_external ist explizit an.
        is_external = local is None or bool(local.get("external_adopted")) \
            or local.get("strategy_id") == "external"
        # Misch-Schutz: Börsen-Position DEUTLICH größer als der Website-Trade
        # -> der Trader hat manuell aufgestockt (Börse führt beides zusammen).
        # Dann NICHT anfassen, sonst würde der Watchdog den manuellen Anteil
        # mit-managen (SL setzen / schließen).
        if not is_external and local is not None:
            local_qty = _f(local.get("qty_remaining") or local.get("qty"))
            if local_qty > 0 and pos["qty"] > local_qty * 1.05:
                is_external = True
                logger.info(
                    f"Watchdog: {internal} {pos['side']} enthält manuellen Anteil "
                    f"(Börse {pos['qty']} > Website {local_qty}) – wird NICHT gemanagt")
        if local is None and self.settings.get("adopt_unknown", True):
            local = await self._adopt(internal, pos)
            if local:
                status["adopted"] += 1
        if not manage:
            # Watchdog aus: nur sichtbar machen, keinerlei Eingriffe an der Börse
            self._sl_fail.pop(pos["position_id"], None)
            return
        if is_external and not self.settings.get("manage_external", False):
            self._sl_fail.pop(pos["position_id"], None)
            return
        # Dust-Positionen (unter dem Börsen-Minimum, meist verwaiste Cent-Reste):
        # keinen SL managen, sondern versuchen zu schließen (nur Website-Trades).
        try:
            min_qty = float((self.client.contract_meta(pos["bitunix_symbol"]) or {})
                            .get("min_qty") or 0)
        except Exception:
            min_qty = 0.0
        if min_qty > 0 and pos["qty"] < min_qty:
            try:
                res = await self.client.flash_close(internal, pos["position_id"],
                                                    pos["side"], pos["qty"])
                if isinstance(res, dict) and res.get("code") == 0:
                    status["dust_closed"] += 1
                    self._sl_fail.pop(pos["position_id"], None)
                    logger.info(f"Watchdog: Dust-Position {internal} {pos['side']} "
                                f"(qty {pos['qty']} < min {min_qty}) geschlossen")
                    await self._notify(
                        f"🧹 *WATCHDOG*\n{internal} {pos['side']}: verwaiste "
                        f"Rest-Position (Menge {pos['qty']}, unter Börsen-Minimum) "
                        f"wurde aufgeräumt.")
                else:
                    logger.info(f"Watchdog: Dust-Position {internal} nicht schließbar "
                                f"(unter Minimum) – wird ignoriert: {res}")
            except Exception as e:
                logger.info(f"Watchdog: Dust-Close {internal} fehlgeschlagen: {e}")
            return
        has_sl = await self.autotrader._position_has_sl(internal, pos["position_id"])
        if has_sl is None:
            return  # API unsicher -> kein Eingriff (kein falscher Notfall-Close)
        if has_sl:
            self._sl_fail.pop(pos["position_id"], None)
            if local and local.get("sl_exchange_missing"):
                await self.db.auto_trades.update_one(
                    {"id": local["id"]}, {"$set": {"sl_exchange_missing": False}})
            return
        status["sl_missing"] += 1
        mark = _f(await self.client.get_mark_price(internal))
        sl = emergency_sl(pos["side"], pos["entry"], mark,
                          self.settings.get("fallback_sl_percent", 2.0),
                          local_sl=(local or {}).get("sl"))
        placed = False
        if sl:
            try:
                res = await self.client.place_position_tp_sl(
                    internal, pos["position_id"], pos["side"], sl_price=sl)
                placed = isinstance(res, dict) and res.get("code") == 0
                if placed:
                    # Verifikation: nur bei klarem "kein SL" als Fehlschlag werten
                    placed = (await self.autotrader._position_has_sl(
                        internal, pos["position_id"])) is not False
            except Exception as e:
                logger.warning(f"Watchdog SL-Platzierung {internal} fehlgeschlagen: {e}")
        if placed:
            status["sl_fixed"] += 1
            self._sl_fail.pop(pos["position_id"], None)
            if local:
                await self.db.auto_trades.update_one({"id": local["id"]}, {"$set": {
                    "sl": sl, "sl_exchange_missing": False,
                    "events": (local.get("events", []) +
                               [f"WATCHDOG: fehlenden Börsen-SL nachgezogen @ {sl}"])[-20:]}})
            logger.warning(f"Watchdog: fehlenden SL für {internal} {pos['side']} "
                           f"nachgezogen @ {sl}")
            await self._notify(f"🛡️ *WATCHDOG*\n{internal} {pos['side']}: fehlender "
                               f"Stop-Loss wurde nachgezogen (`{sl}`).")
            return
        fails = self._sl_fail.get(pos["position_id"], 0) + 1
        self._sl_fail[pos["position_id"]] = fails
        max_retries = int(self.settings.get("max_sl_retries", 3))
        logger.error(f"Watchdog: SL für {internal} {pos['side']} fehlt weiterhin "
                     f"(Zyklus {fails}/{max_retries})")
        if fails < max_retries or not self.settings.get("emergency_close", True):
            if fails == 1:
                await self._notify(
                    f"⚠️ *WATCHDOG*\n{internal} {pos['side']}: Position hat KEINEN "
                    f"Stop-Loss und er konnte nicht gesetzt werden "
                    f"(Zyklus {fails}/{max_retries}).")
            return
        await self._emergency_close(internal, pos, local, status, fails)

    async def _emergency_close(self, internal: str, pos: Dict,
                               local: Optional[Dict], status: Dict, fails: int):
        try:
            res = await self.client.flash_close(internal, pos["position_id"],
                                                pos["side"], pos["qty"])
            closed = isinstance(res, dict) and res.get("code") == 0
        except Exception as e:
            closed = False
            logger.error(f"Watchdog Notfall-Close {internal} fehlgeschlagen: {e}")
        if closed:
            status["emergency_closed"] += 1
            self._sl_fail.pop(pos["position_id"], None)
            logger.error(f"Watchdog: NOTFALL-CLOSE {internal} {pos['side']} "
                         f"(SL nach {fails} Zyklen nicht setzbar)")
            await self._notify(
                f"⛔ *WATCHDOG NOTFALL-CLOSE*\n{internal} {pos['side']}: Stop-Loss "
                f"konnte nach {fails} Versuchen nicht gesetzt werden – Position "
                f"wurde zur Sicherheit geschlossen.")
            if local:
                try:
                    await self.autotrader._book_external_close(local)
                except Exception as e:
                    logger.warning(f"Watchdog: lokales Verbuchen fehlgeschlagen: {e}")
        else:
            await self._notify(
                f"🚨 *WATCHDOG ALARM*\n{internal} {pos['side']}: KEIN Stop-Loss und "
                f"Notfall-Close FEHLGESCHLAGEN – bitte SOFORT manuell in Bitunix prüfen!")

    async def _adopt(self, internal: str, pos: Dict) -> Optional[Dict]:
        """Unbekannte Börsen-Position als sichtbaren 'Extern'-Trade übernehmen."""
        entry = pos["entry"] or _f(await self.client.get_mark_price(internal))
        if entry <= 0:
            return None
        now_iso = datetime.now(timezone.utc).isoformat()
        pct = max(abs(_f(self.settings.get("fallback_sl_percent", 2.0))), 0.1) / 100
        sl_guess = round(entry * (1 - pct) if pos["side"] == "LONG"
                         else entry * (1 + pct), 8)
        trade = {
            "id": f"{internal}-ext-{int(time.time() * 1000)}",
            "symbol": internal, "side": pos["side"], "mode": "live",
            "entry": entry, "sl": sl_guess, "tp1": None, "tpf": None,
            "initial_sl": sl_guess, "liq_price": None, "liquidated": False,
            "atr": 0, "qty": pos["qty"], "qty_remaining": pos["qty"],
            "risk": round(abs(entry - sl_guess), 8),
            "leverage": pos.get("leverage") or 0,
            "max_capital": round(pos.get("margin") or 0, 6),
            "status": "open", "tp1_hit": False, "breakeven_moved": False,
            "realized_pnl": 0.0, "fees_paid": 0.0, "fee_percent": 0.06,
            "strategy_id": "external", "strategy_name": "Manuell (Bitunix)",
            "manual_trade": True,
            "external_adopted": True, "sl_exchange_missing": False,
            "bitunix_order_id": None, "bitunix_position_id": pos["position_id"],
            "bitunix_tpsl_order_id": None, "tp1_exchange_placed": False,
            "opened_at": now_iso, "trade_date": now_iso[:10],
            "events": [f"WATCHDOG: Börsen-Position ohne lokalen Trade übernommen "
                       f"(Menge {pos['qty']} @ {entry})"],
        }
        await self.db.auto_trades.insert_one(dict(trade))
        logger.warning(f"Watchdog: unbekannte Börsen-Position übernommen: "
                       f"{internal} {pos['side']} qty={pos['qty']}")
        await self._notify(
            f"👁️ *WATCHDOG*\n{internal} {pos['side']}: manuell eröffnete Bitunix-Position "
            f"entdeckt (Menge {pos['qty']}, Entry `{entry}`). Sie ist jetzt als "
            f"'Manuell (Bitunix)' auf der Website sichtbar und wird NICHT angefasst.")
        return trade

    async def _record(self, status: Dict):
        try:
            await self.db.settings.update_one(
                {"_id": "position_watchdog_status"},
                {"$set": {**status, "errors": status.get("errors", [])[:5]}},
                upsert=True)
        except Exception as e:
            logger.debug(f"Watchdog-Status nicht gespeichert: {e}")


watchdog = PositionWatchdog()
