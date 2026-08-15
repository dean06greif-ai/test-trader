"""Belohnungssystem (Reward-Score) für den KI Trader.

Jeder geschlossene KI-Trade wird nach transparenten, deterministischen Regeln
bewertet (belohnt/bestraft) und in `ai_rewards` gespeichert:
  + PnL-Basis      : Gewinn/Verlust relativ zur eingesetzten Margin
  + Ergebnis       : Win-Bonus / Loss-Malus / Breakeven
  - Sofort-Stop-Out: Verlust-Trade unter 15 Minuten Haltedauer
  ± Konfidenz      : Verlust unter 80% Konfidenz wird bestraft, disziplinierte
                     Gewinne (>=80%) belohnt
  + CRV-Disziplin  : geplantes CRV >= 2.0 in der Entscheidung

Der Reward-Verlauf und die Auswertung pro Markt-Regime fließen als eigener
Prompt-Block in jeden Lernlauf ein (services/ai_learning.py) – so lernt die
KI direkt an ihrer eigenen Belohnungskurve. Frontend: Lern-Panel (AIRewardPanel).
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_backfill_ts = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_reward(trade: Dict, decision: Optional[Dict] = None) -> Dict:
    """Reiner, testbarer Reward für einen geschlossenen Trade."""
    pnl = float(trade.get("realized_pnl") or 0)
    cap = float(trade.get("max_capital") or 0)
    pnl_pct = (pnl / cap * 100) if cap > 0 else 0.0
    comps: List[Dict] = []

    comps.append({"label": "PnL-Basis", "value": round(_clamp(pnl_pct / 2.5, -4.0, 4.0), 3)})

    result = trade.get("result")
    if result == "win":
        comps.append({"label": "Gewinn-Bonus", "value": 1.0})
    elif result == "loss":
        comps.append({"label": "Verlust-Malus", "value": -1.0})
    else:
        comps.append({"label": "Breakeven", "value": 0.2})

    dur_min = None
    try:
        o = datetime.fromisoformat(str(trade.get("opened_at")).replace("Z", "+00:00"))
        c = datetime.fromisoformat(str(trade.get("closed_at")).replace("Z", "+00:00"))
        dur_min = (c - o).total_seconds() / 60
    except (TypeError, ValueError):
        pass
    if dur_min is not None and dur_min < 15 and result == "loss":
        comps.append({"label": "Sofort-Stop-Out (<15 min)", "value": -0.5})

    conf = (decision or {}).get("confidence")
    if conf is not None:
        if result == "loss" and conf < 80:
            comps.append({"label": f"Verlust bei Konfidenz {conf}% (<80%)", "value": -0.5})
        elif result == "win" and conf >= 80:
            comps.append({"label": f"Disziplin-Bonus (Konfidenz {conf}%)", "value": 0.25})

    try:
        sl = float((decision or {}).get("sl_pct") or 0)
        tp = float((decision or {}).get("tp1_pct") or 0)
        if sl > 0 and tp / sl >= 2.0:
            comps.append({"label": f"CRV-Disziplin (geplant {round(tp / sl, 2)})", "value": 0.25})
    except (TypeError, ValueError):
        pass

    # Fee-Feedback (bewusst KEIN eigener Malus – reine Transparenz, damit die
    # KI selbst lernt, weitere Stops zu wählen): Wie viel % des Verlusts waren
    # reine Gebühren? realized_pnl ist inkl. Fees -> Anteil = fees / |pnl|.
    fees = float(trade.get("fees_paid") or 0)
    fee_share = None
    if result == "loss" and pnl < 0 and fees > 0:
        fee_share = round(min(100.0, fees / abs(pnl) * 100), 1)

    return {"score": round(sum(c["value"] for c in comps), 3),
            "components": comps,
            "pnl_pct": round(pnl_pct, 3),
            "fees": round(fees, 6),
            "fee_share_pct": fee_share,
            "duration_min": round(dur_min, 1) if dur_min is not None else None}


async def _regime_for(db, symbol: str, trade: Dict) -> Optional[str]:
    """Markt-Regime zum Trade. Prio (Fix ai_rewards-RCA + P1 Tech-Debt):
    1) Entry-Regime direkt vom Trade (entry_market_snapshot, Fix 0.2) –
       das ML-relevante Regime im Entscheidungs-Moment,
    2) historischer 15-min-Snapshot <= closed_at (korrekt für Backfill),
    3) Live-Beobachter (nur Notnagel für frisch geschlossene Trades)."""
    try:
        r = ((trade.get("entry_market_snapshot") or {}).get("features")
             or {}).get("regime")
        if r:
            return str(r)
    except Exception:
        pass
    try:
        ref = trade.get("closed_at") or _now_iso()
        snap = await db.ai_market_snapshots.find(
            {"symbol": symbol, "ts": {"$lte": ref}}).sort("ts", -1).limit(1).to_list(1)
        if snap:
            r = (snap[0].get("features") or {}).get("regime")
            if r:
                return str(r)
    except Exception:
        pass
    try:
        from services.ai_market_observer import market_observer
        r = (market_observer.features_for(symbol) or {}).get("regime")
        if r:
            return str(r)
    except Exception:
        pass
    return None


async def on_trade_closed(db, trade: Dict) -> Optional[Dict]:
    """Hook nach jedem geschlossenen KI-Trade: Reward berechnen + speichern."""
    if trade.get("strategy_id") != "ai_trader" or trade.get("status") != "closed":
        return None
    try:
        if trade.get("id") and await db.ai_rewards.find_one({"trade_id": trade["id"]}):
            return None
        decision = None
        if trade.get("signal_id"):
            decision = await db.ai_decisions.find_one({"signal_id": trade["signal_id"]})
        r = compute_reward(trade, decision)
        doc = {"id": uuid.uuid4().hex[:12], "trade_id": trade.get("id"),
               "symbol": trade.get("symbol"), "side": trade.get("side"),
               "mode": trade.get("mode"), "result": trade.get("result"),
               "regime": await _regime_for(db, trade.get("symbol"), trade),
               "pnl": float(trade.get("realized_pnl") or 0),
               **r, "ts": trade.get("closed_at") or _now_iso()}
        await db.ai_rewards.insert_one(dict(doc))
        doc.pop("_id", None)
        return doc
    except Exception as e:
        logger.warning(f"Reward-Berechnung fehlgeschlagen: {e}")
        return None


async def backfill_missing(db, include_cleared: bool = False) -> int:
    """Lückenfüllender Backfill (RCA 'ai_rewards leer', 2026-08-13): bewertet
    ALLE geschlossenen KI-Trades, die noch keinen Reward-Eintrag haben –
    idempotent (Dedupe über trade_id in on_trade_closed). Repariert damit auch
    einzelne Hook-Ausfälle statt nur den 'Collection komplett leer'-Fall.

    Ein bewusstes 'Belohnungsdaten löschen' des Traders (cleared_at) wird
    respektiert: nur Trades mit closed_at NACH cleared_at werden nachbewertet.
    include_cleared=True hebt die Löschung auf (cleared_at wird entfernt) und
    bewertet auch die historischen Trades neu (Admin-Endpoint)."""
    st = await db.settings.find_one({"_id": "ai_rewards_state"}) or {}
    cleared_at = st.get("cleared_at")
    if include_cleared and cleared_at:
        await db.settings.update_one({"_id": "ai_rewards_state"},
                                     {"$unset": {"cleared_at": ""}})
        logger.info("Reward-Backfill: cleared_at aufgehoben (include_cleared)")
        cleared_at = None
    flt = {"strategy_id": "ai_trader", "status": "closed"}
    if cleared_at:
        flt["closed_at"] = {"$gt": cleared_at}
    have = {r.get("trade_id") async for r in
            db.ai_rewards.find({}, {"trade_id": 1, "_id": 0})}
    trades = await db.auto_trades.find(flt).sort("closed_at", 1).to_list(1000)
    n = 0
    for t in trades:
        if t.get("id") in have:
            continue
        if await on_trade_closed(db, t):
            n += 1
    if n:
        logger.info(f"Reward-Backfill: {n} geschlossene KI-Trades bewertet")
    return n


async def ensure_backfill(db) -> int:
    """Periodischer Lücken-Check (max. alle 10 Minuten), respektiert cleared_at."""
    global _backfill_ts
    import time as _t
    if _t.time() - _backfill_ts < 600:
        return 0
    _backfill_ts = _t.time()
    try:
        return await backfill_missing(db, include_cleared=False)
    except Exception as e:
        logger.warning(f"Reward-Backfill fehlgeschlagen: {e}")
        return 0


async def clear(db) -> int:
    """Alle Belohnungsdaten löschen. Historische Trades vor dem Löschzeitpunkt
    werden danach NICHT mehr auto-backfilled (cleared_at); neue Trades werden
    weiterhin normal bewertet. Aufhebbar via POST /api/ai/rewards/backfill
    mit include_cleared=true."""
    res = await db.ai_rewards.delete_many({})
    await db.settings.update_one({"_id": "ai_rewards_state"},
                                 {"$set": {"cleared_at": _now_iso()}}, upsert=True)
    logger.info(f"Belohnungssystem: {res.deleted_count} Reward-Einträge gelöscht")
    return res.deleted_count


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, min(365, days)))).isoformat()


async def history(db, days: int = 30) -> List[Dict]:
    """Reward-Verlauf (chronologisch, mit kumulierter Kurve)."""
    rows = await db.ai_rewards.find({"ts": {"$gte": _cutoff(days)}}) \
        .sort("ts", 1).to_list(3000)
    out, cum = [], 0.0
    for r in rows:
        cum = round(cum + float(r.get("score") or 0), 3)
        out.append({"ts": r.get("ts"), "score": r.get("score"), "cum": cum,
                    "symbol": r.get("symbol"), "side": r.get("side"),
                    "mode": r.get("mode"), "pnl": r.get("pnl"),
                    "fees": r.get("fees"), "fee_share_pct": r.get("fee_share_pct"),
                    "regime": r.get("regime"), "result": r.get("result")})
    return out


async def by_regime(db, days: int = 30) -> List[Dict]:
    """Reward-Auswertung pro Markt-Regime (datenbasis für Regime-Lektionen)."""
    rows = await db.ai_rewards.find({"ts": {"$gte": _cutoff(days)}}).to_list(3000)
    agg: Dict[str, Dict] = {}
    for r in rows:
        k = str(r.get("regime") or "unbekannt")
        d = agg.setdefault(k, {"regime": k, "trades": 0, "reward_sum": 0.0,
                               "wins": 0, "pnl": 0.0})
        d["trades"] += 1
        d["reward_sum"] += float(r.get("score") or 0)
        d["pnl"] += float(r.get("pnl") or 0)
        if r.get("result") == "win":
            d["wins"] += 1
    out = []
    for d in agg.values():
        d["avg_reward"] = round(d["reward_sum"] / d["trades"], 3)
        d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1)
        d["reward_sum"] = round(d["reward_sum"], 3)
        d["pnl"] = round(d["pnl"], 2)
        out.append(d)
    return sorted(out, key=lambda x: -x["avg_reward"])


async def summary(db, days: int = 30) -> Dict:
    rows = await history(db, days)
    n = len(rows)
    total = round(sum(float(r.get("score") or 0) for r in rows), 3)
    last10 = [float(r["score"] or 0) for r in rows[-10:]]
    prev10 = [float(r["score"] or 0) for r in rows[-20:-10]]
    trend = None
    if last10 and prev10:
        trend = round(sum(last10) / len(last10) - sum(prev10) / len(prev10), 3)
    return {"trades": n, "total": total,
            "avg": round(total / n, 3) if n else 0.0,
            "trend": trend, "days": days}


def _top_penalties(rows: List[Dict], limit: int = 4) -> List[str]:
    counts: Dict[str, int] = {}
    for r in rows:
        for c in r.get("components") or []:
            if float(c.get("value") or 0) < 0:
                # Konfidenz-Label vereinheitlichen, damit gezählt werden kann
                label = str(c.get("label", ""))
                if label.startswith("Verlust bei Konfidenz"):
                    label = "Verlust bei Konfidenz <80%"
                counts[label] = counts.get(label, 0) + 1
    return [f"{k} ({v}×)" for k, v in
            sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


async def context_text(db, days: int = 14) -> str:
    """Kompakter Prompt-Block für Lernläufe (leerer String, wenn keine Daten)."""
    await ensure_backfill(db)
    rows = await db.ai_rewards.find({"ts": {"$gte": _cutoff(days)}}) \
        .sort("ts", 1).to_list(3000)
    if not rows:
        return ""
    n = len(rows)
    total = sum(float(r.get("score") or 0) for r in rows)
    last10 = [float(r.get("score") or 0) for r in rows[-10:]]
    prev10 = [float(r.get("score") or 0) for r in rows[-20:-10]]
    trend_txt = ""
    if last10 and prev10:
        diff = sum(last10) / len(last10) - sum(prev10) / len(prev10)
        trend_txt = (f" | Trend: {'BESSER' if diff > 0.1 else ('SCHLECHTER' if diff < -0.1 else 'stabil')} "
                     f"({diff:+.2f} Ø-Reward, letzte 10 vs. 10 davor)")
    lines = [
        "=== BELOHNUNGSSYSTEM (Reward-Score deiner geschlossenen Trades) ===",
        "Jeder Trade wird belohnt/bestraft: PnL-Höhe, Win/Loss, Sofort-Stop-Outs (<15 min), "
        "Konfidenz-Disziplin (<80% verloren = Malus) und CRV-Planung (>=2.0 = Bonus). "
        "Dein Ziel ist es, den kumulierten Reward zu MAXIMIEREN – leite Lektionen ab, "
        "die die häufigsten Malus-Gründe eliminieren.",
        f"Gesamt-Reward ({days} Tage): {total:+.2f} über {n} Trades "
        f"(Ø {total / n:+.2f}/Trade){trend_txt}",
    ]
    regimes = await by_regime(db, days)
    if regimes:
        reg_parts = [f"{d['regime']}: Ø {d['avg_reward']:+.2f} ({d['trades']} Trades, "
                     f"Winrate {d['win_rate']}%)" for d in regimes[:6]]
        lines.append("Reward pro Markt-Regime: " + " | ".join(reg_parts))
        best, worst = regimes[0], regimes[-1]
        if best["regime"] != worst["regime"]:
            lines.append(f"Bestes Regime: {best['regime']} (Ø {best['avg_reward']:+.2f}) | "
                         f"Schwächstes: {worst['regime']} (Ø {worst['avg_reward']:+.2f})")
    penalties = _top_penalties(rows)
    if penalties:
        lines.append("Häufigste Malus-Gründe: " + " | ".join(penalties))
    # Fee-Feedback: zeigt der KI explizit, welcher Anteil ihrer Verluste reine
    # Gebühren waren – sie soll daraus SELBST lernen (keine Stil-Vorgabe).
    shares = [float(r["fee_share_pct"]) for r in rows
              if r.get("result") == "loss" and r.get("fee_share_pct") is not None]
    if shares:
        avg_share = sum(shares) / len(shares)
        fee_dom = sum(1 for s in shares if s >= 50)
        lines.append(
            f"GEBÜHREN-ANTEIL an Verlusten: Ø {avg_share:.0f}% des Verlusts waren reine "
            f"Roundtrip-Fees; bei {fee_dom} von {len(shares)} Verlusten machten Fees >=50% aus. "
            "Ein hoher Anteil heißt: Der Trade verlor an die Gebühren (zu enger Stop und/oder "
            "zu großes Notional), nicht an den Markt – wähle dann von dir aus weitere "
            "SL-Distanzen oder kleinere Positionen.")
    return "\n".join(lines)
