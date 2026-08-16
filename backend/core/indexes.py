"""MongoDB-Indizes an EINER Stelle (idempotent, beim Start aufgerufen).

Ohne diese Indizes muss MongoDB für jede Verlaufs-Abfrage die komplette
Collection scannen und im RAM sortieren – im Live-Betrieb der Grund für das
langsame Laden des KI-Verlaufs. `create_index` ist idempotent, ein Fehler
(z.B. fehlende Rechte) darf den Start nie blockieren.
"""
import logging

logger = logging.getLogger(__name__)

# (collection, keys, name)
INDEX_SPECS = [
    ("ai_chat", [("ts", -1)], "ai_chat_ts"),
    ("ai_chat", [("role", 1), ("ts", -1)], "ai_chat_role_ts"),
    ("ai_chat", [("role", 1), ("pinned", 1), ("ts", -1)], "ai_chat_role_pinned_ts"),
    ("ai_proposals", [("status", 1), ("ts", -1)], "ai_proposals_status_ts"),
    ("ai_ghost_trades", [("candidate_id", 1), ("ts", -1)], "ghost_cid_ts"),
    ("signals", [("timestamp", -1)], "signals_ts"),
    ("signals", [("symbol", 1), ("strategy_id", 1), ("timestamp", -1)], "signals_sym_strat_ts"),
    ("signals", [("strategy_id", 1), ("timestamp", -1)], "signals_strat_ts"),
    ("auto_trades", [("opened_at", -1)], "trades_opened"),
    ("auto_trades", [("status", 1), ("closed_at", -1)], "trades_status_closed"),
    ("auto_trades", [("symbol", 1), ("strategy_id", 1), ("opened_at", -1)], "trades_sym_strat"),
    ("ai_market_snapshots", [("ts", 1)], "snapshots_ts"),
    # Gate v1 Shadow-Report + Outcome-Sync (P2-Backlog "ai_decisions ohne Index")
    ("ai_decisions", [("ts", -1)], "decisions_ts"),
    ("ai_decisions", [("outcome", 1), ("ts", -1)], "decisions_outcome_ts"),
    ("analytics_daily", [("date", -1)], "analytics_daily_date"),
    ("trade_stats", [("date", -1)], "trade_stats_date"),
    ("audit_log", [("ts", -1)], "audit_ts"),
    ("fee_guard_blocks", [("ts", -1)], "fee_guard_ts"),
]


async def ensure_indexes(db) -> int:
    """Legt alle Indizes an. Gibt die Anzahl erfolgreicher Aufrufe zurück."""
    ok = 0
    for coll, keys, name in INDEX_SPECS:
        try:
            await db[coll].create_index(keys, name=name, background=True)
            ok += 1
        except Exception as e:  # noqa: BLE001 – Start darf nie scheitern
            # Code 85 (IndexOptionsConflict): identischer Index existiert bereits
            # unter anderem Namen (z.B. ts_-1 statt ai_chat_ts) – funktional
            # gleichwertig, zählt als OK statt Warnung (Log-Rauschen auf Render).
            if getattr(e, "code", None) == 85 or "IndexOptionsConflict" in str(e):
                ok += 1
                logger.info(f"index {coll}.{name}: existiert bereits unter anderem Namen – ok")
            else:
                logger.warning(f"index {coll}.{name} failed: {e}")
    logger.info(f"MongoDB-Indizes geprüft/angelegt: {ok}/{len(INDEX_SPECS)}")
    return ok
