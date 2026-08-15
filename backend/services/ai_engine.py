"""
AI Trading Engine ("KI Trader")
- Periodically sends multi-timeframe market snapshots + crypto news + user chat
  directives to a configurable LLM (Gemini, Groq, OpenRouter/Grok, Mistral).
- The LLM returns structured trade decisions (LONG/SHORT/HOLD + confidence +
  SL/TP suggestions + reasoning). Actionable decisions are emitted as signals
  through the normal signal/auto-trade pipeline (strategy_id "ai_trader").
- Provides a multi-turn chat so the user can give the AI instructions
  ("achte auf BTC-Support bei 60k") that flow into the next analysis.

Provider (alle kostenlos in ihren Free-Tiers, deploybar auf Render):
  - Google Gemini      -> GEMINI_API_KEY  (google-genai SDK)
  - Groq (Llama, Qwen) -> GROQ_API_KEY    (OpenAI-kompatibel)
  - OpenRouter (Grok, DeepSeek, Llama Free) -> OPENROUTER_API_KEY
  - Mistral            -> MISTRAL_API_KEY (OpenAI-kompatibel)

Der Fallback bei Rate-Limit bleibt innerhalb des ausgewählten Providers.
"""
import os
import json
import re
import time
import uuid
import hashlib
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback (nicht relevant für Render, aber safe)
    from backports.zoneinfo import ZoneInfo  # type: ignore

from dotenv import load_dotenv
load_dotenv()

from core import timeutil
from services.timeframes import aggregate_candles
from services import session_levels
from services import range_analysis
from services.technical_indicators import TechnicalIndicators
from services.news_feed import news_feed
from services import macro_context
from services import liquidity_data
from services import liquidity_levels
from services.ai_knowledge import PLATFORM_KNOWLEDGE, tunable_spec_text, validate_changes
from services.ai_master_prompt import master_prompt
from services.ai_strategy_lab import strategy_lab
from services import ai_schedule
from services import ai_validation
from services.ai_validation import validation_gate
from services import ai_providers
from services import ai_playbook
from services.ai_roles import role_manager
from services.ai_json import parse_json_lenient

logger = logging.getLogger(__name__)

BERLIN_TZ = ZoneInfo("Europe/Berlin")

SUMMARY_SYSTEM = (
    "Du bist der 'KI Trader'. Fasse den abgelaufenen Trading-Tag prägnant auf Deutsch zusammen. "
    "Antworte AUSSCHLIESSLICH mit reinem Text (kein JSON, kein Markdown-Codeblock). "
    "Struktur (kompakt, max. 12 Zeilen):\n"
    "• Tages-Marktüberblick (2-3 Sätze)\n"
    "• Wichtigste Eckdaten: Anzahl Analysen, ausgelöste Signale, Trade-Entscheidungen (LONG/SHORT/HOLD)\n"
    "• Trader-Direktiven (die vom Nutzer selbst definierten Anweisungen, die die Handelsentscheidungen aktuell steuern)\n"
    "• Aktive Konfiguration (Provider/Modell, Intervall, Min. Konfidenz, Cooldown)\n"
    "Sei nüchtern und ohne Floskeln. Nutze ausschließlich die übergebenen Fakten."
)

DEFAULT_AI_CONFIG = {
    "enabled": False,
    "interval_min": 10,
    # Zeitplan der regelmäßigen Analyse: Fenster mit eigenem Intervall,
    # z.B. nachts alle 30 min, 15-18 Uhr alle 5 min (services/ai_schedule.py).
    "schedule": [],
    "min_confidence": 65,
    "provider": "gemini",
    "model": "gemini-3.5-flash",
    "news_enabled": True,
    # Externer Makro-Kontext (Key-Levels, Funding/OI, Makro-Kalender, DXY/Yield,
    # BTC-Dominanz, Trump/Truth-Social) — pro Analyse-Zyklus über get_macro_context().
    "macro_enabled": True,
    # Coins, für die pro Zyklus Key-Levels + Funding/OI geholt werden (kompakt ~2 KB).
    "macro_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    # Liquiditäts-/Liquidations-Kontext (Eigenbau-Heatmap, Orderbook-Wände,
    # Long/Short-Ratio, OI-Trend, eigene "Liquidity Levels") – frei & keyless.
    "liquidity_enabled": True,
    "liquidity_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    # Feinsteuerung der Liquiditäts-Daten:
    # use_liquidation_data = ECHTE Daten (Long/Short-Ratio, OI, Orderbook-Wände,
    #   Live-Liquidationen der Börsen) – Standard AN.
    # use_heatmap_data = MODELLIERTE Liq-Cluster (reine Formel Preis ± 1/Hebel,
    #   KEINE gemessenen Daten) – Standard AUS: sie existieren rechnerisch für
    #   jeden Coin und haben die KI in Fade-Trades an erfundenen Levels gelockt.
    "use_liquidation_data": True,
    "use_heatmap_data": False,
    "cooldown_min": 45,
    # ---- Autonomie-Leitplanken (Self-Tuning-Guard): Spanne, in der die KI
    # ihre eigenen Engine-Werte (min_confidence, cooldown_min) bei
    # autonomy=auto selbst anwenden darf. Außerhalb wird die Änderung nur
    # Vorschlag (needs_confirmation). Die Spanne selbst darf NUR der Trader
    # ändern (nicht in der KI-Whitelist). Hintergrund: RCA 14.08. – die KI
    # hatte min_confidence per Self-Tuning auf 85 geschraubt und sich damit
    # selbst stranguliert (Prompts kalibrieren A-Setups auf 70–85).
    "tune_conf_min": 55,
    "tune_conf_max": 75,
    "tune_cooldown_max": 45,
    # ---- Datensammel-Modus (Phase 4): Entscheidungen unterhalb der
    # Live-Schwelle (aber >= collection_min_confidence) werden als PAPER-
    # Trades ausgeführt und mit data_collection=true markiert – nie live,
    # kein Kapital, keine Telegram-Meldungen. Ziel: deutlich mehr gelabelte
    # Trades für das ML-Training (separat gewichtet, keine Vermischung).
    "collection_enabled": True,
    "collection_min_confidence": 60,
    "collection_cooldown_min": 30,
    "collection_max_same_direction": 5,
    "collection_max_per_coin": 2,
    # ---- Fee-Wächter: Physik-Grenze statt Stil-Vorgabe. Ein KI-Trade wird
    # nur eröffnet, wenn seine SL-Distanz mind. fee_guard_mult × Roundtrip-
    # Fees (2 × fee_percent des Coins, Standard 0,12%) beträgt. Blockt nur
    # mathematisch garantierte Fee-Verlierer – Scalpen bleibt sonst frei.
    # Gilt für alle KI-Trades inkl. Sammel-Trades; NICHT in der KI-Whitelist.
    "fee_guard_enabled": True,
    "fee_guard_mult": 4.0,
    # Max. gleichzeitig offene KI-Trader-Trades pro Coin (1–5). Default 1 =
    # bisheriges Verhalten (strikt ein Trade pro Coin). Nur der KI-Trader nutzt
    # dieses Limit; alle anderen Strategien bleiben bei strikt 1 Trade pro Coin.
    "max_trades_per_coin": 1,
    # Max. Kapital (USDT Margin) pro KI-Trade. 0 = aus -> Coin-Trade-Settings
    # gelten wie bisher. Wenn > 0, entscheidet die KI PRO TRADE selbst, wie viel
    # Kapital (10-100% dieses Betrags) sie einsetzt – nicht automatisch immer das Maximum.
    "max_capital_per_trade": 0,
    # Einstellungs-Autonomie: darf die KI ihre Trade-Settings ändern?
    # off = nie | suggest = Vorschläge, Trader bestätigt | auto = sofort anwenden
    "autonomy": "suggest",
    # Selbst-Lernen aus Signal-/Trade-Ergebnissen
    "learning_enabled": True,
    "learn_on_trade_close": True,
    "learning_lookback_days": 14,
    "max_lessons": 10,
    # KI-berechnete SL/TP-Levels direkt für die Order nutzen (statt Coin-Trade-Settings)
    "use_ai_levels": False,
    # Übergeordnete Swing-Trades: eigene Kategorie mit niedrigem Hebel und
    # weiten Zielen, parallel zu kurzfristigen (auch gegenläufigen) Scalps.
    "swing_enabled": True,
    "swing_max_leverage": 8,
    # Gruppen-Analyse: Krypto / Forex / Indizes+Rohstoffe in getrennten
    # LLM-Läufen für tiefere, asset-spezifischere Begründungen.
    "group_analysis": True,
    # Lean-Prompt: statische Info-Blöcke (Plattform-Wissen, Parameter der
    # anderen Strategien) aus jedem Analyse-Lauf weglassen – spart Tokens/Kosten
    # ohne die Entscheidungsqualität zu beeinflussen.
    "lean_prompt": True,
    # Smart-Skip: geplanten LLM-Lauf einer Gruppe überspringen, wenn sich der
    # Markt seit der letzten Analyse kaum bewegt hat, keine Position offen ist
    # und die letzte Entscheidung überall HOLD war (max. 2 Skips in Folge;
    # manuelle Analysen laufen immer). Spart LLM-Calls/Kosten.
    "smart_skip": True,
    "smart_skip_move_pct": 0.15,
    # Trade-Rahmen (global fürs ganze KI-Team, gilt für jeden Trade):
    # CRV-Spanne (TP1 vs. SL) in der sich die KI frei bewegen darf.
    # CRV-Rahmen (TP1 relativ zum SL), von der KI pro Trade frei innerhalb der
    # Spanne wählbar. crv_max Standard 4 (User 15.06.): deckelt unrealistisch
    # weite TPs; 0 = keine Obergrenze bleibt wählbar. Prod-Hinweis: eine bereits
    # gespeicherte 0 in der Prod-Config bleibt 0 – einmal im Setup umstellen.
    "crv_min": 1.2,
    "crv_max": 4.0,
    # Hebel-Modus: "coin" = Coin-Trade-Settings entscheiden (bisheriges
    # Verhalten) | "auto" = KI wählt pro Trade frei bis lev_auto_max |
    # "fixed" = immer fester Hebel lev_fixed.
    "lev_mode": "coin",
    "lev_auto_max": 25,
    "lev_fixed": 10,
    # Diversifikations-Guards (technisch erzwungen, gegen Klumpen-Trades):
    # max. gleichzeitig offene KI-Trades in DIESELBE Richtung (0 = aus) und
    # Mindestabstand (%) zwischen Entries auf demselben Symbol + Richtung.
    "max_same_direction": 3,
    "min_entry_distance_pct": 0.5,
    # BTC/ETH/SOL als EIN Richtungs-Risiko zählen (Korrelations-Guard)
    "correlation_guard": True,
}

# Kataloge, Keys (inkl. Backup-Keys) & Modell-Gewichte leben zentral in
# services/ai_providers.py – hier nur Aliase für Rückwärtskompatibilität.
ALLOWED_MODELS = ai_providers.ALLOWED_MODELS
OPENAI_COMPAT_PROVIDERS = ai_providers.OPENAI_COMPAT_PROVIDERS
FALLBACK_ORDER = ai_providers.FALLBACK_ORDER

DEEP_ANALYSIS_SYSTEM = (
    "Du bist der 'Tiefen-Analyst' im KI-Team einer Krypto-Daytrading-Plattform. "
    "Du erstellst eine SEHR gründliche Marktanalyse (kein direkter Trade-Auftrag): "
    "Makro-Lage, Schlüssel-Levels, Szenarien pro Coin, Risiken, konkrete Empfehlungen "
    "für den regulären Analysten (der periodisch tradet). Nutze ALLE übergebenen Daten "
    "inkl. Wirtschaftskalender, News-Wächter-Ereignisse und Performance der anderen "
    "Strategien der Plattform. Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"report": "8-15 Sätze tiefe Marktanalyse auf Deutsch", '
    '"outlook": [{"symbol": "BTCUSDT", "bias": "bullish|bearish|neutral", '
    '"key_levels": "kompakt", "szenario": "1-2 Sätze"}], '
    '"risks": ["Risiko 1", "Risiko 2"], '
    '"recommendations": ["konkrete Empfehlung für den Analysten"]}'
)

ANALYSIS_SYSTEM = (
    "Du bist ein erfahrener Krypto-Daytrading-Analyst und triffst eigenständige "
    "Trading-Entscheidungen für ein automatisiertes System. Du bekommst Multi-Timeframe-"
    "Marktdaten, aktuelle News-Schlagzeilen, offene Positionen und Anweisungen des Traders. "
    "Sei diszipliniert: Trade NUR bei klarer Edge, sonst HOLD. Sei ehrlich mit der Konfidenz. "
    "Bestimme je Asset ZUERST die aktuelle Marktphase (Trend/Range/Squeeze/News-getrieben) und "
    "suche den dazu passenden Edge. RANGE-TRADING: Liefern die Marktdaten einen 'Range-Check'-"
    "Block (Seitwärtsrange mit mehrfachen Touches), ist ein range_fade-Setup valide, sobald eine "
    "Wick-Rejection an der Range-Grenze vorliegt: Entry an der Grenze, SL knapp dahinter, "
    "TP zur Range-Mitte bzw. Gegenseite. Ohne Wick-Rejection oder mitten in der Range: HOLD. "
    "Bei HOLD nenne im reasoning kurz den konkreten Grund "
    "(z.B. 'schlechtes Handelsfenster', 'Range ohne klares Level', 'Trade-Sperre bis klarer Edge') – "
    "confidence darf dann bewusst niedrig oder 0 sein. "
    "Berücksichtige Anweisungen des Traders IMMER mit höchster Priorität. "
    "Der MASTERPROMPT des Traders steht über allem – auch über deinen Lektionen. "
    "Du bist EINE Strategie von vielen auf dieser Plattform: dein Auftrag ist, den Markt "
    "perfekt zu kennen, passende Strategien anzuwenden oder neu zu entwickeln und dabei von "
    "den anderen Strategien und deren Parametern zu lernen. "
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown, exakt in diesem Schema:\n"
    '{"market_overview": "2-4 Sätze Marktlage auf Deutsch", '
    '"decisions": [{"symbol": "BTCUSDT", "action": "LONG|SHORT|HOLD", '
    '"confidence": 0-100, "horizon": "scalp|swing", "runner": false, '
    '"setup": "trend_follow|breakout|squeeze_breakout|mean_reversion|range_fade|'
    'liquidity_sweep|momentum_news|pullback|swing_trend|hedge", '
    '"sl_pct": 0.2-3.0, "tp1_pct": 0.3-4.0, "tpf_pct": 0.5-8.0, '
    '"capital_pct": 10-100, '
    '"news_impact": "positive|negative|neutral", "strategy_candidate_id": null, '
    '"reasoning": "1-2 Sätze auf Deutsch", '
    '"size_reason": "1 kurzer Satz: warum diese Positionsgröße (capital_pct/Hebel)", '
    '"levels_reason": "1 kurzer Satz: warum SL und TP genau dort liegen"}], '
    '"new_strategies": [{"name": "...", "thesis": "...", "rules_text": "...", '
    '"symbols": ["BTCUSDT"], "learned_from": "..."}], '
    '"config_changes": [{"symbol": "BTCUSDT", "changes": {"leverage": 8}, "reason": "kurz"}]}\n'
    "Regeln: sl_pct/tp1_pct/tpf_pct sind Prozent-Abstände vom aktuellen Preis. "
    "tp1_pct > sl_pct (CRV mind. 1.2), tpf_pct > tp1_pct. Für JEDES übergebene Symbol genau eine Entscheidung. "
    "SETUP-PFLICHT: Wähle bei LONG/SHORT das gehandelte Setup aus dem STRATEGIE-PLAYBOOK "
    "(Feld 'setup'); bevorzuge bewährte Setups, meide gesperrte, variiere statt immer dasselbe "
    "Muster; bei HOLD 'setup' weglassen. SL und TP gehören ZUM Setup (Mean-Reversion enge Ziele, "
    "Breakout/Squeeze weite) – nicht immer dieselben Standardwerte. "
    "HORIZON: 'scalp' (Standard) = kurz-/mittelfristig mit den normalen Bereichen oben. "
    "'swing' = übergeordneter, langfristiger Trade: niedriger Hebel (wird automatisch gedeckelt), "
    "weite Ziele erlaubt (sl_pct 0.5-12, tp1_pct 0.8-25, tpf_pct bis 60), 'runner': true bedeutet "
    "nach TP1 läuft der Rest ohne festes Endziel mit Trailing-Stop weiter. Ein Swing-Trade und "
    "kurzfristige Gegen-Scalps auf demselben Asset schließen sich NICHT aus – du darfst z.B. einen "
    "übergeordneten LONG halten und zwischenzeitliche Abwärtsbewegungen mit SHORT-Scalps handeln. "
    "Nutze swing nur bei klarer übergeordneter Struktur (Higher-Timeframe-Trend, Makro-These), "
    "nicht als Ausrede für weite Stops.\n"
    "DIVERSIFIKATION (weiche Regel): Eröffne nicht reflexartig viele Trades in dieselbe Richtung "
    "mit derselben Begründung – das bündelt das Risiko auf eine einzige Prognose. Prüfe die "
    "offenen Positionen und staffle/variiere bewusst; wenn du dennoch mehrere gleichgerichtete "
    "Trades willst, begründe das explizit. Kein hartes Verbot.\n"
    "BEGRÜNDUNGEN: Jedes 'reasoning' muss ASSET-SPEZIFISCH sein (konkrete Level, Struktur, "
    "Besonderheit des Assets) – KEINE Copy-Paste-Sätze über viele Assets hinweg. Wenn ein "
    "übergreifender Grund (z.B. Makro-Event) alle Assets betrifft, nenne ihn EINMAL in "
    "market_overview und schreibe im reasoning nur die asset-spezifischen Details. "
    "Staffle auch die confidence-Werte ehrlich pro Asset statt pauschal denselben Wert zu geben. "
    "capital_pct = wie viel Prozent deines Max-Kapitals pro Trade du einsetzen willst (10-100). "
    "Wähle capital_pct proportional zu deiner Überzeugung und dem Setup-Risiko – setze NICHT "
    "reflexartig immer 100, sondern staffle: schwächere Setups kleiner, A+-Setups größer. "
    "strategy_candidate_id nur setzen, wenn die Entscheidung zu einem Strategie-Kandidaten aus "
    "deinem Strategie-Labor gehört (Kandidaten in der Ghost-Phase werden automatisch nur "
    "simuliert). new_strategies nur bei einer wirklich neuen, begründeten Idee (sonst leere Liste). "
    "config_changes ist optional und NUR erlaubt, wenn der Prompt-Abschnitt EINSTELLUNGS-AUTONOMIE aktiv ist – "
    "sonst leere Liste. Nutze deine Performance-Statistik und gelernten Lektionen aktiv für bessere Entscheidungen.\n"
    "TIMEFRAME-DISZIPLIN: Gewichte 5m/15m-Struktur stärker als den 1m-Chart. RSI auf 1m ist "
    "Rauschen und allein KEIN Trade-Grund – nutze ihn höchstens fürs Entry-Timing; die Bestätigung "
    "kommt vom 5m/15m (Scalps) bzw. 1h+ (Swings). Läuft ein Setup wiederholt schlecht, wechsle "
    "bewusst die Auswertungs-Ebene (höherer Timeframe, andere Trigger) oder setze das Setup aus – "
    "das Playbook sperrt schwache Setups zusätzlich automatisch.\n"
    "SESSION-LEVELS & ZONEN: Die Marktdaten enthalten Highs/Lows der Asia-/London-/NY-Session und "
    "Umverteilungszonen (Volumen-Cluster). Nutze sie aktiv: Sweeps von Session-Hochs/-Tiefs sind "
    "bevorzugte liquidity_sweep-Trigger, Ausbrüche aus Umverteilungszonen bevorzugte "
    "breakout-Trigger; Entries mitten in einer Zone haben schlechtes CRV und sind zu meiden.\n"
    "KONFIDENZ-KALIBRIERUNG: HOLD ohne Edge ist richtig – aber wenn Struktur, Level und Trigger "
    "zusammenpassen, benenne das Setup und handle es mit ehrlicher Konfidenz (sauberes A-Setup = "
    "70-85, nicht chronisch 50-60). Dauer-HOLD über viele Zyklen trotz klarer Setups ist genauso "
    "ein Fehler wie Overtrading."
)

# Kompakte Variante des Analyse-Systemprompts (aktiv bei lean_prompt=AN):
# identisches JSON-Schema und identische Kernregeln, aber ohne ausschweifende
# Erklärtexte – spart pro Gruppen-Lauf mehrere hundert Tokens ohne die
# Entscheidungsqualität zu verändern.
ANALYSIS_SYSTEM_LEAN = (
    "Du bist ein disziplinierter Krypto-Daytrading-Analyst eines automatisierten Systems. "
    "Anweisungen des Traders und der MASTERPROMPT stehen über allem. "
    "Bestimme je Asset ZUERST die Marktphase (Trend/Range/Squeeze/News) und suche den dazu "
    "passenden Edge – trade NUR bei klarer Edge, sonst HOLD mit kurzem Grund im reasoning "
    "(z.B. 'schlechtes Handelsfenster', 'kein klares Level', 'Trade-Sperre bis klarer Edge'); "
    "confidence darf bei HOLD bewusst 0 sein. "
    "Range-Trading: 'Range-Check'-Block + Wick-Rejection an der Range-Grenze = valides "
    "range_fade-Setup (Entry an der Grenze, SL knapp dahinter, TP Range-Mitte/Gegenseite). "
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown, exakt in diesem Schema:\n"
    '{"market_overview": "2-3 Sätze Marktlage auf Deutsch", '
    '"decisions": [{"symbol": "BTCUSDT", "action": "LONG|SHORT|HOLD", '
    '"confidence": 0-100, "horizon": "scalp|swing", "runner": false, '
    '"setup": "trend_follow|breakout|squeeze_breakout|mean_reversion|range_fade|'
    'liquidity_sweep|momentum_news|pullback|swing_trend|hedge", '
    '"sl_pct": 0.2-3.0, "tp1_pct": 0.3-4.0, "tpf_pct": 0.5-8.0, '
    '"capital_pct": 10-100, '
    '"news_impact": "positive|negative|neutral", "strategy_candidate_id": null, '
    '"reasoning": "1-2 Sätze auf Deutsch", '
    '"size_reason": "1 kurzer Satz: warum diese Positionsgröße (capital_pct/Hebel)", '
    '"levels_reason": "1 kurzer Satz: warum SL und TP genau dort liegen"}], '
    '"new_strategies": [{"name": "...", "thesis": "...", "rules_text": "...", '
    '"symbols": ["BTCUSDT"], "learned_from": "..."}], '
    '"config_changes": [{"symbol": "BTCUSDT", "changes": {"leverage": 8}, "reason": "kurz"}]}\n'
    "Regeln: sl/tp-Prozente = Abstand vom aktuellen Preis; tp1_pct > sl_pct; tpf_pct > tp1_pct; "
    "für JEDES übergebene Symbol genau EINE Entscheidung. "
    "setup = gehandeltes Playbook-Setup (Pflicht bei LONG/SHORT; gesperrte Setups nicht nutzen; "
    "SL/TP passend zum Setup wählen statt Standardwerte; bei HOLD weglassen). "
    "horizon 'swing' = übergeordneter Trade (Hebel wird automatisch gedeckelt, weite Ziele erlaubt: "
    "sl_pct 0.5-12, tp1_pct 0.8-25, tpf_pct bis 60); runner=true nur bei swing (Rest läuft nach TP1 "
    "mit Trailing weiter). Swing-Position und gegenläufige Scalps auf demselben Asset sind erlaubt. "
    "Kein reflexartiges Bündeln vieler gleichgerichteter Trades mit derselben Begründung. "
    "reasoning IMMER asset-spezifisch (konkrete Level/Struktur), keine Copy-Paste-Sätze; "
    "confidence ehrlich pro Asset staffeln. capital_pct proportional zur Überzeugung (nicht immer 100). "
    "strategy_candidate_id nur für Kandidaten aus deinem Strategie-Labor. "
    "new_strategies nur bei wirklich neuer, begründeter Idee (sonst []). "
    "config_changes NUR wenn der Abschnitt EINSTELLUNGS-AUTONOMIE aktiv ist – sonst []. "
    "Nutze Performance-Statistik und Lektionen aktiv. "
    "TIMEFRAMES: 5m/15m-Struktur schlägt 1m; RSI(1m) ist nur Entry-Timing, nie der Trade-Grund; "
    "Swings auf 1h+ bestätigen; schlecht laufende Setups auf höhere Timeframes umstellen oder aussetzen. "
    "SESSION-LEVELS & ZONEN: Sweeps der Asia-/London-/NY-Session-Hochs/-Tiefs = bevorzugte "
    "liquidity_sweep-Trigger; Ausbrüche aus Umverteilungszonen (Volumen-Cluster) = bevorzugte "
    "breakout-Trigger; Entries mitten in einer Zone meiden. "
    "KONFIDENZ: sauberes A-Setup ehrlich mit 70-85 bewerten (nicht chronisch 50-60) – "
    "Dauer-HOLD trotz klarer Setups ist genauso ein Fehler wie Overtrading."
)

# --- Fix 0.4: Prompt-Versionierung -------------------------------------------
# Kurz-Hashes der statischen Analyse-Systemprompts (ändern sich automatisch bei
# jeder Prompt-Code-Änderung/Deploy). Zusammen mit master_prompt.version_hash()
# wird jede ai_decision einem exakten Prompt-Stand zuordenbar -> ML-Daten
# lassen sich nach Prompt-Version segmentieren.


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


ANALYSIS_PROMPT_HASHES = {
    "full": _prompt_hash(ANALYSIS_SYSTEM),
    "lean": _prompt_hash(ANALYSIS_SYSTEM_LEAN),
}


def prompt_version_info(variant: str) -> Dict:
    """Versions-Fingerprint des kompletten Entscheidungs-Prompts.

    combined = ML-Groupby-Key; Einzelteile für gezielte Analysen:
    analysis = Hash des statischen Systemprompts (variant lean|full),
    master/master_v = Inhalts-Hash + laufende Version des Trader-MasterPrompts.
    """
    a_hash = ANALYSIS_PROMPT_HASHES.get(variant, "")
    m_hash = master_prompt.version_hash()
    return {
        "analysis": a_hash,
        "variant": variant,
        "master": m_hash,
        "master_v": master_prompt.version,
        "combined": f"{variant}-{a_hash}+{m_hash}",
    }


OPINION_SYSTEM = (
    "Du bist der 'KI Trader'. Der Trader hat eine Änderung an deinen Vorgaben vorgenommen. "
    "Sie gilt sofort – du kannst sie nicht blockieren. Sage aber ehrlich und datenbasiert deine "
    "Meinung: stimmt sie mit deiner Erfahrung überein, oder hast du einen Einwand? "
    "Antworte AUSSCHLIESSLICH mit validem JSON ohne Markdown:\n"
    '{"stance": "zustimmung|einwand|neutral", "comment": "2-4 Sätze auf Deutsch", '
    '"risk": "kurz, falls Risiko – sonst leer"}'
)

CHAT_SYSTEM_TEMPLATE = (
    "Du bist der 'KI Trader' – die integrierte Trading-KI einer Krypto-Daytrading-Plattform. "
    "Du analysierst periodisch alle Coins (Multi-Timeframe + News) und kannst automatisch Trades auslösen. "
    "Der Nutzer chattet hier mit dir, um dir Anweisungen zu geben (z.B. 'achte auf BTC-Support bei 60k', "
    "'sei heute defensiv', 'keine Shorts auf SOL'). Alle Nutzer-Nachrichten fließen automatisch als "
    "Direktiven in deine nächste Analyse ein – bestätige das, wenn dir jemand eine Anweisung gibt. "
    "Antworte kompakt, präzise und auf Deutsch. Nutze die Live-Daten unten für fundierte Antworten. "
    "Erfinde keine Zahlen.\n"
    "WICHTIG – ECHTE AKTIONEN: Gibt der Trader eine ausführbare Anweisung (Positionen schließen, "
    "Trade anpassen, Lektion anlegen/ändern/löschen, Einstellung ändern), führt das System sie REAL "
    "aus, BEVOR du antwortest. Die echten Ergebnisse stehen dann im Block 'SOEBEN REAL AUSGEFÜHRTE "
    "AKTIONEN'. Berichte EXAKT diese Ergebnisse. Behaupte NIEMALS, etwas geschlossen oder geändert "
    "zu haben, das dort nicht mit ✅ gelistet ist – fehlt der Block, wurde NICHTS ausgeführt: sage "
    "das ehrlich und bitte um eine präzisere Anweisung.\n\n"
    "=== AKTUELLER KONTEXT ===\n{context}\n\n"
    "=== BISHERIGER CHAT-VERLAUF ===\n{history}"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_rate_limit_error(err: Exception) -> bool:
    """True wenn Gemini 429 / RESOURCE_EXHAUSTED / Quota-Fehler wirft."""
    s = str(err).lower()
    return any(k in s for k in ("429", "resource_exhausted", "quota", "rate limit", "ratelimit"))


class AIEngine:
    def __init__(self):
        self.config = dict(DEFAULT_AI_CONFIG)
        self.db = None
        self.scanner = None
        self.signal_cb: Optional[Callable] = None
        self.toggle_check: Optional[Callable] = None
        self.symbols: List[str] = []
        self.decisions: Dict[str, Dict] = {}
        self.last_run: Optional[str] = None
        self.next_run: Optional[str] = None
        self.active_window: Optional[str] = None
        self._day_risk_cache: Dict = {}
        self.last_error: Optional[str] = None
        self.running = False
        self._analyzing = False
        self._next_due = 0.0
        self._last_signal_ts: Dict[str, float] = {}
        self._group_skips: Dict[str, int] = {}
        self._review_last_check = 0.0
        self._last_ghost_ts: Dict[str, float] = {}
        # Eigener Cooldown für Datensammel-Trades (Phase 4)
        self._last_collection_ts: Dict[str, float] = {}
        # Modell, das aktuell benutzt wird (nach Fallback ggf. abweichend von cfg.model)
        self._effective_model: Optional[str] = None
        self._effective_provider: Optional[str] = None
        # Deep-Analysis-Scheduling: Slot ("HH:MM") -> Berlin-Datum des letzten Laufs
        self._deep_ran: Dict[str, str] = {}
        self.deep_last: Optional[str] = None
        self.deep_last_error: Optional[str] = None
        # Housekeeping-State (Europe/Berlin) – wird in settings/ai_trader_housekeeping persistiert.
        # Hour-Key im Format "YYYYMMDDHH", Date-Key "YYYY-MM-DD".
        self._last_cleanup_hour: Optional[str] = None
        self._last_reset_date: Optional[str] = None
        self._housekeeping_lock = asyncio.Lock()
        # Retry-Backoff für den täglichen Reset. Zählt Fehlversuche pro anstehendem
        # Vortag, damit ein LLM-/DB-Ausfall den Reset nicht dauerhaft verhindert –
        # aber auch nicht die Engine in einer Endlosschleife blockiert.
        self._reset_retry_day: Optional[str] = None
        self._reset_retry_count: int = 0
        # Lern-Modul (wird in setup() initialisiert, braucht db)
        self.learning = None

    @property
    def key(self) -> Optional[str]:
        """API-Key des aktuell konfigurierten Providers (primärer Key).

        Fällt auf den Key eines beliebigen konfigurierten Providers zurück –
        die Modell-Kette (services/ai_roles.chain) nutzt ohnehin alle Provider
        mit Key als letzte Fallback-Stufe. So blockiert eine Voreinstellung für
        einen Provider ohne Key den Betrieb nicht."""
        direct = ai_providers.primary_key(self.config.get("provider", "gemini"))
        if direct:
            return direct
        for prov, has_key in ai_providers.available_providers().items():
            if has_key:
                return ai_providers.primary_key(prov)
        return None

    @staticmethod
    def _provider_key(provider: str) -> Optional[str]:
        return ai_providers.primary_key(provider)

    def _available_providers(self) -> Dict[str, bool]:
        """True, wenn für den Provider ein API-Key gesetzt ist."""
        return ai_providers.available_providers()

    def setup(self, db, scanner, signal_cb, toggle_check, symbols: List[str]):
        self.db = db
        self.scanner = scanner
        self.signal_cb = signal_cb
        self.toggle_check = toggle_check
        self.symbols = symbols
        from services.ai_learning import AILearning  # lazy: vermeidet Zyklen
        self.learning = AILearning(self)

    # ---------------- config ----------------
    async def load_config(self):
        doc = await self.db.settings.find_one({"_id": "ai_trader_config"})
        if doc:
            doc.pop("_id", None)
            for k in DEFAULT_AI_CONFIG:
                if k in doc:
                    self.config[k] = doc[k]
            # Migration: unbekannten Provider oder ungültiges Modell -> zuerst
            # tote Slugs auf Nachfolger mappen, sonst Default (Gemini Flash)
            prov = self.config.get("provider")
            mod = self.config.get("model")
            if prov not in ALLOWED_MODELS or mod not in ALLOWED_MODELS.get(prov, []):
                new_prov, new_mod = ai_providers.migrate_model(prov, mod)
                if new_mod not in ALLOWED_MODELS.get(new_prov or "", []):
                    new_prov, new_mod = "gemini", "gemini-3.5-flash"
                logger.info(f"AI config: Modell migriert {prov}/{mod} -> {new_prov}/{new_mod}")
                self.config["provider"] = new_prov
                self.config["model"] = new_mod
                await self.db.settings.update_one(
                    {"_id": "ai_trader_config"},
                    {"$set": {"provider": new_prov, "model": new_mod}},
                    upsert=True,
                )
            # Einmalige Migration (User 15.06.): gespeichertes crv_max=0 (alte
            # Voreinstellung "keine Obergrenze") -> neuer Standard 4. Wer danach
            # bewusst wieder 0 wählt, bleibt bei 0 (Marker verhindert Wiederholung).
            if not doc.get("crv_max_migrated_v1") and float(doc.get("crv_max", 0) or 0) == 0:
                self.config["crv_max"] = 4.0
                await self.db.settings.update_one(
                    {"_id": "ai_trader_config"},
                    {"$set": {"crv_max": 4.0, "crv_max_migrated_v1": True}},
                    upsert=True)
                logger.info("AI config: crv_max 0 -> 4.0 migriert (Deckel gegen unrealistisch weite TPs)")
            elif not doc.get("crv_max_migrated_v1"):
                await self.db.settings.update_one(
                    {"_id": "ai_trader_config"},
                    {"$set": {"crv_max_migrated_v1": True}}, upsert=True)
        else:
            await self.db.settings.insert_one({"_id": "ai_trader_config", **self.config})
        # Self-Tuning-Guard: von der KI selbst gesetzte Werte außerhalb der
        # Leitplanken einmalig zurückholen (heilt Prod nach Deploy, ohne einen
        # manuell vom Trader gesetzten Wert anzufassen).
        try:
            await self._normalize_auto_tuned()
        except Exception as e:
            logger.warning(f"Self-Tuning-Guard Normalisierung fehlgeschlagen: {e}")
        # load last decisions for continuity after restart
        try:
            rows = await self.db.ai_decisions.find().sort("ts", -1).limit(60).to_list(60)
            for r in rows:
                sym = r.get("symbol")
                if sym and sym not in self.decisions:
                    r.pop("_id", None)
                    self.decisions[sym] = r
        except Exception:
            pass
        # Housekeeping-Marker laden. Beim allerersten Start werden sie mit dem
        # aktuellen Berlin-Zeitstempel initialisiert, damit weder Cleanup noch
        # Reset direkt nach dem Boot feuern (sondern erst zur nächsten vollen
        # Stunde bzw. zum nächsten 00:00 Uhr Berlin).
        try:
            hk = await self.db.settings.find_one({"_id": "ai_trader_housekeeping"})
            now_berlin = datetime.now(BERLIN_TZ)
            if hk:
                self._last_cleanup_hour = hk.get("last_cleanup_hour")
                self._last_reset_date = hk.get("last_reset_date")
            if not self._last_cleanup_hour:
                self._last_cleanup_hour = now_berlin.strftime("%Y%m%d%H")
            if not self._last_reset_date:
                self._last_reset_date = now_berlin.strftime("%Y-%m-%d")
            await self.db.settings.update_one(
                {"_id": "ai_trader_housekeeping"},
                {"$set": {
                    "last_cleanup_hour": self._last_cleanup_hour,
                    "last_reset_date": self._last_reset_date,
                }},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"AI housekeeping init failed: {e}")
        if self.learning:
            await self.learning.load_state()
        # KI-Team-Rollen (Modelle, Handelszeiten, Fallback-KI) laden
        try:
            await role_manager.load(self.db)
        except Exception as e:
            logger.warning(f"AI roles load failed: {e}")

    async def update_config(self, updates: Dict) -> Dict:
        was_enabled = self.config.get("enabled")
        if "enabled" in updates:
            self.config["enabled"] = bool(updates["enabled"])
        if "interval_min" in updates:
            self.config["interval_min"] = max(2, min(120, int(updates["interval_min"])))
        if "schedule" in updates:
            self.config["schedule"] = ai_schedule.normalize_schedule(updates["schedule"])
        if "min_confidence" in updates:
            self.config["min_confidence"] = max(0, min(100, int(updates["min_confidence"])))
        if "cooldown_min" in updates:
            self.config["cooldown_min"] = max(0, min(720, int(updates["cooldown_min"])))
        if "tune_conf_min" in updates:
            self.config["tune_conf_min"] = max(0, min(100, int(updates["tune_conf_min"])))
        if "tune_conf_max" in updates:
            self.config["tune_conf_max"] = max(0, min(100, int(updates["tune_conf_max"])))
        if self.config.get("tune_conf_min", 55) > self.config.get("tune_conf_max", 75):
            self.config["tune_conf_min"] = self.config["tune_conf_max"]
        if "tune_cooldown_max" in updates:
            self.config["tune_cooldown_max"] = max(0, min(720, int(updates["tune_cooldown_max"])))
        if "fee_guard_enabled" in updates:
            self.config["fee_guard_enabled"] = bool(updates["fee_guard_enabled"])
        if "fee_guard_mult" in updates:
            try:
                self.config["fee_guard_mult"] = max(0.0, min(30.0, float(updates["fee_guard_mult"])))
            except (TypeError, ValueError):
                pass
        if "collection_enabled" in updates:
            self.config["collection_enabled"] = bool(updates["collection_enabled"])
        if "collection_min_confidence" in updates:
            self.config["collection_min_confidence"] = max(0, min(100, int(updates["collection_min_confidence"])))
        if "collection_cooldown_min" in updates:
            self.config["collection_cooldown_min"] = max(0, min(720, int(updates["collection_cooldown_min"])))
        if "collection_max_same_direction" in updates:
            self.config["collection_max_same_direction"] = max(0, min(10, int(updates["collection_max_same_direction"])))
        if "collection_max_per_coin" in updates:
            self.config["collection_max_per_coin"] = max(1, min(5, int(updates["collection_max_per_coin"])))
        if "max_trades_per_coin" in updates:
            self.config["max_trades_per_coin"] = max(1, min(5, int(updates["max_trades_per_coin"])))
        if "max_capital_per_trade" in updates:
            try:
                self.config["max_capital_per_trade"] = max(0.0, min(100000.0, float(updates["max_capital_per_trade"] or 0)))
            except (TypeError, ValueError):
                pass
        if "news_enabled" in updates:
            self.config["news_enabled"] = bool(updates["news_enabled"])
        if "macro_enabled" in updates:
            self.config["macro_enabled"] = bool(updates["macro_enabled"])
        if "macro_symbols" in updates and isinstance(updates["macro_symbols"], list):
            syms = [str(s).upper() for s in updates["macro_symbols"] if str(s).strip()]
            self.config["macro_symbols"] = syms[:6] or macro_context.DEFAULT_SYMBOLS
        if "liquidity_enabled" in updates:
            self.config["liquidity_enabled"] = bool(updates["liquidity_enabled"])
        if "liquidity_symbols" in updates and isinstance(updates["liquidity_symbols"], list):
            syms = [str(s).upper() for s in updates["liquidity_symbols"] if str(s).strip()]
            self.config["liquidity_symbols"] = syms[:6] or list(liquidity_data.DEFAULT_SYMBOLS)
        for flag in ("use_liquidation_data", "use_heatmap_data", "lean_prompt", "smart_skip"):
            if flag in updates:
                self.config[flag] = bool(updates[flag])
        if "smart_skip_move_pct" in updates:
            try:
                self.config["smart_skip_move_pct"] = max(0.02, min(2.0, float(updates["smart_skip_move_pct"])))
            except (TypeError, ValueError):
                pass
        if "autonomy" in updates and updates["autonomy"] in ("off", "suggest", "auto"):
            self.config["autonomy"] = updates["autonomy"]
        if "learning_enabled" in updates:
            self.config["learning_enabled"] = bool(updates["learning_enabled"])
        if "learn_on_trade_close" in updates:
            self.config["learn_on_trade_close"] = bool(updates["learn_on_trade_close"])
        if "learning_lookback_days" in updates:
            self.config["learning_lookback_days"] = max(3, min(90, int(updates["learning_lookback_days"])))
        if "max_lessons" in updates:
            self.config["max_lessons"] = max(3, min(100, int(updates["max_lessons"])))
        if "use_ai_levels" in updates:
            self.config["use_ai_levels"] = bool(updates["use_ai_levels"])
        if "swing_enabled" in updates:
            self.config["swing_enabled"] = bool(updates["swing_enabled"])
        if "group_analysis" in updates:
            self.config["group_analysis"] = bool(updates["group_analysis"])
        if "swing_max_leverage" in updates:
            try:
                self.config["swing_max_leverage"] = max(1, min(20, int(updates["swing_max_leverage"])))
            except (TypeError, ValueError):
                pass
        if "crv_min" in updates:
            try:
                self.config["crv_min"] = max(1.0, min(10.0, float(updates["crv_min"])))
            except (TypeError, ValueError):
                pass
        if "crv_max" in updates:
            try:
                v = float(updates["crv_max"] or 0)
                self.config["crv_max"] = 0 if v <= 0 else max(
                    float(self.config.get("crv_min", 1.2) or 1.2), min(20.0, v))
            except (TypeError, ValueError):
                pass
        if "lev_mode" in updates and updates["lev_mode"] in ("coin", "auto", "fixed"):
            self.config["lev_mode"] = updates["lev_mode"]
        if "lev_auto_max" in updates:
            try:
                self.config["lev_auto_max"] = max(1, min(100, int(updates["lev_auto_max"])))
            except (TypeError, ValueError):
                pass
        if "lev_fixed" in updates:
            try:
                self.config["lev_fixed"] = max(1, min(100, int(updates["lev_fixed"])))
            except (TypeError, ValueError):
                pass
        if "max_same_direction" in updates:
            try:
                self.config["max_same_direction"] = max(0, min(20, int(updates["max_same_direction"])))
            except (TypeError, ValueError):
                pass
        if "min_entry_distance_pct" in updates:
            try:
                self.config["min_entry_distance_pct"] = max(0.0, min(5.0, float(updates["min_entry_distance_pct"])))
            except (TypeError, ValueError):
                pass
        if "correlation_guard" in updates:
            self.config["correlation_guard"] = bool(updates["correlation_guard"])
        if "provider" in updates and "model" in updates:
            prov, mod = updates["provider"], updates["model"]
            if prov in ALLOWED_MODELS and mod in ALLOWED_MODELS[prov]:
                self.config["provider"], self.config["model"] = prov, mod
                # Wechselt der Nutzer das Modell manuell, reset des Fallback-States.
                self._effective_model = None
        elif "model" in updates:
            mod = updates["model"]
            # Finde Provider automatisch anhand des Modells
            for prov, models in ALLOWED_MODELS.items():
                if mod in models:
                    self.config["model"] = mod
                    self.config["provider"] = prov
                    self._effective_model = None
                    break
        await self.db.settings.update_one({"_id": "ai_trader_config"},
                                          {"$set": dict(self.config)}, upsert=True)
        if self.config.get("enabled") and not was_enabled:
            self._next_due = 0  # run analysis immediately after enabling
        elif "schedule" in updates or "interval_min" in updates:
            # Neues (kürzeres) Intervall soll sofort greifen, nicht erst nach dem
            # alten Wartefenster.
            interval = max(1, self.current_interval()[0]) * 60
            self._next_due = min(self._next_due, time.time() + interval)
        return dict(self.config)

    # ---------------- market context ----------------
    def _snapshot(self, symbol: str) -> Optional[Dict]:
        candles = self.scanner.candle_buffer.get(symbol, [])
        if len(candles) < 60:
            return None
        ti = TechnicalIndicators
        price = candles[-1]["close"]
        # Frischester Live-Preis: die formende (noch offene) Kerze des Scanners
        # nutzen, solange sie aktuell ist (<4 min) – sonst letzter Schlusskurs.
        forming = (getattr(self.scanner, "forming", None) or {}).get(symbol)
        if isinstance(forming, dict) and forming.get("close"):
            age_ms = (datetime.now(timezone.utc).timestamp() * 1000
                      - float(forming.get("timestamp") or 0))
            if 0 <= age_ms < 4 * 60 * 1000:
                price = forming["close"]
        lines = []
        rsi_1m = 0
        rsi_5m = 0
        # 5m ergänzt: RSI/Struktur auf 1m ist überwiegend Rauschen ("Gambling"),
        # auf 5m/15m deutlich aussagekräftiger – die KI bekommt beide Ebenen.
        for tf in ("1m", "5m", "15m", "1h"):
            agg = candles if tf == "1m" else aggregate_candles(candles, tf, drop_partial=True)
            if len(agg) < 20:
                continue
            cl = [c["close"] for c in agg][-120:]
            rsi_arr = ti.calculate_rsi(cl, 14)
            rsi = rsi_arr[-1] if rsi_arr and rsi_arr[-1] is not None else 50
            if tf == "1m":
                rsi_1m = rsi
            elif tf == "5m":
                rsi_5m = rsi
            ema20 = ti.calculate_ema(cl, 20)[-1]
            ema50 = ti.calculate_ema(cl, 50)[-1] if len(cl) >= 50 else None
            trend = "aufwärts" if (ema50 and ema20 > ema50) else ("abwärts" if ema50 else "unklar")
            chg = (cl[-1] - cl[0]) / cl[0] * 100 if cl[0] else 0
            hi = max(c["high"] for c in agg[-60:])
            lo = min(c["low"] for c in agg[-60:])
            noise = " (nur Timing)" if tf == "1m" else ""
            lines.append(f"{tf}: RSI {rsi:.0f}{noise}, Trend {trend}, Δ{chg:+.2f}%, Range {lo:g}-{hi:g}")
        try:
            atr = ti.calculate_atr(candles, 14)[-1] or 0
            vols = [c.get("volume", 0) for c in candles]
            v_recent = sum(vols[-5:]) / 5
            v_base = (sum(vols[-60:]) / 60) or 1
            lines.append(f"ATR(1m) {atr / price * 100:.3f}% | Volumen x{v_recent / v_base:.2f}")
        except Exception:
            pass
        # Session-Highs/-Lows (Asia/London/NY) + Umverteilungszonen für die KI
        try:
            sess = session_levels.levels_text(candles, price)
            if sess:
                lines.append(sess)
            zones = session_levels.zones_text(candles, price)
            if zones:
                lines.append(zones)
        except Exception as e:
            logger.debug(f"Session-Levels für {symbol} nicht berechenbar: {e}")
        # Range-/Wick-Analyse (15m/1h): Datenbasis für range_fade / mean_reversion
        try:
            rng = range_analysis.range_text(candles, price)
            if rng:
                lines.append(rng)
        except Exception as e:
            logger.debug(f"Range-Analyse für {symbol} nicht berechenbar: {e}")
        return {"symbol": symbol, "price": price,
                "rsi": round(rsi_5m or rsi_1m, 1), "rsi_1m": round(rsi_1m, 1),
                "text": f"{symbol}: Preis {price:g} | " + " | ".join(lines)}

    async def _user_directives(self, limit: int = 15) -> str:
        rows = await self.db.ai_chat.find({"role": "user"}).sort("ts", -1).limit(limit).to_list(limit)
        rows.reverse()
        if not rows:
            return "(keine)"
        return "\n".join(f"- [{r.get('ts', '')[:16]}] {r.get('text', '')}" for r in rows)

    def _resolve_coins(self, coins) -> List[str]:
        """Normalisiert den Coin-Filter aus dem Chat.

        Leer / None / enthält "ALL" => alle bekannten Symbole. Sonst nur die
        angeforderten Symbole (Reihenfolge von self.symbols beibehalten,
        unbekannte ignorieren)."""
        if not coins:
            return list(self.symbols)
        wanted = {str(c).upper() for c in coins}
        if "ALL" in wanted or "ALLE" in wanted:
            return list(self.symbols)
        filtered = [s for s in self.symbols if s.upper() in wanted]
        return filtered or list(self.symbols)

    async def _open_trades_text(self, allowed: Optional[List[str]] = None) -> str:
        """Text-Übersicht aller offener Trades (jede Strategie, Paper + Live).

        Wenn `allowed` gesetzt ist, werden nur die passenden Symbole detailliert
        gezeigt – die übrigen offenen Positionen erscheinen als kompakte Zeile,
        damit die KI weiß, dass sie existieren (kein Blindflug bei Fokus-Chats).
        """
        rows = await self.db.auto_trades.find({"status": "open"}).to_list(200)
        if not rows:
            return "(keine offenen Positionen)"

        def _age(iso: str) -> str:
            try:
                dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
                if mins < 60:
                    return f"{mins}m"
                if mins < 60 * 24:
                    return f"{mins // 60}h{mins % 60:02d}m"
                return f"{mins // 1440}d{(mins % 1440) // 60}h"
            except (ValueError, TypeError):
                return "?"

        def _fmt(t: Dict) -> str:
            flags = []
            if str(t.get("horizon") or "") == "swing":
                flags.append("SWING" + ("·RUNNER" if t.get("runner") else ""))
            if t.get("setup"):
                flags.append(str(t["setup"]))
            if t.get("tp1_hit"):
                flags.append("TP1✓")
            if t.get("breakeven_moved"):
                flags.append("BE")
            if t.get("profit_secured"):
                flags.append("Profit-Lock")
            if t.get("liquidated"):
                flags.append("LIQ")
            flag_txt = f" [{' '.join(flags)}]" if flags else ""
            strat = t.get("strategy_name") or t.get("strategy_id") or "?"
            qty_rem = t.get("qty_remaining", t.get("qty"))
            pnl = t.get("realized_pnl")
            pnl_txt = f", realPnL {pnl:+.2f}USDT" if isinstance(pnl, (int, float)) else ""
            return (
                f"- id={t.get('id')} {t.get('symbol')} {t.get('side')} "
                f"[{t.get('mode')}/{strat}] Entry {t.get('entry')} "
                f"SL {t.get('sl')} TP1 {t.get('tp1')} TPf {t.get('tpf')} "
                f"Hebel {t.get('leverage')}x Qty {qty_rem}/{t.get('qty')}"
                f"{pnl_txt} Alter {_age(t.get('opened_at'))}{flag_txt}"
            )

        if allowed is None:
            focus = rows
            others: List[Dict] = []
        else:
            allow = {s.upper() for s in allowed}
            focus = [t for t in rows if str(t.get("symbol", "")).upper() in allow]
            others = [t for t in rows if str(t.get("symbol", "")).upper() not in allow]

        lines = [_fmt(t) for t in focus] if focus else ["(keine offenen Positionen im Fokus)"]
        if others:
            paper = sum(1 for t in others if t.get("mode") == "paper")
            live = sum(1 for t in others if t.get("mode") == "live")
            syms = sorted({str(t.get("symbol", "")) for t in others})
            lines.append(
                f"(WEITERE offene Positionen außerhalb des Fokus: {len(others)} "
                f"[paper {paper}, live {live}] auf {', '.join(syms)})"
            )
        return "\n".join(lines)

    async def _today_activity_block(self, allow) -> str:
        """HEUTE-Block für den Chat: Signale + eröffnete/geschlossene Trades.

        Bugfix: Der Chat hatte keinen Zugriff auf die heutigen Signale/Trades
        und behauptete deshalb fälschlich 'heute keine Signale', obwohl
        Signale kamen und Trades eröffnet wurden."""
        today = timeutil.berlin_date()
        sig_rows = await self.db.signals.find().sort("timestamp", -1) \
            .limit(300).to_list(300)
        sigs = [s for s in sig_rows
                if timeutil.berlin_date(s.get("timestamp")) == today]
        trade_rows = await self.db.auto_trades.find().sort("opened_at", -1) \
            .limit(300).to_list(300)
        opened = [t for t in trade_rows
                  if timeutil.berlin_date(t.get("opened_at")) == today]
        closed = [t for t in trade_rows if t.get("closed_at")
                  and timeutil.berlin_date(t.get("closed_at")) == today]
        lines = [f"=== HEUTIGE AKTIVITÄT ({today}, Europe/Berlin) – Fakten aus "
                 "der Datenbank, verbindlicher als dein Gedächtnis ==="]
        if not sigs:
            lines.append("Signale heute: keine")
        else:
            focus = [s for s in sigs
                     if str(s.get("symbol", "")).upper() in allow]
            lines.append(f"Signale heute: {len(sigs)} gesamt, davon "
                         f"{len(focus)} auf den Fokus-Coins")
            for s in sigs[:8]:
                lines.append(
                    f"- {timeutil.berlin_hhmm(s.get('timestamp'))} {s.get('symbol')} "
                    f"{s.get('type')} [{s.get('strategy_name') or s.get('strategy_id')}] "
                    f"Entry {s.get('entry_price')} "
                    f"(Ergebnis: {s.get('result') or 'offen'})")
            if len(sigs) > 8:
                lines.append(f"  … und {len(sigs) - 8} weitere")
        lines.append(f"Trades heute eröffnet: {len(opened)} | "
                     f"heute geschlossen: {len(closed)}")
        for t in opened[:6]:
            lines.append(
                f"- eröffnet {timeutil.berlin_hhmm(t.get('opened_at'))}: "
                f"{t.get('symbol')} {t.get('side')} [{t.get('mode')}] "
                f"{t.get('strategy_name') or t.get('strategy_id')} "
                f"(Status: {t.get('status')})")
        for t in closed[:6]:
            lines.append(
                f"- geschlossen {timeutil.berlin_hhmm(t.get('closed_at'))}: "
                f"{t.get('symbol')} {t.get('side')} [{t.get('mode')}] "
                f"PnL {float(t.get('realized_pnl') or 0):+.2f} USDT")
        return "\n".join(lines)

    async def _context_brief(self, coins=None) -> str:
        cadence = ai_schedule.schedule_text(self.config.get("schedule"),
                                            self.config.get("interval_min", 10))
        parts = [master_prompt.prompt_block(),
                 self._role_context_block(),
                 f"=== DEIN ANALYSE-RHYTHMUS ===\nDu wirst nach diesem Zeitplan aufgerufen: "
                 f"{cadence}. Plane Stops/Ziele so, dass sie bis zum nächsten Aufruf "
                 f"tragfähig sind – aktuell in {self.current_interval()[0]} Minuten.",
                 "PLATTFORM-WISSEN (was diese Website macht – dein Grundverständnis):\n"
                 + PLATFORM_KNOWLEDGE]
        # Letzte Tages-Zusammenfassung als KI-Gedächtnis ganz oben einfügen.
        try:
            last_sum = await self.db.ai_chat.find_one(
                {"role": "summary"}, sort=[("ts", -1)],
            )
            if last_sum and last_sum.get("text"):
                parts.append(
                    f"TAGES-ZUSAMMENFASSUNG ({last_sum.get('day', '')}) – merken & berücksichtigen:\n"
                    + str(last_sum["text"])[:1500]
                )
        except Exception:
            pass
        selected = self._resolve_coins(coins)
        is_all = len(selected) == len(self.symbols)
        allow = {s.upper() for s in selected}

        focus = "ALLE COINS" if is_all else ", ".join(s.replace("USDT", "") for s in selected)
        parts.append(
            "FOKUS-COINS: " + focus + "\n"
            "(Der Nutzer hat den Chat auf diese Coins eingegrenzt – beziehe dich "
            "ausschließlich auf ihre Marktdaten, KI-Strategien, Signale und Trades. "
            "Ignoriere alle anderen Assets, außer der Nutzer fragt ausdrücklich danach.)"
        )

        snaps = []
        for s in selected:
            snap = self._snapshot(s)
            if snap:
                snaps.append(snap["text"])
        parts.append("MARKTDATEN:\n" + ("\n".join(snaps) if snaps else "(noch keine Daten)"))
        if self.config.get("news_enabled"):
            news = await news_feed.get_headlines(8)
            if news:
                parts.append("NEWS:\n" + "\n".join(f"- {n['title']} ({n['source']})" for n in news))
        try:
            macro = await self._macro_block()
            if macro:
                parts.append(macro)
        except Exception:
            pass
        try:
            liq = await self._liquidity_block()
            if liq:
                parts.append(liq)
        except Exception:
            pass
        if self.decisions:
            dec = [f"- {s}: {d.get('action')} ({d.get('confidence')}%) – {d.get('reasoning', '')[:120]}"
                   for s, d in self.decisions.items() if s.upper() in allow]
            if dec:
                parts.append("LETZTE KI-ENTSCHEIDUNGEN:\n" + "\n".join(dec))
        parts.append("OFFENE POSITIONEN:\n" + await self._open_trades_text(selected))
        try:
            parts.append(await self._today_activity_block(allow))
        except Exception as e:
            logger.warning(f"today activity block failed: {e}")
        try:
            if self.learning:
                parts.append("DEINE PERFORMANCE (Signale + Live/Paper-Trades):\n"
                             + await self.learning.performance_text())
                parts.append("DEINE GELERNTEN LEKTIONEN:\n" + await self.learning.lessons_text())
        except Exception:
            pass
        try:
            parts.append("PERFORMANCE DER ANDEREN STRATEGIEN (letzte 14 Tage):\n"
                         + await self._strategy_performance_text())
        except Exception:
            pass
        try:
            parts.append(await strategy_lab.context_text())
        except Exception:
            pass
        try:
            pend = await self.db.ai_proposals.count_documents({"status": "pending"})
            if pend:
                parts.append(f"OFFENE EINSTELLUNGS-VORSCHLÄGE: {pend} "
                             "(warten im Panel auf Bestätigung des Traders)")
        except Exception:
            pass
        cfg = self.config
        parts.append(f"ENGINE: {'AKTIV' if cfg['enabled'] else 'AUS'} | Analyse alle {cfg['interval_min']} min | "
                     f"Min. Konfidenz {cfg['min_confidence']}% | Modell {cfg['provider']}/{cfg['model']} | "
                     f"Autonomie: {cfg.get('autonomy', 'suggest')} | Lernen: "
                     f"{'an' if cfg.get('learning_enabled', True) else 'aus'} | "
                     f"Letzte Analyse: {timeutil.fmt_berlin(self.last_run, fallback='noch keine')} "
                     f"(deutsche Zeit) | Jetzt: {timeutil.fmt_berlin(timeutil.now_iso())} "
                     "(Europe/Berlin, alle Zeitangaben in dieser Zeitzone)")
        return "\n\n".join(parts)

    # ---------------- analysis ----------------
    async def _ai_coin_settings_text(self) -> str:
        """Aktuelle KI-Trader Trade-Einstellungen pro Coin (für Prompt & Self-Tuning)."""
        from core.defaults import DEFAULT_STRATEGY_COIN_CFG
        docs = await self.db.strategy_coin_configs.find(
            {"_id": {"$regex": "^ai_trader_"}}).to_list(100)
        saved = {d["_id"].replace("ai_trader_", "", 1): d.get("config", {}) for d in docs}
        lines = []
        for sym in self.symbols:
            c = {**DEFAULT_STRATEGY_COIN_CFG, **saved.get(sym, {})}
            sl_desc = {"structure": f"Struktur(Lookback {c.get('sl_lookback')})",
                       "fixed": f"fest {c.get('sl_fixed_percent')}%",
                       "atr": f"ATR x{c.get('atr_sl_multiplier', 1.2)}"}.get(
                           c.get("sl_mode"), str(c.get("sl_mode")))
            lev = (f"auto (max {c.get('auto_lev_max')}x)" if c.get("auto_leverage_enabled")
                   else f"{c.get('leverage')}x")
            lines.append(
                f"{sym} [{c.get('mode', 'off')}]: Hebel {lev}, SL {sl_desc}, "
                f"TP1 CRV {c.get('tp1_crv')} ({c.get('tp1_close_percent')}% Teilverkauf), "
                f"TP-Full CRV {c.get('tp_full_crv')}, BE {c.get('be_mode')}, "
                f"Profit-Secure {'an' if c.get('profit_secure_enabled') else 'aus'}")
        return "\n".join(lines)

    async def _macro_block(self) -> str:
        """Externer Makro-Kontext (get_macro_context) als kompakter Text-Block für die KI.

        Deckt die 4 vom Trader gewünschten Quellen ab (Key-Levels, Funding/OI,
        Makro-Kalender mit UTC-No-Trade-Fenstern, DXY/Yield/BTC-Dominanz) plus
        Trump/Truth-Social. Fällt lautlos aus, wenn eine Quelle nicht erreichbar ist.
        """
        if not self.config.get("macro_enabled", True):
            return ""
        try:
            syms = self.config.get("macro_symbols") or macro_context.DEFAULT_SYMBOLS
            ctx = await macro_context.get_macro_context(symbols=list(syms))
        except Exception as e:
            logger.warning(f"macro context failed: {e}")
            return ""

        lines = ["=== EXTERNER MAKRO-KONTEXT (live, alle ~10 min · get_macro_context) ==="]

        mr = ctx.get("market_regime") or {}
        if mr:
            dxy = mr.get("dxy") or {}
            y10 = mr.get("us10y_yield") or {}
            lines.append(
                "MARKT-REGIME: "
                f"BTC-Dominanz {mr.get('btc_dominance_pct', '?')}% | "
                f"DXY {dxy.get('value', '?')} ({dxy.get('chg_pct', '?')}%) | "
                f"US10Y {y10.get('value', '?')}% ({y10.get('chg_pct', '?')}%) | "
                f"Bias: {mr.get('risk_bias', 'neutral')}"
            )

        cal = ctx.get("macro_calendar") or {}
        ntw = cal.get("no_trade_windows_utc") or []
        if ntw:
            lines.append("⛔ NO-TRADE-FENSTER (deutsche Zeit, High-Impact – NICHT traden, Lektion 16):")
            for w in ntw[:5]:
                lines.append(f"  - {w.get('event')}: "
                             f"{timeutil.fmt_berlin(w.get('start_utc'), with_date=False)} → "
                             f"{timeutil.fmt_berlin(w.get('end_utc'), with_date=False)} "
                             f"(am {timeutil.berlin_date(w.get('start_utc'))})")
        upcoming = cal.get("upcoming") or []
        if upcoming:
            nxt = [f"{u.get('event')} ({u.get('importance')}) "
                   f"{timeutil.fmt_berlin(u.get('time_utc'))}"
                   for u in upcoming[:4]]
            lines.append("MAKRO-TERMINE (deutsche Zeit): " + " | ".join(nxt))

        fo = ctx.get("funding_oi") or {}
        for sym, f in fo.items():
            lines.append(
                f"FUNDING/OI {sym}: rate {f.get('funding_rate', '?')} "
                f"(ann. {f.get('funding_annualized_pct', '?')}%), "
                f"OI-Δ 15m {f.get('oi_delta_15m_pct', '?')}% / 1h {f.get('oi_delta_1h_pct', '?')}% / "
                f"4h {f.get('oi_delta_4h_pct', '?')}% → {f.get('squeeze_bias', '?')}"
            )

        kl = ctx.get("key_levels") or {}
        for sym, tfs in kl.items():
            for tf, lv in tfs.items():
                sup = ", ".join(str(x) for x in (lv.get("support") or [])[:3]) or "-"
                res = ", ".join(str(x) for x in (lv.get("resistance") or [])[:3]) or "-"
                lines.append(
                    f"KEY-LEVELS {sym} {tf}: Support [{sup}] | Resistance [{res}] | "
                    f"POC {lv.get('poc')} VAH {lv.get('vah')} VAL {lv.get('val')}"
                )

        trump = ctx.get("trump_truth_social") or {}
        posts = trump.get("latest") or []
        if posts:
            flag = "⚠️ MARKTRELEVANT" if trump.get("market_relevant") else "keine klare Marktrelevanz"
            lines.append(f"TRUMP / TRUTH SOCIAL ({flag}):")
            for p in posts[:3]:
                kw = f" [{', '.join(p.get('market_keywords', []))}]" if p.get("market_keywords") else ""
                lines.append(f"  - [{timeutil.fmt_berlin(p.get('time_utc'), with_date=False)}]{kw} "
                             f"{p.get('text', '')[:160]}")

        lines.append(
            "NUTZUNG: Setze SL/TP an die Key-Levels (POC/VAH/VAL & Support/Resistance). "
            "Beachte Funding/OI für Squeeze-/Trend-Nachhaltigkeit. Handle NICHT in No-Trade-Fenstern. "
            "Berücksichtige DXY/Yield/Dominanz für Bias & Risiko-Budget."
        )
        return "\n".join(lines)

    async def _liquidity_block(self) -> str:
        """Liquiditäts-/Liquidations-Kontext als kompakter Text-Block für die KI.

        Quellen (frei, keyless): Binance/OKX/Bybit über
        ``services/liquidity_data.py`` (Long/Short-Ratio, Open Interest,
        Orderbook-Wände, modellierte Liquidations-Cluster, Live-Liquidationen)
        plus die eigenen „Liquidity Levels" (X-Ray-Pro-Äquivalent) aus
        ``services/liquidity_levels.py``. Fällt lautlos aus, wenn eine Quelle
        nicht erreichbar ist – der Analyse-Zyklus darf daran nie scheitern.
        """
        if not self.config.get("liquidity_enabled", True):
            return ""
        syms = self.config.get("liquidity_symbols") or list(liquidity_data.DEFAULT_SYMBOLS)
        try:
            ctx = await liquidity_data.get_liquidity_context(list(syms))
        except Exception as e:
            logger.warning(f"liquidity context failed: {e}")
            return ""

        use_liq = self.config.get("use_liquidation_data", True)
        use_heat = self.config.get("use_heatmap_data", False)
        lines = ["=== LIQUIDITÄT & LIQUIDATIONEN (live, Multi-Exchange · keyless) ==="]
        for sym in syms:
            b = ctx.get(sym)
            if not isinstance(b, dict):
                continue
            if use_liq:
                ls = b.get("long_short") or {}
                lines.append(
                    f"{sym}: Preis {b.get('price')} | Positionierung {ls.get('bias')} "
                    f"(Retail L/S {ls.get('retail')}, Top-Trader {ls.get('top_trader_pos')}, "
                    f"Taker {ls.get('taker_ratio')}) | OI {b.get('oi_usd')} USD "
                    f"({b.get('oi_trend')})")
            else:
                lines.append(f"{sym}: Preis {b.get('price')}")
            if use_heat:
                mc_ = b.get("liq_clusters_measured") or {}
                below = ", ".join(f"{c.get('price')} ({round((c.get('usd') or 0) / 1e3)}k USD, {c.get('count')}x)"
                                  for c in (mc_.get("below_price") or [])[:3])
                above = ", ".join(f"{c.get('price')} ({round((c.get('usd') or 0) / 1e3)}k USD, {c.get('count')}x)"
                                  for c in (mc_.get("above_price") or [])[:3])
                if below or above:
                    lines.append(f"  GEMESSENE LIQUIDATIONEN {sym} (echte Force-Orders, "
                                 f"letzte {mc_.get('window_h', 4)}h, "
                                 f"{round((mc_.get('total_usd') or 0) / 1e3)}k USD gesamt): "
                                 f"Long-Liqs [{below or '-'}] | Short-Liqs [{above or '-'}]")
                else:
                    lines.append(f"  GEMESSENE LIQUIDATIONEN {sym}: noch keine Daten im "
                                 f"Fenster (Sammler läuft) – KEINE Liquidations-These bilden")
            if use_liq:
                walls = b.get("orderbook_walls") or {}
                bids = ", ".join(f"{w.get('price')} ({round((w.get('usd') or 0) / 1e6, 2)}M)"
                                 for w in (walls.get("bids") or [])[:3]) or "-"
                asks = ", ".join(f"{w.get('price')} ({round((w.get('usd') or 0) / 1e6, 2)}M)"
                                 for w in (walls.get("asks") or [])[:3]) or "-"
                lines.append(f"  ORDERBOOK-WÄNDE {sym}: Bids [{bids}] | Asks [{asks}]")
                rl = b.get("recent_liquidations_5m") or {}
                if rl.get("long_usd") or rl.get("short_usd"):
                    lines.append(
                        f"  LIQUIDATIONEN 5min {sym}: Longs {rl.get('long_usd')} USD, "
                        f"Shorts {rl.get('short_usd')} USD"
                        + (" ⚠️ KASKADE" if rl.get("cascade") else ""))
            # Eigene Liquiditäts-Level (Swings/EQH/EQL/FVG/Volumen-Profil)
            try:
                data = await macro_context.historical_candles(sym, interval="15m", limit=200)
                lvl = liquidity_levels.liquidity_levels(data.get("candles") or [])
                top = ", ".join(f"{x['price']} [{x['type']}, {x['side']}, "
                                f"{x['dist_pct']}%, Stärke {x['strength']}]"
                                for x in (lvl.get("levels") or [])[:5])
                if top:
                    lines.append(f"  LIQUIDITY LEVELS {sym} (15m): {top}")
                vp = lvl.get("volume_profile") or {}
                if vp.get("poc"):
                    lines.append(f"  VOLUMEN-PROFIL {sym}: POC {vp.get('poc')} | "
                                 f"VAH {vp.get('vah')} | VAL {vp.get('val')}")
            except Exception as e:
                logger.debug(f"liquidity levels {sym}: {e}")

        if len(lines) == 1:
            return ""
        usage = ["NUTZUNG:"]
        if use_heat:
            usage.append(
                "GEMESSENE LIQUIDATIONEN sind echte Force-Orders der Börsen (keine Modell-Formel): "
                "Zonen mit hohem Liq-Volumen wurden bereits abgeräumt – dort liegt oft kurzfristig "
                "weniger Brennstoff; frische große Liq-Spitzen nahe am Preis markieren dagegen "
                "erschöpfte Bewegungen (mögliche Umkehr). Ohne Daten im Fenster: keine Liquidations-These.")
        if use_liq:
            usage.append(
                "Orderbook-Wände und Live-Liquidationen sind ECHTE Daten: Setze SL NICHT direkt "
                "hinter eine Orderbook-Wand. Bei Liquidations-Kaskaden (⚠️) erst Stabilisierung abwarten.")
        usage.append(
            "Unberührte Swing-Level/EQH/EQL und der POC sind Ziel-Zonen für TPs. "
            "Order Blocks (ob_bull/ob_bear, Smart-Money-Concept) sind institutionelle "
            "Einstiegs-Zonen: unberührte ob_bull unter dem Preis sind Long-Einstiegs-"
            "Kandidaten beim Retest, ob_bear über dem Preis Short-Kandidaten.")
        lines.append(" ".join(usage))
        return "\n".join(lines)

    async def _strategy_performance_text(self, days: int = 14) -> str:
        """Leserechte auf die anderen Strategien der Website: Winrate der Signale
        + PnL der geschlossenen Trades pro Strategie – als Lern-Kontext für die KI."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            sig_rows = await self.db.signals.aggregate([
                {"$match": {"timestamp": {"$gte": cutoff},
                            "signal_class": {"$ne": "PRE_SIGNAL"}}},
                {"$group": {"_id": "$strategy_id", "total": {"$sum": 1},
                            "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
                            "losses": {"$sum": {"$cond": [{"$eq": ["$result", "loss"]}, 1, 0]}}}},
                {"$sort": {"total": -1}},
            ]).to_list(50)
            trade_rows = await self.db.auto_trades.aggregate([
                {"$match": {"status": "closed", "opened_at": {"$gte": cutoff}}},
                {"$group": {"_id": "$strategy_id", "trades": {"$sum": 1},
                            "pnl": {"$sum": "$realized_pnl"},
                            "wins": {"$sum": {"$cond": [{"$gt": ["$realized_pnl", 0]}, 1, 0]}}}},
            ]).to_list(50)
        except Exception as e:
            return f"(Strategie-Performance nicht verfügbar: {str(e)[:80]})"
        trades_by = {t["_id"]: t for t in trade_rows}
        lines = []
        seen = set()
        for r in sig_rows:
            sid = r["_id"] or "unknown"
            seen.add(sid)
            dec = r["wins"] + r["losses"]
            wr = f"{round(r['wins'] / dec * 100)}%" if dec else "–"
            t = trades_by.get(sid)
            tr_txt = ""
            if t and t.get("trades"):
                twr = round(t["wins"] / t["trades"] * 100)
                tr_txt = f" | Trades: {t['trades']}, PnL {float(t.get('pnl') or 0):+.2f} USDT, Winrate {twr}%"
            me = " (DU SELBST)" if sid == "ai_trader" else ""
            lines.append(f"- {sid}{me}: {r['total']} Signale, Winrate {wr}{tr_txt}")
        for sid, t in trades_by.items():
            if sid in seen or not sid:
                continue
            twr = round(t["wins"] / t["trades"] * 100) if t["trades"] else 0
            lines.append(f"- {sid}: Trades: {t['trades']}, PnL {float(t.get('pnl') or 0):+.2f} USDT, Winrate {twr}%")
        return "\n".join(lines) or "(noch keine Strategie-Daten)"

    async def _deep_report_block(self) -> str:
        """Letzte Tiefenanalyse als Kontext-Block für die regulären Analysen."""
        try:
            doc = await self.db.settings.find_one({"_id": "ai_deep_report"})
        except Exception:
            return ""
        if not doc or not doc.get("report"):
            return ""
        lines = [f"=== LETZTE TIEFENANALYSE ({str(doc.get('ts', ''))[:16]} · "
                 f"{doc.get('model', '?')}, Gewicht {doc.get('weight_label', '?')}) ==="]
        lines.append(str(doc["report"])[:1500])
        recs = doc.get("recommendations") or []
        if recs:
            lines.append("EMPFEHLUNGEN DES TIEFEN-ANALYSTEN (stark gewichten):")
            lines.extend(f"- {str(r)[:200]}" for r in recs[:6])
        return "\n".join(lines)


    async def _other_strategy_params_text(self, limit: int = 14) -> str:
        """Parameter & Timeframes der ANDEREN Strategien – die KI soll daraus lernen."""
        try:
            from strategies.registry import registry as strategy_registry
            metas = strategy_registry.list_all()
        except Exception as e:
            return f"(Strategie-Parameter nicht verfügbar: {str(e)[:80]})"
        settings = getattr(self.scanner, "settings", {}) or {}
        params_by = settings.get("strategy_params", {}) or {}
        tfs = settings.get("strategy_timeframes", {}) or {}
        enabled = set(settings.get("enabled_strategies", []) or [])
        lines = []
        for m in metas[:limit]:
            sid = m.get("id")
            if sid == "ai_trader":
                continue
            p = params_by.get(sid) or m.get("default_params") or {}
            p_txt = ", ".join(f"{k}={v}" for k, v in list(p.items())[:8]) or "Standard-Parameter"
            lines.append(f"- {sid} ({m.get('name')}) [{tfs.get(sid, m.get('timeframe', '1m'))}, "
                         f"{'aktiv' if sid in enabled else 'inaktiv'}]: {p_txt}")
        return "\n".join(lines) or "(keine weiteren Strategien)"

    def _role_context_block(self) -> str:
        return (
            "=== DEINE ROLLE IM SYSTEM (wichtig) ===\n"
            "Du bist EINE Strategie von vielen auf dieser Plattform (strategy_id 'ai_trader'). "
            "Die anderen Strategien laufen parallel und unabhängig weiter – du ersetzt sie nicht "
            "und konkurrierst nicht mit ihnen. Dein Auftrag:\n"
            "1. Den Markt so gut wie möglich verstehen (Struktur, Regime, Liquidität, News).\n"
            "2. Für die aktuelle Lage die passende Strategie WÄHLEN oder eine neue ENTWICKELN – "
            "du bist nicht an ein festes Regelwerk gebunden und darfst dynamisch bleiben.\n"
            "3. Von den anderen Strategien und ihren Parametern LERNEN: was funktioniert in "
            "welchem Marktzustand, welche SL/TP-Logik und Timeframes tragen sich, was scheitert.\n"
            "4. Neue Ideen zuerst im Strategie-Labor testen (Ghost/Paper), nie ungetestet live.\n"
            "5. News-getriebene und live nachjustierte Trades sind Teil deiner Stärke – sie sind "
            "aber NICHT backtestbar; bewerte sie separat von regelbasierten Tests."
        )

    async def _capital_risk_block(self) -> str:
        """Live-Kapital- und Risiko-Status für die KI: Guthaben, freies Kapital
        pro Modus und Kill-Switch-Zustand – damit Positionsgrößen und neue
        Trades zur echten Kontolage passen."""
        from core.state import autotrader
        lines = []
        total = None
        try:
            total = await autotrader._live_total_balance()
            if total is not None:
                lines.append(f"Bitunix-Gesamtguthaben: {total:.2f} USDT")
        except Exception:
            pass
        for scope in ("live", "paper"):
            try:
                alloc = await autotrader.allocated_capital(
                    scope, total=total if scope == "live" else None)
                used = await autotrader.used_margin(scope)
                if alloc is not None:
                    lines.append(f"{scope.upper()}: zugewiesen {alloc:.2f} USDT | "
                                 f"gebunden {used:.2f} | FREI {alloc - used:.2f}")
                else:
                    lines.append(f"{scope.upper()}: gebundene Margin {used:.2f} USDT")
            except Exception:
                continue
        try:
            from services import trade_guard
            gstate = await trade_guard.get_state(self.db)
            if gstate.get("paused"):
                lines.append(f"⚠ KILL-SWITCH AKTIV: {gstate.get('reason')} – "
                             f"Auto-Trading pausiert bis {gstate.get('paused_until')}")
        except Exception:
            pass
        if not lines:
            return ""
        return ("=== KAPITAL & RISIKO-STATUS (live) ===\n" + "\n".join(lines) +
                "\nBerücksichtige das FREIE Kapital bei capital_pct/neuen Trades – "
                "Profit-Lock auf Gewinner kann zusätzlich Kapital freimachen.")

    async def _analysis_extra_blocks(self, purpose: str = "analysis") -> str:
        """MasterPrompt, Rolle, Plattform-Wissen, Performance, Lektionen, Settings,
        Strategie-Labor, Validierung + Autonomie-Regeln.

        purpose:
          "analysis"      – kompletter Kontext (bisheriges Verhalten)
          "analysis_base" – wie analysis, aber OHNE Liquiditäts-Block (der wird
                            in run_analysis nur der Krypto-Gruppe angehängt –
                            Forex/Indizes brauchen ihn nicht -> spart Tokens)
          "trade_review"  – schlanker Kontext für den Trade-Manager (Positions-
                            Review): ohne Strategie-Labor/-Performance, Forschung,
                            ML, Gedächtnis, Deep-Report und Autonomie-Block –
                            der Review ändert keine Configs und braucht das nicht.
        """
        review = purpose == "trade_review"
        parts = [master_prompt.prompt_block(),
                 self._role_context_block()]
        if not review and not self.config.get("lean_prompt", True):
            parts.append(f"=== PLATTFORM-WISSEN ===\n{PLATFORM_KNOWLEDGE}")
        try:
            cap = await self._capital_risk_block()
            if cap:
                parts.append(cap)
        except Exception as e:
            logger.warning(f"AI capital block failed: {e}")
        macro = await self._macro_block()
        if macro:
            parts.append(macro)
        if purpose != "analysis_base":
            try:
                liq = await self._liquidity_block()
                if liq:
                    parts.append(liq)
            except Exception as e:
                logger.warning(f"AI liquidity block failed: {e}")
        try:
            if self.learning:
                parts.append("=== DEINE BISHERIGE PERFORMANCE (echte Ergebnisse) ===\n"
                             + await self.learning.performance_text())
                parts.append("=== DEINE GELERNTEN LEKTIONEN (aus echten Ergebnissen – befolgen!) ===\n"
                             + await self.learning.lessons_text())
        except Exception as e:
            logger.warning(f"AI learning blocks failed: {e}")
        if not review:
            try:
                pb = await ai_playbook.context_text(self.db)
                if pb:
                    parts.append(pb)
            except Exception as e:
                logger.warning(f"AI playbook block failed: {e}")
        if not review:
            try:
                parts.append("=== DEINE AKTUELLEN TRADE-EINSTELLUNGEN (KI Trader, pro Coin) ===\n"
                             + await self._ai_coin_settings_text())
            except Exception:
                pass
            try:
                parts.append(f"=== PERFORMANCE DER ANDEREN STRATEGIEN (letzte 14 Tage – lerne daraus) ===\n"
                             + await self._strategy_performance_text())
            except Exception:
                pass
        if not review and not self.config.get("lean_prompt", True):
            try:
                parts.append("=== PARAMETER DER ANDEREN STRATEGIEN (Vorbilder für eigene Ideen) ===\n"
                             + await self._other_strategy_params_text())
            except Exception:
                pass
        if not review:
            try:
                parts.append(await strategy_lab.context_text())
            except Exception as e:
                logger.warning(f"AI strategy lab block failed: {e}")
            try:
                parts.append(validation_gate.prompt_block())
            except Exception:
                pass
            try:
                deep = await self._deep_report_block()
                if deep:
                    parts.append(deep)
            except Exception:
                pass
            # KI-Ökosystem: Forschungs-Analyst, ML-Labor, Markt-Beobachter, Gedächtnis
            try:
                from services.ai_research import research_analyst
                research = await research_analyst.context_text()
                if research:
                    parts.append(research)
            except Exception as e:
                logger.warning(f"AI research block failed: {e}")
            try:
                from services.ai_ml_lab import ml_lab
                ml = await ml_lab.context_text()
                if ml:
                    parts.append(ml)
            except Exception as e:
                logger.warning(f"AI ml block failed: {e}")
        try:
            from services.ai_market_observer import market_observer
            obs = await market_observer.context_text()
            if obs:
                parts.append(obs)
        except Exception as e:
            logger.warning(f"AI observer block failed: {e}")
        if not review:
            try:
                from services.ai_memory import memory
                mem = await memory.context_text(kinds=["idea", "ml_finding"], per_kind=3)
                if mem:
                    parts.append("=== KI-GEDÄCHTNIS (jüngstes Team-Wissen) ===\n" + mem)
            except Exception as e:
                logger.warning(f"AI memory block failed: {e}")
        try:
            from services.ai_news_watcher import news_watcher
            nw = await news_watcher.context_text()
            if nw:
                parts.append(nw)
        except Exception:
            pass
        if review:
            return "\n\n".join(parts)
        autonomy = self.config.get("autonomy", "suggest")
        if autonomy in ("suggest", "auto"):
            mode_txt = ("Deine Änderungen werden SOFORT automatisch übernommen – sei entsprechend konservativ."
                        if autonomy == "auto" else
                        "Deine Änderungen werden dem Trader als Vorschlag angezeigt und erst nach seiner Bestätigung übernommen.")
            parts.append(
                "=== EINSTELLUNGS-AUTONOMIE (AKTIV) ===\n"
                f"Du darfst deine eigenen Trade-Einstellungen anpassen. {mode_txt}\n"
                "Nutze das optionale JSON-Feld \"config_changes\" (max. 5 Einträge, NUR bei klarem, "
                "datenbasiertem Grund – nicht bei jeder Analyse):\n"
                '[{"symbol": "BTCUSDT", "changes": {"leverage": 8, "sl_fixed_percent": 1.2}, "reason": "kurze Begründung"}]\n'
                'Für Engine-Einstellungen (min_confidence, cooldown_min) nutze "symbol": "ENGINE".\n'
                "STRENG VERBOTEN: max_capital / investierter Betrag / mode (paper/live) – NIE ändern oder vorschlagen.\n"
                + tunable_spec_text())
        else:
            parts.append("=== EINSTELLUNGS-AUTONOMIE (AUS) ===\nGib KEINE config_changes zurück (leere Liste).")
        return "\n\n".join(parts)

    @staticmethod
    def _safe_lev(raw) -> int:
        try:
            return max(0, min(200, int(float(raw or 0))))
        except (TypeError, ValueError):
            return 0

    def _apply_crv_frame(self, sl_pct: float, tp1_pct: float, tpf_pct: float,
                         is_swing: bool) -> tuple:
        """CRV-Spanne aus dem Trade-Rahmen technisch erzwingen (TP1 relativ zu SL)."""
        crv_min = max(1.0, float(self.config.get("crv_min", 1.2) or 1.2))
        crv_max = float(self.config.get("crv_max", 0) or 0)
        cap = 0.25 if is_swing else 0.10
        tp1_pct = max(tp1_pct, min(cap, sl_pct * crv_min))
        if crv_max >= crv_min:
            tp1_pct = min(tp1_pct, sl_pct * crv_max)
            # TP Full ebenfalls deckeln (User 15.06.: keine unrealistisch weiten
            # Ziele mehr): max. 2× crv_max, bei Standard 4 also 8R.
            tpf_pct = min(tpf_pct, sl_pct * crv_max * 2)
        return tp1_pct, max(tpf_pct, tp1_pct)

    def _frame_leverage(self, dec: Dict, is_swing: bool) -> float:
        """Hebel gemäß Hebel-Modus des Trade-Rahmens (0 = Coin-Settings entscheiden)."""
        lev_mode = str(self.config.get("lev_mode", "coin") or "coin")
        swing_cap = float(self.config.get("swing_max_leverage", 8) or 8)
        ai_lev = 0.0
        if lev_mode == "fixed":
            ai_lev = max(1.0, min(100.0, float(self.config.get("lev_fixed", 10) or 10)))
        elif lev_mode == "auto":
            lev_max = max(1.0, min(100.0, float(self.config.get("lev_auto_max", 25) or 25)))
            chosen = float(dec.get("leverage") or 0)
            # KI hat keinen Hebel angegeben -> Coin-Settings entscheiden (kein Zwang)
            ai_lev = min(chosen, lev_max) if chosen > 0 else 0.0
        if is_swing:
            ai_lev = min(ai_lev, swing_cap) if ai_lev > 0 else swing_cap
        return ai_lev

    @staticmethod
    def _parse_json(text: str) -> Dict:
        # Tolerant gegenüber Markdown-Zäunen, Kommentaren und abgeschnittenen
        # Antworten – gültiges JSON wird unverändert gelesen (siehe ai_json.py).
        return parse_json_lenient(text)

    def is_fresh(self, decision: Optional[Dict]) -> bool:
        if not decision or not decision.get("ts"):
            return False
        try:
            ts = datetime.fromisoformat(decision["ts"].replace("Z", "+00:00"))
            max_age = max(self.config.get("interval_min", 10) * 2.5, 20)
            return (datetime.now(timezone.utc) - ts) < timedelta(minutes=max_age)
        except Exception:
            return False

    async def generate_for_role(self, role: str, prompt: str, system: str,
                                temperature: float = 0.4,
                                json_mode: bool = True) -> tuple[str, str, str]:
        """Generierung über die Modell-Kette einer KI-Team-Rolle.

        Kette = Rollen-Modell (bzw. Haupt-Modell) + Provider-Fallbacks +
        Rollen-Fallback-KI; pro Provider werden primärer + Backup-Key probiert.
        Der Analyst kann pro Zeitplan-Fenster ein eigenes Modell haben
        (z.B. starkes Modell zur US-Eröffnung). Rückgabe: (text, provider, model)."""
        chain = role_manager.chain(role, self.config)
        ai_providers.set_current_role(role)
        if role == "analyst":
            w = self.current_window()
            wm = (w or {}).get("model")
            if wm:
                wp = (w or {}).get("provider") or ai_providers.provider_for_model(wm)
                if wp:
                    prepend = ai_providers.same_provider_chain(wp, wm)
                    chain = prepend + [c for c in chain if c not in prepend]
        text, provider, model = await ai_providers.generate_chain(
            chain, prompt, system, temperature=temperature, json_mode=json_mode)
        self._effective_model = model
        self._effective_provider = provider
        try:
            await self._track_tokens(role, model,
                                     (len(prompt) + len(system) + len(text)) // 4)
        except Exception:
            pass
        if model != self.config.get("model"):
            logger.info(f"AI [{role}]: nutzt {provider}/{model} (Haupt-Modell: {self.config.get('model')})")
        return text, provider, model

    def current_window(self) -> Optional[Dict]:
        """Aktives Zeitplan-Fenster (Berlin-Zeit) oder None = Standard."""
        try:
            now = self.scanner.berlin_now()
            minutes = now.hour * 60 + now.minute
        except Exception:
            minutes = timeutil.berlin_minutes()
        return ai_schedule.effective_window(self.config.get("schedule"), minutes)

    async def _track_tokens(self, role: str, model: str, est_tokens: int):
        """Geschätzte Tokens pro Rolle und Tag (deutsche Zeit) mitschreiben –
        Basis für das Kosten-Dashboard (GET /api/ai/token-usage)."""
        if self.db is None or est_tokens <= 0:
            return
        day = timeutil.berlin_date()
        await self.db.ai_token_usage.update_one(
            {"date": day, "role": role},
            {"$inc": {"tokens": int(est_tokens), "calls": 1},
             "$set": {"model": model, "updated_at": _now_iso()}},
            upsert=True)
        try:
            await self._check_token_alert(role, day)
        except Exception:  # noqa: BLE001 – Wächter darf Analysen nie stören
            pass

    # Token-Kosten-Wächter: absolute Untergrenze + Vielfaches des eigenen Schnitts
    TOKEN_ALERT_MIN = 120_000     # unter ~120k Tokens/Tag je Rolle nie warnen
    TOKEN_ALERT_FACTOR = 2.5      # warnen ab 2.5x des 7-Tage-Schnitts der Rolle

    async def _check_token_alert(self, role: str, day: str):
        """Warnt (Glocke + Telegram-Toggle `token_alert`), wenn eine Rolle heute
        ungewöhnlich viele Tokens verbraucht – max. 1x pro Rolle und Tag."""
        doc = await self.db.ai_token_usage.find_one({"date": day, "role": role})
        today = int((doc or {}).get("tokens") or 0)
        if today < self.TOKEN_ALERT_MIN:
            return
        cursor = (self.db.ai_token_usage
                  .find({"role": role, "date": {"$lt": day}})
                  .sort("date", -1).limit(7))
        hist = [int(d.get("tokens") or 0) for d in await cursor.to_list(length=7)]
        baseline = int(sum(hist) / len(hist)) if hist else 0
        if baseline > 0 and today < baseline * self.TOKEN_ALERT_FACTOR:
            return
        from services.notifications import notify_token_spike
        await notify_token_spike(role, today, baseline, len(hist))

    async def _generate_json(self, prompt: str, system: str,
                             role: str = "analyst") -> tuple[str, str]:
        """JSON-Generierung für eine Rolle. Gibt (raw_text, effektives_model) zurück."""
        text, _provider, model = await self.generate_for_role(role, prompt, system)
        return text, model

    def _analysis_groups(self, symbols: List[str]) -> List[tuple]:
        """Symbole für die Analyse gruppieren: Krypto / Forex / Indizes+Rohstoffe.
        Getrennte LLM-Läufe liefern differenziertere, asset-spezifische
        Begründungen als ein einzelner Batch über alle ~20 Assets."""
        if not self.config.get("group_analysis", True):
            return [("Alle Assets", list(symbols))]
        from core import instruments
        by_group = {i.symbol: i.group for i in instruments.INSTRUMENTS}
        buckets = {"Krypto": [], "Forex": [], "Indizes & Rohstoffe": []}
        for s in symbols:
            g = by_group.get(s)
            if g == instruments.GROUP_FOREX:
                buckets["Forex"].append(s)
            elif g == instruments.GROUP_CRYPTO:
                buckets["Krypto"].append(s)
            else:
                buckets["Indizes & Rohstoffe"].append(s)
        return [(k, v) for k, v in buckets.items() if v]

    def _should_skip_group(self, g_label: str, g_syms, snaps, open_syms, manual: bool) -> bool:
        """Smart-Skip: LLM-Lauf einer Gruppe auslassen, wenn seit der letzten
        Analyse praktisch nichts passiert ist.

        Konservativ: nie bei manuellen Läufen, nie bei offenen Positionen in der
        Gruppe, nur wenn die letzte (frische) Entscheidung überall HOLD war und
        sich kein Preis um mehr als `smart_skip_move_pct` bewegt hat. Max. 2
        Skips in Folge – jeder 3. Lauf analysiert IMMER."""
        if manual or not self.config.get("smart_skip", True):
            return False
        if open_syms is None or any(s in open_syms for s in g_syms):
            return False
        if self._group_skips.get(g_label, 0) >= 2:
            return False
        try:
            thr = max(0.02, float(self.config.get("smart_skip_move_pct", 0.15) or 0.15))
        except (TypeError, ValueError):
            thr = 0.15
        for s in g_syms:
            dec = self.decisions.get(s)
            if not self.is_fresh(dec) or not dec.get("price"):
                return False
            if dec.get("action") != "HOLD":
                return False
            try:
                move = abs(float(snaps[s]["price"]) - float(dec["price"])) \
                    / float(dec["price"]) * 100
            except (TypeError, ValueError, ZeroDivisionError, KeyError):
                return False
            if move >= thr:
                return False
        return True

    async def run_analysis(self, manual: bool = False) -> Dict:
        if self._analyzing:
            return {"status": "busy", "detail": "Analyse läuft bereits"}
        if not self.key:
            self.last_error = f"API-Key für Provider '{self.config.get('provider')}' fehlt (Render EnvVars setzen)"
            return {"status": "error", "detail": self.last_error}
        self._analyzing = True
        try:
            symbols = [s for s in self.symbols
                       if (not self.toggle_check or self.toggle_check("ai_trader", s))
                       and len(self.scanner.candle_buffer.get(s, [])) >= 60]
            if not symbols:
                return {"status": "error", "detail": "Keine Coins mit ausreichend Kursdaten"}
            # Nur Coins analysieren, die für den KI Trader freigeschaltet sind
            # (Trade-Modus paper ODER live – 'off' wird übersprungen, spart Tokens)
            try:
                docs = await self.db.strategy_coin_configs.find(
                    {"_id": {"$regex": "^ai_trader_"}}).to_list(200)
                modes = {d["_id"].replace("ai_trader_", "", 1):
                         (d.get("config") or {}).get("mode") for d in docs}
                from core.defaults import DEFAULT_STRATEGY_COIN_CFG
                default_mode = DEFAULT_STRATEGY_COIN_CFG.get("mode", "off")
                active = [s for s in symbols if (modes.get(s) or default_mode) != "off"]
                if active:
                    symbols = active
                else:
                    return {"status": "error",
                            "detail": "Kein Coin für den KI Trader freigeschaltet "
                                      "(Trade-Modus überall 'off')"}
            except Exception as fe:
                logger.warning(f"AI Coin-Freigabe-Filter fehlgeschlagen: {fe}")
            snaps = {s: self._snapshot(s) for s in symbols}
            snaps = {s: v for s, v in snaps.items() if v}

            news_block = "(News deaktiviert)"
            if self.config.get("news_enabled"):
                news = await news_feed.get_headlines(18)
                news_block = "\n".join(f"- {n['title']} ({n['source']})" for n in news) or "(keine News verfügbar)"

            directives = await self._user_directives()
            open_trades = await self._open_trades_text()
            open_syms = None
            try:
                from services.ai_trade_manager import exposure_text
                _open_rows = await self.db.auto_trades.find({"status": "open"}) \
                    .limit(60).to_list(60)
                open_syms = {str(r.get("symbol")) for r in _open_rows}
                open_trades += f"\nExposure: {exposure_text(_open_rows)}"
            except Exception:
                pass
            extra_blocks = await self._analysis_extra_blocks(purpose="analysis_base")
            liq_block = ""
            try:
                liq_block = await self._liquidity_block() or ""
            except Exception as e:
                logger.warning(f"AI liquidity block failed: {e}")
            berlin = self.scanner.berlin_now().strftime("%d.%m.%Y %H:%M")

            capital_block = ""
            max_cap = float(self.config.get("max_capital_per_trade") or 0)
            if max_cap > 0:
                capital_block = (
                    f"=== KAPITAL PRO TRADE ===\n"
                    f"Max. Kapital pro Trade: {max_cap:.2f} USDT Margin. Du entscheidest "
                    f"pro Trade über 'capital_pct' (10-100), wie viel davon du einsetzt. "
                    f"Staffle nach Überzeugung – nutze NICHT automatisch immer 100%.\n\n")

            # Trade-Rahmen (global, vom Trader im AI-Panel vorgegeben): CRV-Spanne
            # und Hebel-Modus, in denen sich die KI pro Trade frei bewegen darf.
            crv_min = float(self.config.get("crv_min", 1.2) or 1.2)
            crv_max = float(self.config.get("crv_max", 0) or 0)
            lev_mode = str(self.config.get("lev_mode", "coin") or "coin")
            frame_lines = [
                "CRV (tp1_pct / sl_pct): mind. " + f"{crv_min:g}"
                + (f", max. {crv_max:g}" if crv_max > 0 else "")
                + " – wähle SL/TP innerhalb dieser Spanne frei passend zum Setup "
                  "(wird technisch erzwungen)."]
            if lev_mode == "auto":
                lam = int(self.config.get("lev_auto_max", 25) or 25)
                frame_lines.append(
                    f"HEBEL: Auto – gib pro Entscheidung zusätzlich das Feld \"leverage\" "
                    f"(ganze Zahl 1-{lam}) an, passend zu Setup-Qualität und SL-Abstand "
                    f"(enger SL + hoher Hebel = hohes Liquidationsrisiko).")
            elif lev_mode == "fixed":
                frame_lines.append(
                    f"HEBEL: fest {int(self.config.get('lev_fixed', 10) or 10)}x "
                    f"(vom Trader vorgegeben – nicht wählbar).")
            # Fee-Wächter in den Prompt: sonst schlägt die KI weiter Stops vor,
            # die technisch geblockt werden (gleiche Logik wie SL-Ratchet-Regel).
            if self.config.get("fee_guard_enabled", True):
                fg_mult = float(self.config.get("fee_guard_mult", 4.0) or 0)
                if fg_mult > 0:
                    frame_lines.append(
                        f"FEE-WÄCHTER: sl_pct muss mind. {fg_mult * 0.12:.2f}% betragen "
                        f"({fg_mult:g}× Roundtrip-Fees ~0.12%). Engere Stops werden technisch "
                        f"geblockt, weil Gebühren das geplante Risiko auffressen würden – "
                        f"wähle SL-Distanzen bewusst darüber.")
            frame_block = ("=== TRADE-RAHMEN (vom Trader vorgegeben) ===\n"
                           + "\n".join(frame_lines) + "\n\n")

            prompt_base = (
                f"Zeit (Berlin): {berlin}\n\n"
                f"{extra_blocks}\n\n"
                f"=== AKTUELLE NEWS ===\n{news_block}\n\n"
                + capital_block + frame_block +
                f"=== ANWEISUNGEN DES TRADERS (höchste Priorität) ===\n{directives}\n\n"
                f"=== OFFENE POSITIONEN ===\n{open_trades}\n\n"
            )

            groups = self._analysis_groups(list(snaps.keys()))
            from services.ai_market_observer import market_observer as _observer
            now = _now_iso()
            emitted = []
            stored = []
            overview_parts = []
            group_errors = []
            skipped_groups = []
            token_estimate = 0
            all_new_strategies = []
            all_config_changes = []
            model_used = None
            for g_label, g_syms in groups:
                if self._should_skip_group(g_label, g_syms, snaps, open_syms, manual):
                    self._group_skips[g_label] = self._group_skips.get(g_label, 0) + 1
                    skipped_groups.append(g_label)
                    logger.info(f"AI Smart-Skip: Gruppe {g_label} praktisch unverändert – "
                                f"LLM-Lauf gespart ({self._group_skips[g_label]}. in Folge)")
                    continue
                self._group_skips[g_label] = 0
                # Liquiditäts-/Liquidations-Daten betreffen nur Krypto – Forex-
                # und Indizes-Läufe bekommen den Block nicht (spart Tokens).
                g_liq = (liq_block + "\n\n") if (liq_block and g_label in ("Krypto", "Alle Assets")) else ""
                g_prompt = (
                    prompt_base + g_liq
                    + f"=== MARKTDATEN (Multi-Timeframe) – FOKUS-GRUPPE: {g_label} ===\n"
                    + "\n".join(snaps[s]["text"] for s in g_syms)
                    + f"\n\nDieser Lauf behandelt NUR die Gruppe {g_label} "
                      f"({', '.join(g_syms)}). Analysiere jedes dieser Symbole in der "
                      "Tiefe (Struktur, Level, Korrelationen INNERHALB der Gruppe) und "
                      "gib für jedes genau eine Entscheidung mit individueller, "
                      "asset-spezifischer Begründung als JSON zurück."
                )
                try:
                    lean = bool(self.config.get("lean_prompt", True))
                    sys_prompt = ANALYSIS_SYSTEM_LEAN if lean else ANALYSIS_SYSTEM
                    # Fix 0.4: Prompt-Stand dieses Laufs (wird an jede Decision gebunden)
                    pv = prompt_version_info("lean" if lean else "full")
                    raw, model_used = await self._generate_json(g_prompt, sys_prompt)
                    data = self._parse_json(raw)
                except Exception as ge:
                    group_errors.append(f"{g_label}: {str(ge)[:120]}")
                    logger.error(f"AI Gruppen-Analyse {g_label} fehlgeschlagen: {ge}")
                    continue
                ov = str(data.get("market_overview", "")).strip()
                if ov:
                    overview_parts.append(f"[{g_label}] {ov}" if len(groups) > 1 else ov)
                all_new_strategies += list(data.get("new_strategies") or [])
                all_config_changes += list(data.get("config_changes") or [])
                for d in data.get("decisions", []):
                    sym = d.get("symbol")
                    if sym not in snaps or sym not in g_syms:
                        continue
                    action = str(d.get("action", "HOLD")).upper()
                    if action not in ("LONG", "SHORT", "HOLD"):
                        action = "HOLD"
                    horizon = "swing" if (str(d.get("horizon") or "").lower() == "swing"
                                          and self.config.get("swing_enabled", True)) else "scalp"
                    setup = (ai_playbook.normalize_setup(d.get("setup"))
                             if action in ("LONG", "SHORT") else None)
                    dec = {
                        "id": str(uuid.uuid4()),
                        "symbol": sym,
                        "action": action,
                        "confidence": max(0, min(100, int(d.get("confidence", 0) or 0))),
                        "horizon": horizon,
                        "setup": setup,
                        "runner": bool(d.get("runner")) and horizon == "swing",
                        "sl_pct": float(d.get("sl_pct", 0.6) or 0.6),
                        "tp1_pct": float(d.get("tp1_pct", 0.9) or 0.9),
                        "tpf_pct": float(d.get("tpf_pct", 1.8) or 1.8),
                        "capital_pct": max(10, min(100, int(d.get("capital_pct", 100) or 100))),
                        "leverage": self._safe_lev(d.get("leverage")),
                        "news_impact": d.get("news_impact", "neutral"),
                        "reasoning": str(d.get("reasoning", ""))[:500],
                        "size_reason": (str(d.get("size_reason", "") or "")[:200] or None),
                        "levels_reason": (str(d.get("levels_reason", "") or "")[:200] or None),
                        "strategy_candidate_id": str(d.get("strategy_candidate_id") or "") or None,
                        "price": snaps[sym]["price"],
                        "rsi": snaps[sym]["rsi"],
                        "ts": now,
                        "signaled": False,
                        "model": model_used,
                        "model_weight": ai_providers.model_weight(model_used),
                        "prompt_version": pv,
                        "entry_market_snapshot": _observer.entry_snapshot(sym),
                    }
                    # Gate v1 (Phase 5): Shadow-Prediction nur loggen, nie blocken
                    if action in ("LONG", "SHORT"):
                        from services.ml_gate import ml_gate
                        dec["gate_shadow"] = ml_gate.shadow_predict(dec)
                    self.decisions[sym] = dec
                    stored.append(dec)
                    if (action in ("LONG", "SHORT")
                            and dec["confidence"] >= self.config["min_confidence"]
                            and self.scanner.is_trading_session("ai_trader")):
                        ok = await self._emit_signal(dec)
                        if ok:
                            dec["signaled"] = True
                            emitted.append(f"{sym} {action}")
                    # Datensammel-Modus (Phase 4): unterhalb der Live-Schwelle,
                    # aber über der Sammel-Schwelle -> Paper-Trade (data_collection)
                    if (not dec["signaled"]
                            and action in ("LONG", "SHORT")
                            and bool(self.config.get("collection_enabled"))
                            and dec["confidence"] >= int(self.config.get(
                                "collection_min_confidence", 60) or 0)
                            and self.scanner.is_trading_session("ai_trader")):
                        ok = await self._emit_signal(dec, collection=True)
                        if ok:
                            dec["signaled"] = True
                            dec["data_collection"] = True
                            emitted.append(f"{sym} {action} (Datensammlung)")
            if group_errors and not stored:
                self.last_error = "Gruppen-Analyse fehlgeschlagen: " + "; ".join(group_errors)[:280]
                return {"status": "error", "detail": self.last_error}
            if skipped_groups and not stored and not group_errors:
                # Kompletter Zyklus per Smart-Skip gespart: kein Feed-Eintrag (kein Spam)
                self.last_run = now
                self.last_error = None
                logger.info(f"AI Smart-Skip: Zyklus ohne LLM-Call ({', '.join(skipped_groups)})")
                return {"status": "ok", "decisions": 0, "signals": [],
                        "skipped_groups": skipped_groups,
                        "overview": "Smart-Skip: Markt seit letzter Analyse praktisch "
                                    "unverändert – LLM-Analyse gespart"}
            if stored:
                await self.db.ai_decisions.insert_many([dict(x) for x in stored])

            # Neue Strategie-Ideen der KI -> Strategie-Labor (Ghost-Phase)
            new_candidates = []
            for spec in all_new_strategies[:3]:
                if not isinstance(spec, dict):
                    continue
                try:
                    res = await strategy_lab.create_candidate(spec, source="ki")
                    if res.get("status") == "ok":
                        new_candidates.append(res["candidate"]["id"])
                except Exception as se:
                    logger.error(f"Strategie-Kandidat konnte nicht angelegt werden: {se}")

            # Self-Tuning: von der KI gewünschte Einstellungs-Änderungen verarbeiten
            cfg_results = []
            try:
                cfg_results = await self._handle_config_changes(
                    all_config_changes, source="analysis")
            except Exception as ce:
                logger.error(f"AI config changes failed: {ce}")
            # Autonomie "auto": zurückgestellte Wünsche erneut prüfen und
            # anwenden, sobald die Datenlage sie bestätigt.
            try:
                await self.review_parked_proposals()
            except Exception as re_:
                logger.error(f"Autonomie-Review fehlgeschlagen: {re_}")

            feed_entry = {
                "id": str(uuid.uuid4()),
                "role": "analysis",
                "text": "\n\n".join(overview_parts)[:1800],
                "group_errors": group_errors or None,
                "decisions": [{"symbol": x["symbol"], "action": x["action"],
                               "confidence": x["confidence"], "reasoning": x["reasoning"],
                               "horizon": x.get("horizon", "scalp"),
                               "setup": x.get("setup"),
                               "signaled": x["signaled"]} for x in stored],
                "emitted": emitted,
                "config_changes": [{"symbol": p["symbol"], "changes": p["changes"],
                                    "status": p["status"]} for p in cfg_results],
                "new_candidates": new_candidates,
                "skipped_groups": skipped_groups or None,
                "token_estimate": token_estimate or None,
                "manual": manual,
                "model": model_used,
                "ts": now,
            }
            await self.db.ai_chat.insert_one(dict(feed_entry))
            self.last_run = now
            self.last_error = None
            logger.info(f"AI analysis done ({model_used}): {len(stored)} decisions, "
                        f"{len(emitted)} signals ({emitted}), ~{token_estimate} Tokens (Schätzung)")
            return {"status": "ok", "decisions": len(stored), "signals": emitted,
                    "overview": feed_entry["text"], "model": model_used}
        except Exception as e:
            self.last_error = str(e)[:300]
            logger.error(f"AI analysis failed: {e}")
            return {"status": "error", "detail": self.last_error}
        finally:
            self._analyzing = False

    async def _today_risk(self) -> tuple:
        """Realisierter PnL und Trade-Anzahl der KI für den heutigen Handelstag."""
        try:
            day = self.scanner.berlin_date()
            rows = await self.db.auto_trades.find(
                {"strategy_id": "ai_trader", "trade_date": day},
                {"realized_pnl": 1, "status": 1}).to_list(300)
        except Exception as e:
            logger.warning(f"Tages-Risiko nicht ermittelbar: {e}")
            return None, None
        pnl = sum(float(r.get("realized_pnl") or 0) for r in rows)
        self._day_risk_cache = {"date": day, "realized_pnl": round(pnl, 4),
                                "trades": len(rows),
                                "limit_usdt": master_prompt.rules.get("max_daily_loss_usdt"),
                                "max_trades": master_prompt.rules.get("max_trades_per_day")}
        return round(pnl, 4), len(rows)

    async def _diversification_gate(self, sym: str, dec: Dict,
                                    collection: bool = False) -> tuple:
        """Diversifikations- & Playbook-Guards (technisch erzwungen):
        Richtungs-Klumpen, Entry-Cluster in derselben Zone, gesperrte Setups.
        Datensammel-Modus: lockereres Richtungs-Limit, Playbook-Sperren
        gelten nicht (Paper-Daten über gesperrte Setups sind fürs ML wertvoll)."""
        try:
            open_rows = await self.db.auto_trades.find(
                {"status": "open", "strategy_id": "ai_trader"},
                {"symbol": 1, "side": 1, "entry": 1}).to_list(100)
        except Exception as e:
            logger.warning(f"Diversifikations-Guard: offene Trades nicht lesbar: {e}")
            open_rows = []
        max_same = (int(self.config.get("collection_max_same_direction", 5) or 0)
                    if collection else int(self.config.get("max_same_direction", 3) or 0))
        allowed, why = ai_playbook.diversification_check(
            open_rows, sym, dec["action"], float(dec.get("price") or 0),
            max_same_direction=max_same,
            min_dist_pct=float(self.config.get("min_entry_distance_pct", 0.5) or 0),
            setup=dec.get("setup"),
            correlation_guard=bool(self.config.get("correlation_guard", True)))
        if not allowed:
            return False, why
        if not collection:
            blocked = ai_playbook.disabled_reason(dec.get("setup"))
            if blocked:
                return False, blocked
        return True, ""

    async def _emit_signal(self, dec: Dict, collection: bool = False) -> bool:
        sym = dec["symbol"]
        # ---- MasterPrompt: oberstes Gebot, technisch erzwungen ----
        try:
            open_ai_trades = await self.db.auto_trades.count_documents(
                {"status": "open", "strategy_id": "ai_trader"})
        except Exception:
            open_ai_trades = None
        allowed, why = master_prompt.check_trade(
            sym, dec["action"], confidence=dec.get("confidence"), open_trades=open_ai_trades)
        if allowed and not collection:
            # Tages-Risiko-Limits zielen auf echtes Kapital – Sammel-Trades
            # sind immer Paper und werden davon nicht gebremst.
            day_pnl, day_trades = await self._today_risk()
            allowed, why = master_prompt.check_day(day_pnl, day_trades)
        if allowed:
            # ---- Diversifikation & Playbook (Richtungs-/Cluster-Guard) ----
            allowed, why = await self._diversification_gate(sym, dec, collection)
        if not allowed:
            logger.info(f"AI-Signal blockiert ({sym} {dec['action']}): {why}"
                        + (" [Datensammlung]" if collection else ""))
            dec["blocked_by"] = why
            if not collection:
                try:
                    await self.db.ai_chat.insert_one({
                        "id": str(uuid.uuid4()), "role": "governance",
                        "text": f"Trade {dec['action']} {sym} blockiert – {why}",
                        "ts": _now_iso()})
                except Exception:
                    pass
            return False
        if collection:
            cd = int(self.config.get("collection_cooldown_min", 30) or 0) * 60
            if cd and (time.time() - self._last_collection_ts.get(sym, 0)) < cd:
                return False
            cooldown = 0
        else:
            cooldown = self.config.get("cooldown_min", 45) * 60
        if cooldown:
            max_per_coin = max(1, min(5, int(self.config.get("max_trades_per_coin", 1) or 1)))
            if max_per_coin > 1:
                # KI-Trader mit mehreren Slots: Cooldown gilt PRO TRADE statt pro
                # Coin. Solange auf dem Coin noch freie Slots (max_trades_per_coin)
                # offen sind, wird der Coin-Cooldown übersprungen, damit die Slots
                # zeitnah gefüllt werden. Erst wenn die Slots voll sind, bremst der
                # Cooldown (die Slot-Obergrenze setzt on_signal ohnehin durch).
                try:
                    open_count = await self.db.auto_trades.count_documents(
                        {"symbol": sym, "status": "open", "strategy_id": "ai_trader"})
                except Exception:
                    open_count = 0
                if open_count >= max_per_coin and \
                        (time.time() - self._last_signal_ts.get(sym, 0)) < cooldown:
                    return False
            elif (time.time() - self._last_signal_ts.get(sym, 0)) < cooldown:
                return False
        entry = float(dec["price"])
        if entry <= 0:
            return False
        # Makro-Parameter des Strategie-Kandidaten haben Vorrang vor den
        # spontanen Prozentwerten der Analyse (individuelle Feinjustierung
        # je eigener Strategie, siehe services/ai_strategy_lab.py).
        macro = strategy_lab.macro_params(dec.get("strategy_candidate_id"))
        is_swing = str(dec.get("horizon") or "scalp") == "swing"
        runner = bool(dec.get("runner")) and is_swing
        sl_input = macro.get("sl_fixed_percent", dec["sl_pct"])
        if is_swing:
            # Swing: eigene, weite Grenzen (niedriger Hebel wird unten gedeckelt)
            sl_pct = max(0.005, min(0.12, float(sl_input) / 100))
            tp1_pct = max(sl_pct * 1.2, min(0.25, dec["tp1_pct"] / 100))
            tpf_pct = max(tp1_pct, min(0.60, dec["tpf_pct"] / 100))
            if runner:
                tpf_pct = max(tpf_pct, 0.50)  # Endziel sehr weit -> Trailing übernimmt
        else:
            sl_pct = max(0.15, min(5.0, float(sl_input))) / 100
            if macro.get("tp1_crv"):
                tp1_pct = min(0.08, sl_pct * float(macro["tp1_crv"]))
            else:
                tp1_pct = max(sl_pct * 1.2, min(0.08, dec["tp1_pct"] / 100))
            if macro.get("tpf_crv"):
                tpf_pct = min(0.15, max(tp1_pct, sl_pct * float(macro["tpf_crv"])))
            else:
                tpf_pct = max(tp1_pct, min(0.15, dec["tpf_pct"] / 100))
        # Trade-Rahmen: CRV-Spanne (global vorgegeben) technisch erzwingen –
        # TP1 wird in [SL*crv_min, SL*crv_max] geklemmt, TP-Full folgt.
        tp1_pct, tpf_pct = self._apply_crv_frame(sl_pct, tp1_pct, tpf_pct, is_swing)
        sign = 1 if dec["action"] == "LONG" else -1
        sl = entry * (1 - sign * sl_pct)
        tp1 = entry * (1 + sign * tp1_pct)
        tpf = entry * (1 + sign * tpf_pct)
        crv = round(abs(tp1 - entry) / abs(entry - sl), 2) if entry != sl else 0
        # ---- Strategie-Labor: Kandidaten erst nach Ghost-Phase + Freigabe live ----
        cand_id = dec.get("strategy_candidate_id")
        stage = strategy_lab.execution_stage(cand_id) if cand_id else None
        if cand_id and stage in ("ghost", "live_pending", "unknown", "rejected", None):
            if stage in ("unknown", "rejected", None):
                logger.info(f"AI-Signal {sym}: Kandidat {cand_id} unbekannt/abgelehnt "
                            "-> Kandidaten-Bezug verworfen")
                cand_id, stage = None, None
            else:
                if (time.time() - self._last_ghost_ts.get(sym, 0)) < cooldown:
                    return False
                try:
                    await strategy_lab.record_ghost_trade(
                        cand_id, sym, dec["action"], entry, sl, tp1,
                        reason=dec.get("reasoning", ""))
                    # Eigener Cooldown für Ghost-Trades: ein simulierter Test darf
                    # echte Signale auf dem Coin nicht blockieren.
                    self._last_ghost_ts[sym] = time.time()
                except Exception as ge:
                    logger.error(f"Ghost-Trade fehlgeschlagen: {ge}")
                return False
        now = self.scanner.berlin_now()
        rules_met = {"ai_active": True, "ai_direction": True, "ai_confidence": True, "ai_news": True}
        signal = {
            "symbol": sym,
            "type": dec["action"],
            "signal_class": "SIGNAL",
            "entry_price": round(entry, 6),
            "stop_loss": round(sl, 6),
            "take_profit_1": round(tp1, 6),
            "take_profit_full": round(tpf, 6),
            "crv": crv,
            "rsi": dec.get("rsi", 0),
            "ema_fast": 0,
            "ema_slow": 0,
            "rules_met": rules_met,
            "rules_met_count": 4,
            "rules_total": 4,
            "timestamp": _now_iso(),
            "trade_date": self.scanner.berlin_date(),
            "hour": now.hour,
            "weekday": now.weekday(),
            "session": self.scanner.get_current_session(),
            "strategy_id": "ai_trader",
            "strategy_name": "KI Trader",
            "status": "active",
            "ai_confidence": dec["confidence"],
            "ai_reasoning": dec["reasoning"],
            "ai_news_impact": dec.get("news_impact", "neutral"),
            "ai_size_reason": dec.get("size_reason"),
            "ai_levels_reason": dec.get("levels_reason"),
            "decision_id": dec.get("id"),
            "use_ai_levels": bool(self.config.get("use_ai_levels")) or is_swing,
            "ai_horizon": "swing" if is_swing else "scalp",
            "ai_setup": dec.get("setup"),
            "ai_runner": runner,
            "ai_candidate_id": cand_id,
            "cfg_overrides": strategy_lab.trade_overrides(cand_id) if cand_id else None,
            "force_paper": bool(cand_id and stage == "paper"),
            "force_paper_reason": ("Strategie-Kandidat noch nicht für Live freigegeben"
                                  if cand_id and stage == "paper" else None),
        }
        if collection:
            signal["data_collection"] = True
            signal["force_paper"] = True
            signal["force_paper_reason"] = "Datensammel-Modus (nur Paper)"
            signal["collection_reason"] = (
                "below_live_conf"
                if dec["confidence"] < int(self.config.get("min_confidence", 65) or 0)
                else "live_blocked")
        if is_swing:
            # Swing: eigener Timeframe-Schlüssel (Anti-Stacking blockiert Scalps nicht)
            signal["timeframe"] = "swing"
        # Hebel-Modus (global, AI-Panel): coin = Coin-Settings entscheiden
        # (bisheriges Verhalten) | auto = KI wählt pro Trade bis lev_auto_max |
        # fixed = fester Hebel. Swing bleibt immer auf swing_max_leverage gedeckelt.
        ai_lev = self._frame_leverage(dec, is_swing)
        if ai_lev > 0:
            signal["ai_leverage"] = ai_lev
        # Max. Kapital pro Trade (nur KI-Trader): Die KI hat pro Trade selbst
        # entschieden, wie viel Kapital (capital_pct vom Max) sie einsetzt.
        max_cap = float(self.config.get("max_capital_per_trade") or 0)
        if max_cap > 0:
            signal["ai_max_capital"] = max_cap
            signal["ai_capital_pct"] = max(10, min(100, int(dec.get("capital_pct", 100) or 100)))
        try:
            ok = await self.signal_cb(signal)
            if ok:
                if collection:
                    self._last_collection_ts[sym] = time.time()
                else:
                    self._last_signal_ts[sym] = time.time()
                dec["signal_id"] = signal.get("id")
            return bool(ok)
        except Exception as e:
            logger.error(f"AI signal emit failed for {sym}: {e}")
            return False

    # ---------------- self-tuning (KI ändert eigene Trade-Einstellungen) ----------------
    async def _current_cfg_values(self, scope: str, symbol: Optional[str], keys) -> Dict:
        if scope == "engine":
            return {k: self.config.get(k) for k in keys}
        if scope == "candidate":
            # Makro-Parameter einer eigenen KI-Strategie (Kandidat)
            cand = await strategy_lab.get(symbol) or {}
            macro = cand.get("macro_params") or {}
            from core.defaults import DEFAULT_STRATEGY_COIN_CFG
            return {k: macro.get(k, DEFAULT_STRATEGY_COIN_CFG.get(k)) for k in keys}
        from core.defaults import DEFAULT_STRATEGY_COIN_CFG
        doc = await self.db.strategy_coin_configs.find_one({"_id": f"ai_trader_{symbol}"})
        saved = doc.get("config", {}) if doc else {}
        merged = {**DEFAULT_STRATEGY_COIN_CFG, **saved}
        return {k: merged.get(k) for k in keys}

    async def _apply_changes(self, scope: str, symbol: Optional[str], changes: Dict):
        if scope == "engine":
            await self.update_config(dict(changes))
            return
        if scope == "candidate":
            await strategy_lab.update_macro_params(symbol, dict(changes))
            return
        key = f"ai_trader_{symbol}"
        doc = await self.db.strategy_coin_configs.find_one({"_id": key})
        saved = doc.get("config", {}) if doc else {}
        saved.update(changes)
        await self.db.strategy_coin_configs.replace_one(
            {"_id": key}, {"_id": key, "config": saved}, upsert=True)
        try:
            from core.state import autotrader  # lazy: kein Zyklus beim Import
            autotrader.config.setdefault("strategy_coin_configs", {})[key] = saved
        except Exception:
            pass

    async def _macro_gate(self, prop: Dict, macro_keys: List[str], current: Dict,
                          stats: Dict, scope: str, symbol: Optional[str]):
        """Struktur-Parameter (SL, CRV, Hebel ...) brauchen mehrere Bestätigungen
        und dürfen nur in kleinen Schritten wandern.

        Die Bestätigungen werden aus früheren Vorschlägen derselben Richtung
        gezählt – ein einzelner (Verlust-)Trade verschiebt damit nichts."""
        window_days = int(validation_gate.settings.get("macro_confirm_window_days", 14))
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        sample = await self._macro_sample(stats, scope, symbol)
        worst = None
        clamped_any = False
        changes = dict(prop["changes"])
        for key in macro_keys:
            cur, proposed = current.get(key), changes[key]
            direction = "up" if (cur is None or float(proposed) > float(cur)) else "down"
            try:
                confirmations = 1 + await self.db.ai_proposals.count_documents({
                    "scope": scope,
                    "symbol": prop["symbol"],
                    f"changes.{key}": {"$exists": True},
                    "macro_direction": direction,
                    "ts": {"$gte": since},
                })
            except Exception:
                confirmations = 1
            gate = validation_gate.macro(sample, confirmations)
            gate["key"] = key
            gate["direction"] = direction
            if worst is None or (not gate["validated"] and worst["validated"]):
                worst = gate
            value, was_clamped = validation_gate.clamp(key, cur, proposed)
            changes[key] = value
            clamped_any = clamped_any or was_clamped
        prop["changes"] = changes
        prop["macro_direction"] = worst.get("direction") if worst else None
        return (worst or {"validated": True, "reason": "keine Makro-Parameter"}), clamped_any

    async def _macro_sample(self, stats: Dict, scope: str, symbol: Optional[str]) -> int:
        """Stichprobe für Makro-Änderungen – pro Kandidat aus dessen eigenen Trades."""
        if scope == "candidate" and symbol:
            try:
                return await self.db.auto_trades.count_documents(
                    {"ai_candidate_id": symbol, "status": "closed"})
            except Exception:
                return 0
        return ai_validation.sample_size(stats, scope, symbol)

    def _tuning_guard(self, changes: Dict) -> str:
        """Self-Tuning-Guard: Engine-Änderungen der KI nur innerhalb der vom
        Trader definierten Leitplanken (Spanne einstellbar, KI darf sie nie
        ändern). Rückgabe: leerer String = ok, sonst Begründung."""
        lo = int(self.config.get("tune_conf_min", 55) or 0)
        hi = int(self.config.get("tune_conf_max", 75) or 100)
        cd_max = int(self.config.get("tune_cooldown_max", 45) or 0)
        if "min_confidence" in changes:
            try:
                v = int(changes["min_confidence"])
            except (TypeError, ValueError):
                return "min_confidence kein gültiger Wert"
            if not (lo <= v <= hi):
                return (f"min_confidence {v}% liegt außerhalb der Autonomie-Spanne "
                        f"{lo}–{hi}% – nur der Trader darf das bestätigen")
        if "cooldown_min" in changes:
            try:
                c = int(changes["cooldown_min"])
            except (TypeError, ValueError):
                return "cooldown_min kein gültiger Wert"
            if cd_max and c > cd_max:
                return (f"cooldown_min {c} min über dem Autonomie-Limit "
                        f"{cd_max} min – nur der Trader darf das bestätigen")
        return ""

    async def _normalize_auto_tuned(self):
        """Boot-Heilung: Wenn der AKTUELLE Engine-Wert außerhalb der Leitplanken
        liegt UND nachweislich von der KI selbst gesetzt wurde (auto_applied-
        Proposal mit exakt diesem Wert), wird er auf die Leitplanken-Grenze
        zurückgeholt. Manuell vom Trader gesetzte Werte werden NIE angefasst;
        jedes Proposal wird höchstens einmal normalisiert (guard_normalized)."""
        if self.db is None:
            return
        lo = int(self.config.get("tune_conf_min", 55) or 0)
        hi = int(self.config.get("tune_conf_max", 75) or 100)
        cd_max = int(self.config.get("tune_cooldown_max", 45) or 0)
        fixes: Dict = {}
        notes: List[str] = []
        for key, bound, ok in (
                ("min_confidence", hi, lambda v: lo <= v <= hi),
                ("cooldown_min", cd_max, lambda v: not cd_max or v <= cd_max)):
            try:
                cur = int(self.config.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if ok(cur):
                continue
            prop = await self.db.ai_proposals.find_one({
                "scope": "engine", "status": "auto_applied",
                f"changes.{key}": cur, "guard_normalized": {"$ne": True},
            }, sort=[("ts", -1)])
            if not prop:
                continue  # nicht von der KI gesetzt (oder schon normalisiert)
            fixes[key] = bound
            notes.append(f"{key} {cur} → {bound}")
            await self.db.ai_proposals.update_one(
                {"id": prop.get("id")}, {"$set": {"guard_normalized": True}})
        if not fixes:
            return
        self.config.update(fixes)
        await self.db.settings.update_one(
            {"_id": "ai_trader_config"}, {"$set": fixes}, upsert=True)
        msg = ("Self-Tuning-Guard: Die KI hatte ihre Engine-Werte außerhalb der "
               "Autonomie-Leitplanken gesetzt – zurückgeholt: " + ", ".join(notes) +
               ". Die Spanne ist in den KI-Einstellungen anpassbar.")
        logger.warning(msg)
        try:
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "governance",
                "text": msg, "ts": _now_iso()})
        except Exception:
            pass

    async def _handle_config_changes(self, raw_list: List, source: str = "analysis") -> List[Dict]:
        """Validiert KI-Änderungswünsche gegen die Whitelist und wendet sie an
        (autonomy=auto) bzw. legt sie als bestätigungspflichtige Vorschläge ab
        (autonomy=suggest). max_capital & mode sind hart gesperrt."""
        autonomy = self.config.get("autonomy", "suggest")
        if autonomy not in ("suggest", "auto") or not raw_list:
            return []
        upper_syms = {s.upper(): s for s in self.symbols}
        # Datenbasis für die Validierung (nur für KI-initiierte Änderungen nötig)
        stats: Dict = {}
        if source != "user" and self.learning:
            try:
                stats = await self.learning.gather_stats()
            except Exception as e:
                logger.warning(f"Validierungs-Statistik nicht verfügbar: {e}")
        results = []
        for item in raw_list[:6]:
            if not isinstance(item, dict):
                continue
            symbol_raw = str(item.get("symbol", "")).upper().strip()
            cand_ref = str(item.get("strategy_candidate_id") or "").strip() or (
                symbol_raw.lower() if symbol_raw.lower().startswith("cand_") else "")
            if cand_ref:
                scope, symbol = "candidate", cand_ref
                if not await strategy_lab.get(cand_ref):
                    logger.info(f"AI config change: Kandidat {cand_ref} unbekannt – übersprungen")
                    continue
            else:
                scope = "engine" if symbol_raw in ("ENGINE", "GLOBAL", "") else "coin"
                symbol = upper_syms.get(symbol_raw)
                if scope == "coin" and not symbol:
                    continue
            # Kandidaten nutzen dieselbe Whitelist wie Coin-Configs
            valid, rejected = validate_changes(item.get("changes") or {},
                                               scope="coin" if scope == "candidate" else scope)
            if rejected:
                logger.info(f"AI config change abgelehnt ({symbol_raw}): {rejected}")
            if not valid:
                continue
            current = await self._current_cfg_values(scope, symbol, valid.keys())
            valid = {k: v for k, v in valid.items() if current.get(k) != v}
            if not valid:
                continue
            prop = {
                "id": str(uuid.uuid4()),
                "ts": _now_iso(),
                "scope": scope,
                "symbol": symbol if scope in ("coin", "candidate") else "ENGINE",
                "changes": valid,
                "current": {k: current.get(k) for k in valid},
                "reason": str(item.get("reason", ""))[:300],
                "source": source,
                "status": "pending",
            }
            # 1. MasterPrompt (oberstes Gebot) – harte Sperre
            master_ok, master_why = master_prompt.check_changes(valid)
            if not master_ok and source != "user":
                prop["status"] = "blocked_master"
                prop["block_reason"] = master_why
                await self.db.ai_proposals.insert_one(dict(prop))
                results.append(prop)
                logger.info(f"AI config change durch MasterPrompt blockiert: {master_why}")
                continue
            # 2. Datenbasis-Validierung – ohne ausreichende Stichprobe nur parken
            if source != "user":
                macro_keys = [k for k in valid if ai_validation.is_macro_key(k)]
                normal_keys = [k for k in valid if k not in macro_keys]
                gate = validation_gate.change(stats, scope, symbol) if normal_keys else \
                    {"validated": True, "reason": "nur Struktur-Parameter", "sample": 0}
                prop["validation"] = gate
                if normal_keys and not gate.get("validated"):
                    prop["status"] = "needs_data"
                    await self.db.ai_proposals.insert_one(dict(prop))
                    results.append(prop)
                    logger.info(f"AI config change geparkt (needs_data): {gate.get('reason')}")
                    continue
                if macro_keys:
                    macro_gate, clamped = await self._macro_gate(
                        prop, macro_keys, current, stats, scope, symbol)
                    prop["macro_validation"] = macro_gate
                    prop["clamped"] = clamped
                    if not macro_gate.get("validated"):
                        prop["status"] = "needs_confirmation"
                        await self.db.ai_proposals.insert_one(dict(prop))
                        results.append(prop)
                        logger.info(f"AI Makro-Änderung geparkt: {macro_gate.get('reason')}")
                        continue
                    valid = prop["changes"]
            # 3. Self-Tuning-Guard: Engine-Werte außerhalb der Leitplanken
            # werden NIE automatisch angewendet – nur Vorschlag an den Trader.
            if scope == "engine" and source != "user":
                guard_why = self._tuning_guard(valid)
                if guard_why:
                    prop["status"] = "needs_confirmation"
                    prop["guard_reason"] = guard_why
                    await self.db.ai_proposals.insert_one(dict(prop))
                    results.append(prop)
                    logger.info(f"AI Engine-Änderung durch Autonomie-Leitplanke "
                                f"geparkt: {guard_why}")
                    continue
            if autonomy == "auto" or source == "user":
                try:
                    await self._apply_changes(scope, symbol, valid)
                    prop["status"] = "auto_applied"
                    prop["decided_at"] = _now_iso()
                except Exception as e:
                    prop["status"] = "error"
                    prop["error"] = str(e)[:200]
            await self.db.ai_proposals.insert_one(dict(prop))
            results.append(prop)
        if results:
            applied = [p for p in results if p["status"] == "auto_applied"]
            pending = [p for p in results if p["status"] == "pending"]
            parked = [p for p in results if p["status"] == "needs_data"]
            # Autonomie "auto": geparkte Wünsche (needs_data/needs_confirmation)
            # still sammeln statt den Trader mit Hinweisen zu fluten – die KI
            # schlägt sie automatisch erneut vor, sobald die Daten reichen.
            if autonomy == "auto" and source != "user" and not applied and not pending \
                    and not any(p["status"] in ("blocked_master", "error") for p in results):
                return results
            unconfirmed = [p for p in results if p["status"] == "needs_confirmation"]
            blocked = [p for p in results if p["status"] == "blocked_master"]
            txt = []
            if applied:
                txt.append("Ich habe meine Trade-Einstellungen angepasst (Autonomie: automatisch).")
            if pending:
                txt.append("Ich schlage Änderungen an meinen Trade-Einstellungen vor – bitte bestätigen oder ablehnen.")
            if parked:
                txt.append(f"{len(parked)} Änderung(en) warten auf mehr Daten "
                           f"({parked[0].get('validation', {}).get('reason', '')}).")
            if unconfirmed:
                txt.append(f"{len(unconfirmed)} Struktur-Änderung(en) (SL/CRV/Hebel) warten auf "
                           f"weitere Bestätigungen: "
                           f"{unconfirmed[0].get('macro_validation', {}).get('reason', '')}")
            if blocked:
                txt.append(f"{len(blocked)} Änderung(en) verstoßen gegen den MasterPrompt "
                           f"und wurden verworfen ({blocked[0].get('block_reason', '')}).")
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "config",
                "text": " ".join(txt),
                "items": [{"proposal_id": p["id"], "symbol": p["symbol"],
                           "changes": p["changes"], "current": p["current"],
                           "reason": p["reason"], "status": p["status"],
                           "validation": p.get("validation"),
                           "macro_validation": p.get("macro_validation"),
                           "clamped": p.get("clamped"),
                           "block_reason": p.get("block_reason")} for p in results],
                "source": source, "ts": _now_iso(),
            })
        return results

    # ---------------- geparkte Vorschläge (Autonomie "auto") ----------------
    async def _close_proposal(self, pid: str, status: str, extra: Optional[Dict] = None):
        """Status eines Vorschlags final setzen und im KI-Feed spiegeln."""
        patch = {"status": status, "decided_at": _now_iso(), **(extra or {})}
        await self.db.ai_proposals.update_one({"id": pid}, {"$set": patch})
        try:
            await self.db.ai_chat.update_many(
                {"role": "config", "items.proposal_id": pid},
                {"$set": {"items.$.status": status}})
        except Exception:
            pass

    async def review_parked_proposals(self, limit: int = 30) -> Dict:
        """Autonomie "auto": geparkte Änderungswünsche (needs_data /
        needs_confirmation) erneut gegen die AKTUELLE Datenlage prüfen und
        automatisch anwenden, sobald die Validierung sie freigibt.

        Damit muss der Trader im autonomen Modus nichts mehr bestätigen – die
        bestehende Validierung (Stichprobe, Bestätigungen, Schrittweite,
        MasterPrompt) bleibt aber vollständig wirksam. Im Modus "suggest"
        passiert hier nichts: dort entscheidet der Trader per Karte."""
        if self.db is None or self.config.get("autonomy") != "auto":
            return {"reviewed": 0, "applied": 0}
        try:
            rows = await self.db.ai_proposals.find({
                "status": {"$in": ["needs_data", "needs_confirmation"]},
                "source": {"$ne": "user"},
            }).sort("ts", -1).limit(limit).to_list(limit)
        except Exception as e:
            logger.warning(f"Geparkte Vorschläge konnten nicht geladen werden: {e}")
            return {"reviewed": 0, "applied": 0}
        if not rows:
            return {"reviewed": 0, "applied": 0}
        stats: Dict = {}
        if self.learning:
            try:
                stats = await self.learning.gather_stats()
            except Exception as e:
                logger.warning(f"Validierungs-Statistik nicht verfügbar: {e}")
        applied: List[Dict] = []
        for prop in rows:
            prop.pop("_id", None)
            pid = prop.get("id")
            scope = prop.get("scope", "coin")
            sym_field = prop.get("symbol")
            symbol = None if scope == "engine" else sym_field
            changes = dict(prop.get("changes") or {})
            if not pid or not changes:
                continue
            ok, why = master_prompt.check_changes(changes)
            if not ok:
                await self._close_proposal(pid, "blocked_master", {"block_reason": why})
                continue
            # Self-Tuning-Guard: geparkte Engine-Vorschläge außerhalb der
            # Leitplanken bleiben Vorschlag – der Trader muss sie bestätigen.
            if scope == "engine":
                guard_why = self._tuning_guard(changes)
                if guard_why:
                    await self.db.ai_proposals.update_one({"id": pid}, {"$set": {
                        "guard_reason": guard_why, "reviewed_at": _now_iso()}})
                    continue
            macro_keys = [k for k in changes if ai_validation.is_macro_key(k)]
            normal_keys = [k for k in changes if k not in macro_keys]
            gate = validation_gate.change(stats, scope, sym_field) if normal_keys else \
                {"validated": True, "reason": "nur Struktur-Parameter", "sample": 0}
            if normal_keys and not gate.get("validated"):
                await self.db.ai_proposals.update_one(
                    {"id": pid}, {"$set": {"validation": gate, "reviewed_at": _now_iso()}})
                continue
            current = await self._current_cfg_values(scope, symbol, changes.keys())
            if macro_keys:
                probe = {"changes": changes, "symbol": sym_field}
                macro_gate, clamped = await self._macro_gate(
                    probe, macro_keys, current, stats, scope, sym_field)
                if not macro_gate.get("validated"):
                    await self.db.ai_proposals.update_one({"id": pid}, {"$set": {
                        "macro_validation": macro_gate, "clamped": clamped,
                        "reviewed_at": _now_iso()}})
                    continue
                changes = probe["changes"]
            changes = {k: v for k, v in changes.items() if current.get(k) != v}
            if not changes:
                await self._close_proposal(pid, "obsolete")
                continue
            try:
                await self._apply_changes(scope, symbol, changes)
            except Exception as e:
                await self._close_proposal(pid, "error", {"error": str(e)[:200]})
                continue
            await self._close_proposal(pid, "auto_applied", {
                "changes": changes, "validation": gate,
                "current": {k: current.get(k) for k in changes}})
            applied.append({"proposal_id": pid, "symbol": prop.get("symbol"),
                            "changes": changes,
                            "current": {k: current.get(k) for k in changes},
                            "reason": prop.get("reason", ""), "status": "auto_applied"})
        if applied:
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "config",
                "text": f"{len(applied)} zurückgestellte Änderung(en) sind jetzt durch die "
                        "Datenlage bestätigt und wurden automatisch übernommen "
                        "(Autonomie: automatisch).",
                "items": applied, "source": "auto_review", "ts": _now_iso(),
            })
            logger.info(f"Autonomie-Review: {len(applied)} geparkte Änderung(en) angewendet")
        return {"reviewed": len(rows), "applied": len(applied)}

    async def actionable_proposals(self, limit: int = 30) -> List[Dict]:
        """Vorschläge, die WIRKLICH eine Entscheidung des Traders brauchen.

        Im autonomen Modus ist das immer eine leere Liste – dort verwaltet die
        KI ihre Wünsche selbst (siehe `review_parked_proposals`). Damit kann das
        Frontend die Karten ohne Race-Condition ausblenden."""
        if self.config.get("autonomy") == "auto":
            return []
        rows = await self.db.ai_proposals.find({
            "status": {"$in": ["pending", "needs_confirmation", "needs_data"]},
        }).sort("ts", -1).limit(max(1, min(100, limit))).to_list(100)
        for r in rows:
            r.pop("_id", None)
        return rows

    async def comment_on_user_change(self, topic: str, detail: str) -> Dict:
        """Der Trader hat etwas geändert – die KI sagt ehrlich ihre Meinung dazu.
        Blockiert nichts, landet als Eintrag (role='opinion') im KI-Feed."""
        if self.db is None:
            return {"status": "skipped", "detail": "keine DB"}
        if not self.key:
            return {"status": "skipped", "detail": "kein API-Key"}
        try:
            stats_txt = await self.learning.performance_text() if self.learning else ""
            lessons_txt = await self.learning.lessons_text() if self.learning else ""
            prompt = (
                f"{master_prompt.prompt_block()}\n\n"
                f"=== ÄNDERUNG DES TRADERS ({topic}) ===\n{detail}\n\n"
                f"=== DEINE PERFORMANCE ===\n{stats_txt}\n\n"
                f"=== DEINE LEKTIONEN ===\n{lessons_txt}\n\n"
                "Bewerte diese Änderung ehrlich. Sie gilt ohnehin – aber wenn deine Daten "
                "dagegen sprechen, sage klar warum."
            )
            text, provider, model = await self.generate_for_role(
                "chat", prompt, OPINION_SYSTEM, temperature=0.3)
            data = self._parse_json(text)
            entry = {
                "id": str(uuid.uuid4()), "role": "opinion",
                "topic": topic,
                "stance": str(data.get("stance", "neutral"))[:20],
                "text": str(data.get("comment", ""))[:900],
                "risk": str(data.get("risk", ""))[:300],
                "model": model, "ts": _now_iso(),
            }
            await self.db.ai_chat.insert_one(dict(entry))
            logger.info(f"KI-Meinung zu '{topic}': {entry['stance']} ({provider}/{model})")
            return {"status": "ok", **entry}
        except Exception as e:
            logger.warning(f"KI-Meinung zu '{topic}' fehlgeschlagen: {e}")
            return {"status": "error", "detail": str(e)[:200]}

    async def list_proposals(self, status: Optional[str] = None, limit: int = 40) -> List[Dict]:
        q = {"status": status} if status else {}
        rows = await self.db.ai_proposals.find(q).sort("ts", -1).limit(limit).to_list(limit)
        for r in rows:
            r.pop("_id", None)
        return rows

    async def decide_proposal(self, pid: str, approve: bool) -> Optional[Dict]:
        prop = await self.db.ai_proposals.find_one({"id": pid})
        # Der Trader darf auch geparkte Vorschläge (fehlende Daten/Bestätigungen)
        # freigeben – seine Entscheidung braucht keine Validierung.
        if not prop or prop.get("status") not in ("pending", "needs_data",
                                                 "needs_confirmation"):
            return None
        if approve:
            symbol = None if prop.get("scope") == "engine" else prop.get("symbol")
            await self._apply_changes(prop.get("scope", "coin"), symbol, prop.get("changes") or {})
        new_status = "applied" if approve else "rejected"
        await self.db.ai_proposals.update_one(
            {"id": pid}, {"$set": {"status": new_status, "decided_at": _now_iso()}})
        try:
            await self.db.ai_chat.update_many(
                {"role": "config", "items.proposal_id": pid},
                {"$set": {"items.$.status": new_status}})
        except Exception:
            pass
        prop.pop("_id", None)
        prop["status"] = new_status
        return prop

    # ---------------- housekeeping (hourly cleanup + daily reset + summary) ----------------
    async def _persist_housekeeping(self):
        try:
            await self.db.settings.update_one(
                {"_id": "ai_trader_housekeeping"},
                {"$set": {
                    "last_cleanup_hour": self._last_cleanup_hour,
                    "last_reset_date": self._last_reset_date,
                }},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"AI housekeeping persist failed: {e}")

    async def _cleanup_old_analyses(self) -> int:
        """Löscht alle Nachrichten mit role='analysis' bis auf die neueste.
        User-, Assistant- und Summary-Nachrichten bleiben unangetastet."""
        try:
            latest = await self.db.ai_chat.find_one(
                {"role": "analysis"}, sort=[("ts", -1)],
            )
            if not latest:
                return 0
            query = {"role": "analysis"}
            if latest.get("id"):
                query["id"] = {"$ne": latest["id"]}
            else:
                query["_id"] = {"$ne": latest["_id"]}
            result = await self.db.ai_chat.delete_many(query)
            return result.deleted_count or 0
        except Exception as e:
            logger.error(f"AI hourly cleanup failed: {e}")
            return 0

    async def _collect_daily_facts(self, day_iso: str) -> Dict:
        """Sammelt die Fakten des abgelaufenen Tages aus ai_chat (vor dem Löschen)
        + ai_decisions. `day_iso` = YYYY-MM-DD (Berlin) des Tages, der zusammengefasst wird."""
        # Alles was aktuell im Chat liegt = Tages-Nachrichten (Hourly-Cleanup hat alte
        # analysis-Einträge bereits weg-geräumt, außerdem darf hier eine ältere Summary
        # liegen – die kommt in den Archivierungs-Snapshot).
        chat_docs = await self.db.ai_chat.find().sort("ts", 1).to_list(length=None)

        # ai_decisions: filtere nach Berlin-Datum. ts ist ISO in UTC.
        all_dec = await self.db.ai_decisions.find({"ts": {"$exists": True}}).sort("ts", 1).to_list(length=None)
        day_dec = []
        for d in all_dec:
            try:
                dt = datetime.fromisoformat(str(d.get("ts", "")).replace("Z", "+00:00"))
                if dt.astimezone(BERLIN_TZ).strftime("%Y-%m-%d") == day_iso:
                    day_dec.append(d)
            except Exception:
                continue

        analyses = [c for c in chat_docs if c.get("role") == "analysis"]
        directives = [c for c in chat_docs if c.get("role") == "user"]
        assistants = [c for c in chat_docs if c.get("role") == "assistant"]
        summaries_prev = [c for c in chat_docs if c.get("role") == "summary"]

        signals = [d for d in day_dec if d.get("signaled")]
        actions = {"LONG": 0, "SHORT": 0, "HOLD": 0}
        for d in day_dec:
            a = str(d.get("action", "HOLD")).upper()
            if a in actions:
                actions[a] += 1

        overviews = [str(a.get("text") or "").strip() for a in analyses if a.get("text")]

        return {
            "day": day_iso,
            "chat_docs": chat_docs,
            "day_decisions": day_dec,
            "counts": {
                "analyses": len(analyses),
                "decisions": len(day_dec),
                "signals": len(signals),
                "long": actions["LONG"],
                "short": actions["SHORT"],
                "hold": actions["HOLD"],
                "directives": len(directives),
                "assistant_msgs": len(assistants),
                "prev_summaries": len(summaries_prev),
            },
            "signals": [f"{s.get('symbol')} {s.get('action')} ({s.get('confidence')}%)" for s in signals],
            "directives": [str(d.get("text") or "").strip() for d in directives if d.get("text")],
            "overviews": overviews,
        }

    def _statistical_summary(self, facts: Dict) -> str:
        """Fallback-Zusammenfassung, wenn die LLM nicht erreichbar ist."""
        c = facts["counts"]
        cfg = self.config
        parts = [
            f"Tages-Zusammenfassung ({facts['day']}) – statistischer Fallback (LLM nicht erreichbar).",
            f"• Analysen: {c['analyses']} · Entscheidungen: {c['decisions']} "
            f"(LONG {c['long']} / SHORT {c['short']} / HOLD {c['hold']}) · "
            f"Ausgelöste Signale: {c['signals']}",
        ]
        if facts["signals"]:
            parts.append("• Signale: " + ", ".join(facts["signals"][:12]))
        if facts["overviews"]:
            latest_ov = facts["overviews"][-1][:220]
            parts.append(f"• Letzter Marktüberblick: {latest_ov}")
        if facts["directives"]:
            dirs = " | ".join(d[:120] for d in facts["directives"][-6:])
            parts.append(f"• Trader-Direktiven (aktuell aktiv): {dirs}")
        else:
            parts.append("• Trader-Direktiven: (keine vom Nutzer im Chat gesetzt)")
        parts.append(
            f"• Aktive Konfiguration: Provider {cfg.get('provider')} / Modell {cfg.get('model')} · "
            f"Intervall {cfg.get('interval_min')} min · Min. Konfidenz {cfg.get('min_confidence')}% · "
            f"Cooldown {cfg.get('cooldown_min')} min · News {'an' if cfg.get('news_enabled') else 'aus'}"
        )
        return "\n".join(parts)

    async def _llm_daily_summary(self, facts: Dict) -> Optional[str]:
        """Generiert die Zusammenfassung via aktivem LLM-Provider. Gibt None bei Fehler."""
        if not self.key:
            return None
        cfg = self.config
        c = facts["counts"]
        directives_block = "\n".join(f"- {d}" for d in facts["directives"][-15:]) or "(keine)"
        signals_block = "\n".join(f"- {s}" for s in facts["signals"][:20]) or "(keine)"
        overviews_block = "\n".join(f"- {o[:220]}" for o in facts["overviews"][-6:]) or "(keine)"
        prompt = (
            f"Zusammenfassung für Tag: {facts['day']} (Europe/Berlin)\n\n"
            f"KENNZAHLEN:\n"
            f"- Analysen: {c['analyses']}\n"
            f"- Entscheidungen: {c['decisions']} (LONG {c['long']} / SHORT {c['short']} / HOLD {c['hold']})\n"
            f"- Ausgelöste Signale: {c['signals']}\n\n"
            f"SIGNALE:\n{signals_block}\n\n"
            f"MARKTÜBERBLICKE (chronologisch, ältester zuerst):\n{overviews_block}\n\n"
            f"TRADER-DIREKTIVEN (vom Nutzer im Chat gesetzt, definieren wonach gerade getradet wird):\n{directives_block}\n\n"
            f"AKTIVE KONFIGURATION:\n"
            f"- Provider/Modell: {cfg.get('provider')} / {cfg.get('model')}\n"
            f"- Analyse-Intervall: {cfg.get('interval_min')} min\n"
            f"- Min. Konfidenz: {cfg.get('min_confidence')}%\n"
            f"- Trade-Cooldown: {cfg.get('cooldown_min')} min\n"
            f"- News-Feed: {'an' if cfg.get('news_enabled') else 'aus'}\n\n"
            f"Erstelle nun die kompakte deutsche Tages-Zusammenfassung wie im System-Prompt beschrieben."
        )
        provider = cfg.get("provider", "gemini")
        try:
            text, _p, _m = await self.generate_for_role(
                "summarizer", prompt, SUMMARY_SYSTEM, temperature=0.4, json_mode=False)
            return text or None
        except Exception as e:
            logger.warning(f"Daily summary {provider} failed: {e}")
            return None

    async def _daily_reset(self, prev_day_iso: str) -> Dict:
        """Archiviert Tages-Chat + Entscheidungen, generiert eine markierte
        Tages-Zusammenfassung und pinnt sie oben im Chat.

        Reihenfolge (WICHTIG: kein Datenverlust bei LLM- oder DB-Fehlern):
        1) Fakten sammeln
        2) Summary-Text generieren (LLM + Fallback)
        3) Archivieren
        4) Cutoff-Delete (nur Vortags-Nachrichten `ts < Mitternacht Berlin`) –
           nach Mitternacht neu eingetroffene Nachrichten bleiben erhalten
        5) Summary einfügen (ts = Mitternacht Berlin des neuen Tages, damit
           sie chronologisch VOR allen Neuer-Tag-Nachrichten liegt)
        6) Ältere gepinnte Summaries entpinnen (nur die neueste ist pinned)
        """
        # 1) Fakten sammeln – zwingend VOR jeglicher Löschaktion.
        facts = await self._collect_daily_facts(prev_day_iso)

        # 2) Zusammenfassung generieren (LLM + Fallback). Der Fallback liefert
        #    IMMER einen Text, damit wir nie mit leerer Summary weiterlaufen.
        text = await self._llm_daily_summary(facts)
        used_fallback = False
        if not text:
            text = self._statistical_summary(facts)
            used_fallback = True

        # Cutoff = Mitternacht Berlin des NEUEN Tages (= Ende von prev_day_iso).
        # Alle Nachrichten mit ts < cutoff gehören zum Vortag und werden gelöscht.
        try:
            cutoff_dt_berlin = datetime.strptime(prev_day_iso, "%Y-%m-%d") \
                .replace(tzinfo=BERLIN_TZ) + timedelta(days=1)
        except Exception:
            cutoff_dt_berlin = datetime.now(BERLIN_TZ)
        cutoff_utc_iso = cutoff_dt_berlin.astimezone(timezone.utc).isoformat()

        # 3) Archivieren – KI vergisst nichts.
        archive_batch = str(uuid.uuid4())
        archive_ts = _now_iso()
        archive_errors = False
        try:
            if facts["chat_docs"]:
                docs = []
                for c in facts["chat_docs"]:
                    d = dict(c)
                    d.pop("_id", None)
                    d["archive_batch"] = archive_batch
                    d["archive_day"] = prev_day_iso
                    d["archived_at"] = archive_ts
                    d["source"] = "ai_chat"
                    docs.append(d)
                await self.db.ai_chat_archive.insert_many(docs)
            if facts["day_decisions"]:
                docs = []
                for c in facts["day_decisions"]:
                    d = dict(c)
                    d.pop("_id", None)
                    d["archive_batch"] = archive_batch
                    d["archive_day"] = prev_day_iso
                    d["archived_at"] = archive_ts
                    d["source"] = "ai_decisions"
                    docs.append(d)
                await self.db.ai_chat_archive.insert_many(docs)
        except Exception as e:
            archive_errors = True
            logger.error(f"AI daily archive failed: {e}")
            # Best-Effort: Archiv-Fehler blockieren den Chat-Reset nicht,
            # sonst würde die Engine ewig mit vollem Chat weiterlaufen.

        # 4) Cutoff-Delete: nur echte Vortags-Nachrichten löschen. Verhindert,
        #    dass Nachrichten aus dem neuen Tag (Race Condition zwischen 00:00
        #    und dem Ende der Summary-Generierung) versehentlich mit-gelöscht
        #    werden.
        delete_ok = False
        try:
            await self.db.ai_chat.delete_many({"ts": {"$lt": cutoff_utc_iso}})
            delete_ok = True
        except Exception as e:
            logger.error(f"AI daily chat clear failed: {e}")
            # Wir versuchen trotzdem, die Summary einzufügen (siehe 5) – der
            # Nutzer soll wenigstens den Tages-Bericht sehen.

        # 5) Summary einfügen. ts = cutoff (Mitternacht Berlin des neuen Tages),
        #    dadurch sortiert die Summary chronologisch VOR allen neu
        #    eingetroffenen Nachrichten und bleibt auch beim `sort("ts", -1)`
        #    Fenster relevant, wenn wir sie in chat_history() explizit pinnen.
        cfg = self.config
        summary_doc = {
            "id": str(uuid.uuid4()),
            "role": "summary",
            "pinned": True,
            "text": text,
            "day": prev_day_iso,
            "counts": facts["counts"],
            "directives": facts["directives"][-15:],
            "active_config": {
                "provider": cfg.get("provider"),
                "model": cfg.get("model"),
                "interval_min": cfg.get("interval_min"),
                "min_confidence": cfg.get("min_confidence"),
                "cooldown_min": cfg.get("cooldown_min"),
                "news_enabled": cfg.get("news_enabled"),
            },
            "fallback": used_fallback,
            "archive_batch": archive_batch,
            "archive_errors": archive_errors,
            "ts": cutoff_utc_iso,
        }
        summary_inserted = False
        try:
            await self.db.ai_chat.insert_one(dict(summary_doc))
            summary_inserted = True
        except Exception as e:
            logger.error(f"AI daily summary insert failed: {e}")

        # 6) Nur die NEUESTE Summary bleibt gepinnt – alle älteren entpinnen.
        #    Verhindert Doppel-Pins nach mehreren Reset-Läufen und stellt sicher,
        #    dass das Frontend immer genau eine gepinnte Summary sieht.
        if summary_inserted:
            try:
                await self.db.ai_chat.update_many(
                    {"role": "summary", "pinned": True, "id": {"$ne": summary_doc["id"]}},
                    {"$set": {"pinned": False}},
                )
            except Exception as e:
                logger.warning(f"AI daily summary un-pin previous failed: {e}")

        # 7) Automatischer Lernlauf: die KI analysiert die eröffneten/geschlossenen
        #    Trades des Tages und leitet daraus Lektionen für die Zukunft ab.
        learning_status = None
        if summary_inserted and self.learning \
                and self.config.get("learning_enabled", True) and self.key:
            try:
                lres = await self.learning.run_learning(trigger="daily_summary")
                learning_status = lres.get("status")
                logger.info(f"AI daily learning ({prev_day_iso}): {learning_status}")
            except Exception as e:
                logger.warning(f"AI daily learning failed: {e}")

        logger.info(
            f"AI daily reset done for {prev_day_iso}: archived {len(facts['chat_docs'])} chat + "
            f"{len(facts['day_decisions'])} decisions, summary via "
            f"{'FALLBACK' if used_fallback else 'LLM'}, delete_ok={delete_ok}, "
            f"summary_inserted={summary_inserted}"
        )
        return {
            "day": prev_day_iso,
            "archived_chat": len(facts["chat_docs"]),
            "archived_decisions": len(facts["day_decisions"]),
            "fallback": used_fallback,
            "summary_id": summary_doc["id"],
            "summary_inserted": summary_inserted,
            "delete_ok": delete_ok,
            "archive_errors": archive_errors,
            "learning": learning_status,
        }

    async def _run_housekeeping(self):
        """Wird vom run_loop jede Iteration angetriggert. Führt bei Bedarf
        (1) stündliches Analyse-Cleanup und (2) 00:00-Berlin Tages-Reset aus.

        Der Tages-Reset-Marker (`_last_reset_date`) wird AUSSCHLIESSLICH nach
        einem nachweislich erfolgreichen Reset fortgeschrieben – schlägt der
        Reset fehl (z. B. DB-Fehler beim Insert der Summary), wird er im
        nächsten Loop-Durchlauf automatisch erneut versucht. Nach 5 erfolglosen
        Versuchen wird der Marker zwangs-fortgeschrieben und ein Error geloggt,
        damit die Engine nicht dauerhaft blockiert bleibt."""
        async with self._housekeeping_lock:
            now_berlin = datetime.now(BERLIN_TZ)
            hour_key = now_berlin.strftime("%Y%m%d%H")
            date_key = now_berlin.strftime("%Y-%m-%d")

            # (A) Tages-Reset zuerst: neuer Kalendertag Berlin?
            if self._last_reset_date and date_key != self._last_reset_date:
                prev_day = self._last_reset_date

                # Retry-Zähler pro anstehendem Vortag verwalten.
                if self._reset_retry_day != prev_day:
                    self._reset_retry_day = prev_day
                    self._reset_retry_count = 0

                # Notbremse: nach 5 Fehlversuchen Marker fortschreiben, damit
                # die Engine nicht dauerhaft am selben Tag festhängt.
                if self._reset_retry_count >= 5:
                    logger.error(
                        f"Daily reset for {prev_day} skipped after "
                        f"{self._reset_retry_count} failed attempts – marker advanced."
                    )
                    self._last_reset_date = date_key
                    self._last_cleanup_hour = hour_key
                    self._reset_retry_day = None
                    self._reset_retry_count = 0
                    await self._persist_housekeeping()
                    return

                success = False
                try:
                    result = await self._daily_reset(prev_day)
                    # Erfolg = Summary konnte tatsächlich in ai_chat geschrieben
                    # werden. Nur dann darf der Marker fortgeschritten werden,
                    # sonst würde die Summary für diesen Tag ausfallen.
                    success = bool(result.get("summary_inserted"))
                except Exception as e:
                    logger.error(
                        f"Daily reset error "
                        f"(attempt {self._reset_retry_count + 1}/5) for {prev_day}: {e}"
                    )

                if not success:
                    self._reset_retry_count += 1
                    logger.warning(
                        f"Daily reset for {prev_day} not successful, "
                        f"will retry ({self._reset_retry_count}/5)."
                    )
                    # Marker NICHT fortschreiben -> nächster Loop-Durchlauf retried.
                    return

                # Erst nach echtem Erfolg: Lernlauf + Marker fortschreiben.
                try:
                    if self.learning and self.config.get("learning_enabled", True) and self.key:
                        await self.learning.run_learning(trigger="daily")
                except Exception as e:
                    logger.error(f"Daily learning error: {e}")
                self._last_reset_date = date_key
                # Nach Reset ist auch die aktuelle Stunde als 'gecleant' zu markieren
                # (der Chat ist ohnehin leer bis auf die Summary).
                self._last_cleanup_hour = hour_key
                self._reset_retry_day = None
                self._reset_retry_count = 0
                await self._persist_housekeeping()
                return

            # (B) Stündliches Cleanup – exakt zur vollen Stunde einmal pro Stunde.
            if self._last_cleanup_hour and hour_key != self._last_cleanup_hour:
                try:
                    removed = await self._cleanup_old_analyses()
                    if removed:
                        logger.info(f"AI hourly cleanup: {removed} alte Analyse-Nachricht(en) entfernt.")
                except Exception as e:
                    logger.error(f"Hourly cleanup error: {e}")
                self._last_cleanup_hour = hour_key
                await self._persist_housekeeping()

    async def force_daily_summary(self) -> Dict:
        """Manueller Trigger (Endpoint): erzwingt Reset + Summary für den 'aktuellen
        Berlin-Tag' (bzw. dem Marker `_last_reset_date`).

        Marker wird NUR nach nachweislich erfolgreichem Reset fortgeschrieben,
        damit ein Fehler nicht die reguläre Mitternachts-Logik überspringt."""
        prev_day = self._last_reset_date or datetime.now(BERLIN_TZ).strftime("%Y-%m-%d")
        result = await self._daily_reset(prev_day)
        if result.get("summary_inserted"):
            self._last_reset_date = datetime.now(BERLIN_TZ).strftime("%Y-%m-%d")
            self._last_cleanup_hour = datetime.now(BERLIN_TZ).strftime("%Y%m%d%H")
            self._reset_retry_day = None
            self._reset_retry_count = 0
            await self._persist_housekeeping()
        return result

    # ---------------- deep analysis (Tiefen-Analyst) ----------------
    async def run_deep_analysis(self, manual: bool = False) -> Dict:
        """Sehr tiefe Analyse durch die 'deep_analyst'-Rolle. Erzeugt einen
        Report (kein direkter Trade), der die regulären Analysen speist."""
        try:
            symbols = [s for s in self.symbols
                       if len(self.scanner.candle_buffer.get(s, [])) >= 60]
            snaps = [self._snapshot(s) for s in symbols]
            snaps = [v for v in snaps if v]
            news_block = "(News deaktiviert)"
            if self.config.get("news_enabled"):
                news = await news_feed.get_headlines(25)
                news_block = "\n".join(f"- {n['title']} ({n['source']})" for n in news) or "(keine News)"
            macro = await self._macro_block()
            liq = await self._liquidity_block()
            perf = await self._strategy_performance_text()
            directives = await self._user_directives()
            open_trades = await self._open_trades_text()
            lessons = await self.learning.lessons_text() if self.learning else "(keine)"
            research_block = ""
            try:
                from services.ai_research import research_analyst
                research_block = await research_analyst.context_text()
            except Exception:
                pass
            ml_block = ""
            try:
                from services.ai_ml_lab import ml_lab
                ml_block = await ml_lab.context_text()
            except Exception:
                pass
            from services.ai_news_watcher import news_watcher
            nw_block = await news_watcher.context_text() or "(keine relevanten Ereignisse)"
            berlin = self.scanner.berlin_now().strftime("%d.%m.%Y %H:%M")
            prompt = (
                f"{master_prompt.prompt_block()}\n\n"
                f"{self._role_context_block()}\n\n"
                f"Zeit (Berlin): {berlin}\n\n"
                f"=== MARKTDATEN (Multi-Timeframe) ===\n" +
                "\n".join(v["text"] for v in snaps) +
                (f"\n\n{macro}" if macro else "") +
                (f"\n\n{liq}" if liq else "") +
                f"\n\n=== NEWS ===\n{news_block}\n\n"
                f"=== NEWS-WÄCHTER EREIGNISSE ===\n{nw_block}\n\n"
                f"=== PERFORMANCE ALLER STRATEGIEN DER PLATTFORM (lerne daraus) ===\n{perf}\n\n"
                f"=== GELERNTE LEKTIONEN ===\n{lessons}\n\n"
                + (f"{research_block}\n\n" if research_block else "")
                + (f"{ml_block}\n\n" if ml_block else "")
                + f"=== ANWEISUNGEN DES TRADERS ===\n{directives}\n\n"
                f"=== OFFENE POSITIONEN ===\n{open_trades}\n\n"
                "Erstelle jetzt die tiefe Marktanalyse als JSON."
            )
            text, provider, model = await self.generate_for_role(
                "deep_analyst", prompt, DEEP_ANALYSIS_SYSTEM, temperature=0.4)
            data = self._parse_json(text)
            now = _now_iso()
            doc = {
                "report": str(data.get("report", ""))[:4000],
                "outlook": [o for o in (data.get("outlook") or []) if isinstance(o, dict)][:15],
                "risks": [str(r)[:200] for r in (data.get("risks") or [])][:8],
                "recommendations": [str(r)[:250] for r in (data.get("recommendations") or [])][:8],
                "model": f"{provider}/{model}",
                "weight": ai_providers.model_weight(model),
                "weight_label": ai_providers.weight_label(model),
                "ts": now,
                "manual": manual,
            }
            await self.db.settings.update_one(
                {"_id": "ai_deep_report"}, {"$set": dict(doc)}, upsert=True)
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "deep_analysis",
                "text": doc["report"], "outlook": doc["outlook"], "risks": doc["risks"],
                "recommendations": doc["recommendations"], "model": doc["model"],
                "weight_label": doc["weight_label"], "manual": manual, "ts": now,
            })
            self.deep_last = now
            self.deep_last_error = None
            logger.info(f"Deep analysis done ({doc['model']}): "
                        f"{len(doc['outlook'])} Outlooks, {len(doc['recommendations'])} Empfehlungen")
            return {"status": "ok", "report": doc["report"], "model": doc["model"],
                    "ts": doc["ts"], "outlooks": len(doc["outlook"])}
        except Exception as e:
            self.deep_last_error = str(e)[:300]
            logger.error(f"Deep analysis failed: {e}")
            return {"status": "error", "detail": self.deep_last_error}

    async def _check_deep_schedule(self):
        """Feuert die Tiefenanalyse zu den konfigurierten Berlin-Uhrzeiten.
        Bereits vergangene Slots des Tages werden beim Boot übersprungen."""
        cfg = role_manager.role_cfg("deep_analyst")
        if not cfg.get("enabled", True):
            return
        times = cfg.get("schedule_times") or []
        if not times:
            return
        now_b = datetime.now(BERLIN_TZ)
        today = now_b.strftime("%Y-%m-%d")
        cur = now_b.strftime("%H:%M")
        for slot in times:
            if cur < slot:
                continue
            if slot not in self._deep_ran:
                self._deep_ran[slot] = today  # Boot: vergangenen Slot überspringen
                continue
            if self._deep_ran[slot] == today:
                continue
            self._deep_ran[slot] = today
            logger.info(f"Deep analysis Slot {slot} Berlin fällig – starte Tiefenanalyse")
            await self.run_deep_analysis(manual=False)

    # ---------------- background loop ----------------
    async def run_loop(self):
        self.running = True
        logger.info("AI Trader engine loop started (multi-provider: gemini/groq/openrouter/mistral)")
        while self.running:
            await asyncio.sleep(5)
            try:
                # Housekeeping läuft IMMER (auch wenn Engine aus ist / kein Key), damit
                # stündliches Analyse-Cleanup und der 00:00-Berlin-Reset zuverlässig feuern.
                try:
                    await self._run_housekeeping()
                except Exception as hk_err:
                    logger.error(f"AI housekeeping loop error: {hk_err}")

                # Lern-Modul: Ergebnisse synchronisieren + ggf. Lernlauf nach Trade-Close
                try:
                    if self.learning:
                        await self.learning.tick()
                except Exception as le:
                    logger.error(f"AI learning tick error: {le}")

                # Tiefen-Analyst: geplante Deep-Analysen (Berlin-Uhrzeiten)
                try:
                    await self._check_deep_schedule()
                except Exception as de:
                    logger.error(f"AI deep schedule error: {de}")

                # 20-Trade-Review nach dem Heatmap-Fix (max. alle 10 min prüfen)
                try:
                    if time.time() - self._review_last_check > 600:
                        self._review_last_check = time.time()
                        await self._check_heatmap_review()
                except Exception as hre:
                    logger.error(f"AI heatmap review error: {hre}")

                # KI-Ökosystem: Markt-Beobachter (Datensammlung), Forschungs-Analyst
                # (Backtest-/Optimizer-Auswertung) und ML-Labor (Optuna/XGBoost).
                # Alle drei laufen unabhängig von der Analyse-Engine weiter.
                for name, mod_attr in (("market observer", "ai_market_observer.market_observer"),
                                       ("research analyst", "ai_research.research_analyst"),
                                       ("ml lab", "ai_ml_lab.ml_lab"),
                                       ("ml gate", "ml_gate.ml_gate"),
                                       ("strategy lab", "ai_strategy_lab.strategy_lab"),
                                       ("trade manager", "ai_trade_manager.trade_manager")):
                    try:
                        mod_name, obj_name = mod_attr.split(".")
                        mod = __import__(f"services.{mod_name}", fromlist=[obj_name])
                        await getattr(mod, obj_name).tick()
                    except Exception as ex:
                        logger.error(f"AI {name} tick error: {ex}")
                try:
                    from services.ai_memory import memory
                    await memory.housekeeping()
                except Exception as me:
                    logger.error(f"AI memory housekeeping error: {me}")

                if not self.config.get("enabled") or not self.key:
                    self.next_run = None
                    continue
                now = time.time()
                if now >= self._next_due:
                    interval_min, window = self.current_interval()
                    interval = max(1, interval_min) * 60
                    self._next_due = now + interval
                    self.next_run = (datetime.now(timezone.utc)
                                     + timedelta(seconds=interval)).isoformat()
                    self.active_window = window
                    logger.info(f"AI Analyse-Zyklus ({window}: alle {interval_min} min)")
                    await self.run_analysis()
            except Exception as e:
                logger.error(f"AI loop error: {e}")

    # ---------------- chat ----------------
    async def chat_history(self, limit: int = 80) -> List[Dict]:
        """Liefert den Chatverlauf für das Frontend.

        Garantiert, dass die aktuelle gepinnte Tages-Summary IMMER als erstes
        Element enthalten ist – unabhängig vom Limit. Ohne diese Absicherung
        würde die Summary (älteste Nachricht des Tages) nach ~limit
        Neu-Nachrichten aus dem `sort("ts", -1).limit(limit)`-Fenster fallen
        und im Frontend nicht mehr angezeigt werden."""
        pinned = await self.db.ai_chat.find_one(
            {"role": "summary", "pinned": True}, sort=[("ts", -1)],
            projection={"_id": 0}
        )
        # Projection + Index (core/indexes.py: ai_chat_ts) halten die Abfrage auch
        # bei vielen tausend Nachrichten schnell.
        rows = await self.db.ai_chat.find({}, projection={"_id": 0}) \
            .sort("ts", -1).limit(limit).to_list(limit)
        rows.reverse()
        if pinned:
            pinned_id = pinned.get("id")
            # Dedupe: falls die gepinnte Summary bereits im Fenster ist, entferne
            # sie dort – sie wird stattdessen garantiert an den Anfang gesetzt.
            if pinned_id:
                rows = [r for r in rows if r.get("id") != pinned_id]
            rows = [pinned] + rows
        return rows

    async def chat_stream(self, text: str, coins=None):
        """SSE-Streaming der KI-Antwort. Wechselt bei 429 automatisch das Modell
        innerhalb desselben Providers. Unterstützt Gemini + OpenAI-kompatible
        Provider (Groq, OpenRouter, Mistral).

        `coins`: optionale Liste der Symbole, auf die der Chat-Kontext
        eingegrenzt wird (leer / None / "ALL" => alle Coins)."""
        chain = role_manager.chain("chat", self.config)
        if not any(ai_providers.provider_keys(p) for p, _ in chain):
            yield "⚠️ Kein API-Key für die konfigurierten Provider gesetzt – bitte in Render EnvVars setzen."
            return

        hist_rows = await self.db.ai_chat.find({"role": {"$in": ["user", "assistant", "summary"]}}) \
            .sort("ts", -1).limit(14).to_list(14)
        hist_rows.reverse()
        def _role_label(r):
            role = r.get("role")
            if role == "user":
                return "Nutzer"
            if role == "summary":
                return f"KI-Tageszusammenfassung ({r.get('day', '')})"
            return "KI"
        history = "\n".join(
            f"{_role_label(r)}: {r.get('text', '')}" for r in hist_rows
        ) or "(noch keine Nachrichten)"
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "user", "text": text, "ts": _now_iso(),
        })

        # Trader-Anweisungen REAL ausführen (Positionen schließen, Lektionen,
        # Einstellungen ...), bevor die Antwort generiert wird. Die echten
        # Ergebnisse fließen in den Kontext ein – die KI berichtet nur Fakten.
        exec_block = ""
        try:
            from services.ai_chat_commands import chat_commands
            cmd_res = await chat_commands.run(self, text)
            if cmd_res and cmd_res.get("results_text"):
                exec_block = ("\n\n=== SOEBEN REAL AUSGEFÜHRTE AKTIONEN "
                              "(vom System verifiziert) ===\n"
                              + cmd_res["results_text"])
        except Exception as e:
            logger.error(f"Chat-Kommandos fehlgeschlagen: {e}")

        context = await self._context_brief(coins=coins)
        system = CHAT_SYSTEM_TEMPLATE.format(context=context, history=history) + exec_block

        acc = ""
        async for kind, payload in ai_providers.stream_chain(chain, text, system, temperature=0.6):
            if kind == "token":
                acc += payload
                yield payload
            elif kind == "meta":
                provider, model = payload
                self._effective_provider, self._effective_model = provider, model
                if model != self.config.get("model"):
                    logger.info(f"AI chat: genutzt {provider}/{model}")
            elif kind == "error":
                err = f"\n⚠️ {payload}"
                acc += err
                yield err

        if acc:
            await self.db.ai_chat.insert_one({
                "id": str(uuid.uuid4()), "role": "assistant", "text": acc, "ts": _now_iso(),
            })

    async def clear_chat(self):
        await self.db.ai_chat.delete_many({})

    def current_interval(self) -> tuple:
        """Aktuelles Analyse-Intervall gemäß Zeitplan (Berlin-Zeit)."""
        now = self.scanner.berlin_now()
        minutes = now.hour * 60 + now.minute
        return ai_schedule.effective_interval(
            self.config.get("schedule"), self.config.get("interval_min", 10), minutes)

    async def _check_heatmap_review(self):
        """20-Trade-Review: Nach 20 geschlossenen KI-Trades seit dem Heatmap-Fix
        einmalig eine statistische Auswertung in den Feed schreiben (ohne
        LLM-Kosten), damit der Trader sieht, ob es ohne Heatmap besser läuft."""
        if self.db is None:
            return
        doc = await self.db.settings.find_one({"_id": "ai_heatmap_review"}) or {}
        if doc.get("done"):
            return
        if not doc.get("start_ts"):
            await self.db.settings.update_one(
                {"_id": "ai_heatmap_review"},
                {"$set": {"start_ts": _now_iso(), "done": False, "target_trades": 20}},
                upsert=True)
            return
        target = int(doc.get("target_trades", 20) or 20)
        trades = await self.db.auto_trades.find(
            {"strategy_id": "ai_trader", "status": "closed",
             "closed_at": {"$gte": doc["start_ts"]}}).to_list(500)
        if len(trades) < target:
            return
        pnls = [float(t.get("realized_pnl") or 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        wr = round(len(wins) / len(pnls) * 100) if pnls else 0
        stats = {
            "trades": len(pnls), "winrate_pct": wr,
            "pnl_total": round(sum(pnls), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "use_heatmap_data": bool(self.config.get("use_heatmap_data", False)),
            "use_liquidation_data": bool(self.config.get("use_liquidation_data", True)),
            "since": doc["start_ts"],
        }
        text = (f"📊 20-TRADE-REVIEW seit dem Heatmap-Fix "
                f"({timeutil.fmt_berlin(doc['start_ts'])}): {stats['trades']} Trades · "
                f"Winrate {wr}% · PnL {stats['pnl_total']:+.2f} USDT · "
                f"Ø Gewinn {stats['avg_win']:+.2f} / Ø Verlust {stats['avg_loss']:+.2f} USDT. "
                f"Einstellungen: Heatmap-Daten "
                f"{'AN' if stats['use_heatmap_data'] else 'AUS'}, echte Liquidations-Daten "
                f"{'AN' if stats['use_liquidation_data'] else 'AUS'}. "
                + ("Winrate im Ziel-Sweetspot (30-50%+) – Setup beibehalten."
                   if wr >= 30 else
                   "Winrate unter 30% – Setup prüfen (Lektionen/Filter nachschärfen)."))
        await self.db.ai_chat.insert_one({
            "id": str(uuid.uuid4()), "role": "learning",
            "trigger": "heatmap_review", "text": text,
            "stats": stats, "ts": _now_iso()})
        await self.db.settings.update_one(
            {"_id": "ai_heatmap_review"},
            {"$set": {"done": True, "finished_ts": _now_iso(), "stats": stats}})
        logger.info(f"AI 20-Trade-Review veröffentlicht: {stats}")

    def status(self) -> Dict:
        from services.ai_news_watcher import news_watcher
        return {
            "config": dict(self.config),
            "has_key": bool(self.key),
            "provider_keys": self._available_providers(),
            "backup_keys": ai_providers.backup_keys_info(),
            "analyzing": self._analyzing,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "decisions": self.decisions,
            "allowed_models": ALLOWED_MODELS,
            "model_weights": ai_providers.MODEL_WEIGHTS,
            "effective_model": self._effective_model,
            "effective_provider": self._effective_provider,
            "learning": self.learning.summary() if self.learning else None,
            "roles": role_manager.snapshot(),
            "deep_last": self.deep_last,
            "deep_last_error": self.deep_last_error,
            "news_watcher": news_watcher.status(),
            "schedule_active": {
                "interval_min": self.current_interval()[0],
                "window": self.current_interval()[1],
                "text": ai_schedule.schedule_text(self.config.get("schedule"),
                                                 self.config.get("interval_min", 10)),
            },
            "providers_health": ai_providers.health_status(),
            "day_risk": self._day_risk_cache,
            "master_prompt": master_prompt.snapshot(),
            "validation": validation_gate.status(),
            "strategy_lab": strategy_lab.status(),
        }


ai_engine = AIEngine()
