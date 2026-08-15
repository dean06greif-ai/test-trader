"""Regime-Engine v2 – deterministische, mathematisch begründete Marktphasen.

Warum neu (Probleme der alten K-Means-Erkennung):
- Cluster-Labels waren *relativ* zum Datensatz: in einem überwiegend fallenden
  Zeitraum wurde ein stark fallender Cluster als "leicht abwärts" beschriftet.
- Ein einziges Lookback-Fenster (3 Tage) kann weder "500 Tage langsam fallend"
  noch "3 Tage Ausbruch" erkennen.
- Häufig blieben nur 2 Cluster übrig, die nicht zum Chart passten.

Ansatz v2 (fest definierte Taxonomie statt Clustering):
1. TREND: rollierende lineare Regression über den Log-Kurs auf MEHREREN
   Horizonten (z.B. 5/10/20/50/100 Tage). Bewertet wird der t-Wert der
   Steigung (Steigung / Standardfehler) – skalenfrei, dadurch ist "langsam
   aber stetig fallend" ebenso klar erkennbar wie "schnell fallend".
   Zusätzlich: Übereinstimmung der Horizonte (Multi-Timeframe-Konsens),
   ADX/DI als Trendstärke-/Richtungsbestätigung, Kaufman-Effizienz.
2. VOLATILITÄT: ATR% (oder realisierte Vola) im Vergleich zum eigenen,
   rückblickenden Referenzfenster (z-Wert) -> niedrig / mittel / hoch.
3. TAXONOMIE: 3 Trendzustände x 3 Vola-Zustände = 9 Regime mit festen IDs und
   festen Labels (id = trend_idx * 3 + vol_idx). Zusätzlich wird jedes Regime
   auf die 3 NNFX-Regime gemappt (trend / range / breakout).
4. WECHSEL-LOGIK: Hysterese (Ein-/Ausstiegsschwellen unterschiedlich),
   Bestätigungsdauer, Mindesthaltedauer und Confidence-Score verhindern
   Flattern und späte/falsche Wechsel.

Kein Lookahead: alle Features sind rückblickend (services.regime_features),
der Zustandsautomat läuft strikt vorwärts. `ideal_labels()` ist die EINZIGE
Funktion mit Zukunftssicht – sie dient ausschließlich der visuellen Prüfung
und Validierung und wird nie in Backtests/Live verwendet.
"""
import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from services import regime_features as rf

logger = logging.getLogger(__name__)

ENGINE = "v2"

# Anteil der Bars, deren Label vom Kursverlauf abweichen darf, bevor eine
# Analyse als "nicht plausibel" gilt. Ohne Zukunftssicht ist eine
# Umschaltverzögerung (Bestätigungs- + Mindesthaltedauer) unvermeidbar –
# genau diese Latenz landet hier. 8% entspricht rund einer Haltedauer pro
# Zyklus und ist damit die physikalische Untergrenze, nicht Willkür.
VALIDATE_PASS_PCT = 8.0

TREND_STATES = [("down", "Abwärtstrend"), ("side", "Seitwärtsmarkt"), ("up", "Aufwärtstrend")]
VOL_STATES = [("low", "niedrige Volatilität"), ("mid", "mittlere Volatilität"),
              ("high", "hohe Volatilität")]
# Zweite Achse im 5er-Modus: Trendstärke statt Volatilität
STRENGTH_STATES = [("weak", "leicht"), ("strong", "stark")]

# Granularität der Taxonomie:
#   3 = Aufwärts / Seitwärts / Abwärts
#   5 = stark abwärts / leicht abwärts / seitwärts / leicht aufwärts / stark aufwärts
#   9 = 3 Trendzustände x 3 Volatilitätsstufen (ursprüngliche Taxonomie)
REGIME_MODES = (3, 5, 9)
DEFAULT_REGIME_MODE = 5

# --- Adaptive Glättung: Fenster als ANTEIL des analysierten Zeitraums ---
# Begründung: ein Regime ist nur dann eines, wenn es einen relevanten Teil des
# betrachteten Zeitraums ausmacht. Bei 360 Tagen ist "3 Tage abwärts" Rauschen,
# bei 30 Tagen dagegen ein echter Abschnitt. Deshalb skalieren Horizonte,
# Bestätigungs-/Haltedauer und Glättung mit dem Zeitraum – nicht absolut.
# Es gibt drei Profile; "auto" probiert alle und nimmt das mit der besten
# Bewertung (Plausibilitätsprüfung + Rückblick-Übereinstimmung + Abschnittslänge).
ADAPT_PROFILES: Dict[str, Dict] = {
    "fein": {"horizons": [0.008, 0.020, 0.050, 0.110, 0.240],
             "hold": 0.012, "confirm": 0.005, "smooth": 0.0030, "vol_ref": 0.20,
             "vol_window": 0.010, "rev": 2.2, "stall_frac": 0.004,
             "label": "fein (viele, kurze Abschnitte)"},
    "standard": {"horizons": [0.015, 0.035, 0.080, 0.170, 0.330],
                 "hold": 0.024, "confirm": 0.008, "smooth": 0.0055, "vol_ref": 0.28,
                 "vol_window": 0.016, "rev": 3.0, "stall_frac": 0.008,
                 "label": "standard"},
    "grob": {"horizons": [0.028, 0.065, 0.140, 0.270, 0.460],
             "hold": 0.045, "confirm": 0.015, "smooth": 0.0100, "vol_ref": 0.38,
             "vol_window": 0.026, "rev": 4.5, "stall_frac": 0.016,
             "label": "grob (wenige, lange Abschnitte)"},
}
ADAPT_ORDER = ("fein", "standard", "grob")

# Alle Einstellungen sind bewusst in TAGEN angegeben (timeframe-unabhängig)
DEFAULT_CONFIG: Dict = {
    # --- Granularität & adaptive Glättung ---
    "regime_mode": DEFAULT_REGIME_MODE,
    "auto_adapt": True,               # Fenster automatisch an den Zeitraum anpassen
    "adapt_profile": "auto",          # auto | fein | standard | grob | off
    # --- Reaktive Erkennung über Umkehrpunkte (Standard) ---
    # Prinzip: vom Tiefpunkt aus beginnt (nach Bestätigung) die steigende
    # Phase, vom Hochpunkt aus die fallende; Seitwärts, wenn der Kurs nicht
    # mehr wirklich vorankommt. Keine Prediction – nur reaktive Bestätigung.
    "detector": "reactive",           # reactive | regression (alte v2-Logik)
    "rev_atr_mult": 3.0,              # Umkehr-Schwelle = x * ATR%
    "rev_min_pct": 0.0,               # Untergrenze der Schwelle (0 = auto 0.05%)
    "rev_max_pct": 30.0,              # Obergrenze der Schwelle
    "persist_candles": 3,             # 2-3 Kerzen Persistenz vor Regime-Wechsel
    "side_leg_atr_mult": 1.5,         # Bewegungen < x * Schwelle = Seitwärts
    "side_stall_days": 0.0,           # kein neues Extrem so lange -> Seitwärts (0 = auto)
    "min_phase_days": 0.0,            # Mini-Phasen-Filter (0 = auto, ~1% des Zeitraums)
    "mtf_confirm": True,              # grobere Sicht (größere Schwelle) bestätigt Richtung
    "mtf_mult": 2.5,                  # Faktor der groben Umkehr-Schwelle
    "use_volume_confirm": True,       # auffälliges Volumen beschleunigt Bestätigung
    "volume_boost": 1.5,              # Volumen > x * Durchschnitt = auffällig
    # --- EMA-Trendlinien (Regime-Bestätigung + Chart-Anzeige) ---
    # Empirisch getestet (scripts/ema_testbed.py, BTC+ETH 720d/1h und 2000d/1d):
    # Steigungs-geprüfte EMAs mit festen TAGES-Perioden verbessern die kausale
    # Live-Sicht (Holdout-Trefferquote +2pp, Trend-Treffer +5pp, Lag auf langen
    # Zeiträumen -8 Tage). Reine Stack-Ordnung (9>50>200) ohne Steigungs-Check
    # verschlechtert dagegen – EMAs laufen am Trend-ENDE nach.
    "use_ema_confirm": True,          # EMA-Signal überstimmt Live-Seitwärts
    "ema_fast_days": 9.0,             # schnelle EMA – Frische-Check (Steigungs-Vorzeichen)
    "ema_mid_days": 21.0,             # mittlere EMA – ihre Steigung ist das Signal
    "ema_slow_days": 50.0,            # langsame EMA – Richtungs-Anker (Seite zählt)
    "ema_slope_thr": 0.22,            # Steigung >= x * Tagesvola => Trend-Beweis
    "ema_persist_days": 1.5,          # Signal muss so lange stabil sein (Anti-Flicker)
    "chart_ema_days": [9, 21, 50, 200],  # im Chart einblendbare EMA-Linien (Tage)
    # --- Detektor "ema": Regime = Steigung EINER EMA (einfach & intuitiv) ---
    # Empirisch geprüft (scripts/ema_detector_proto.py): Final nutzt die
    # ZENTRIERTE (lag-freie) Steigung -> Segmente laufen fast nie netto gegen
    # ihr Label; Live bleibt streng kausal.
    "ema_regime_days": 14.0,          # EMA-Periode in Tagen (9/5 = feiner, mehr Wechsel)
    "ema_regime_thr": 0.18,           # Steigung >= x * Tagesvola => Trend
    "ema_regime_smooth_days": 1.0,    # Fenster für die Steigungs-Messung
    "ema_regime_persist_days": 1.0,   # Live-Wechsel erst nach x Tagen stabil
    # --- Detektor "kombi": EMA-Steigung + Umkehrpunkte als Beschleuniger ---
    # (Etappe 1) Basis = EMA-Steigung; bestätigte Umkehrpunkte schalten einen
    # Wechsel, den die EMA bereits sieht, sofort (nur Beschleunigung).
    # Trend-Dominanz: Seitwärts-Einschübe bis x Tage bleiben Teil des Trends.
    "kombi_ema_days": 14.0,           # EMA-Periode (Tage)
    "kombi_thr": 0.18,                # Trend-Schwelle: Steigung >= x * Tagesvola
    "kombi_slope_days": 5.0,          # Steigungs-Fenster (Tage)
    "kombi_persist_days": 1.0,        # Live-Wechsel erst nach x Tagen stabil
    "kombi_dominance_days": 3.0,      # Trend-Dominanz: max. Seitwärts-Tage im Trend
    "kombi_pivot_accel": True,        # Umkehrpunkte beschleunigen Übergänge
    # --- Trend (Multi-Timeframe-Regression) ---
    "horizons_days": [5, 10, 20, 50, 100],
    "horizon_weights": None,          # None = längere Horizonte leicht höher gewichtet
    "trend_t": 2.0,                   # |Score| ab dem ein Trend gilt
    "trend_strong_t": 4.5,            # ab hier "starker" Trend (Anzeige/Stats)
    "strong_speed_ratio": 0.35,       # 5er-Modus: "stark" ab Netto-%/Tag >= x·Tagesvola
    "t_clip": 12.0,                   # t-Werte kappen (Ausreißer)
    "agreement_weight": 0.35,         # Gewicht des Multi-Horizont-Konsens im Score
    "use_di": True,                   # DI+/DI- als Richtungsbestätigung
    "di_weight": 0.15,
    "require_adx": True,              # Trend nur mit ADX-Bestätigung ...
    "adx_min": 18.0,                  # ... oder wenn |Score| >= trend_strong_t
    "adx_period_days": 2.0,
    "require_efficiency": False,
    "efficiency_min": 0.12,
    "efficiency_days": 10.0,
    # Range-Filter: ein Trend muss auch am Rand der Handelsspanne stehen
    # (Donchian-Position). Verhindert, dass Schwingungen innerhalb einer Range
    # als Trend gelten – der klassische "Seitwärts wird Trend"-Fehler.
    "require_range_break": True,
    "range_break_pos": 0.72,
    "range_window_days": 0.0,         # 0 = längster Trend-Horizont
    "range_lag_days": 0.0,            # 0 = kürzester Trend-Horizont
    "long_confirm_frac": 0.75,        # Alternativ: langer Horizont bestätigt den Trend
    "gate_timeout_days": 12.0,        # Hält der Score so lange, wird der Range-Filter
                                      # übergangen (verhindert verpasste Trends)
    "strong_bypass_range": True,      # sehr starker Score übergeht den Range-Filter
    # Varianz-Verhältnis (Lo/MacKinlay): erkennt mean-reverting Märkte.
    # VR < vr_min => Range: Trend-Einstieg wird unterdrückt.
    "use_variance_ratio": False,      # experimentell: hilft bei choppigen Märkten
    "vr_k_days": 10.0,
    "vr_window_days": 0.0,            # 0 = längster Trend-Horizont
    "vr_min": 0.85,
    "smooth_days": 0.5,               # Glättung des Trend-Scores
    # --- Volatilität ---
    "vol_metric": "atr",              # atr | stdev
    "vol_window_days": 3.0,
    "vol_smooth_days": 5.0,           # Glättung des Vola-Maßes (gegen Flattern)
    "vol_ref_days": 90.0,             # Referenzfenster für den z-Wert
    "vol_low_z": -0.55,
    "vol_high_z": 0.65,
    "vol_hysteresis": 0.45,
    # --- Wechsel-Logik ---
    "hysteresis": 0.3,                # Ein-/Ausstieg: thr*(1+h) / thr*(1-h)
    "confirm_days": 0.5,              # Kandidat muss so lange stabil sein
    "min_hold_days": 2.0,             # Mindesthaltedauer des aktuellen Regimes
    "confidence_min": 0.55,           # Mindest-Confidence für einen Wechsel
    # --- Validierung ---
    "validate_side_max_pct_per_day": 0.35,
    "validate_side_t": 3.0,
    "validate_min_segment_days": 5.0,
    "validate_tol_pct": 0.5,
    "validate_tol_t": 0.5,
    "validate_vol_tol_mult": 1.5,     # Toleranz = x * Tagesvola * sqrt(Tage)
}

CONFIG_META = [
    ("regime_mode", "Anzahl Regime", "3 = Auf/Seit/Ab · 5 = zusätzlich stark/leicht · "
     "9 = Trend x Volatilität. Weniger Regime = klarere, stabilere Abschnitte."),
    ("detector", "Erkennungs-Prinzip", "reactive (Standard) = Umkehrpunkte: vom "
     "Tiefpunkt aus beginnt die steigende Phase, vom Hochpunkt aus die fallende, "
     "Seitwärts wenn der Kurs nicht mehr vorankommt – reaktive Bestätigung statt "
     "Prediction. ema = Regime folgt der Steigung EINER EMA (z.B. 9 Tage): "
     "einfach, glatt, intuitiv – Final nutzt die zentrierte (lag-freie) "
     "Steigung, Live die kausale. kombi = EMA-Steigung als Basis PLUS "
     "Umkehrpunkte als Beschleuniger und Trend-Dominanz (Mini-Seitwärtsphasen "
     "bleiben Teil des Trends). regression = alte statistische Trend-Erkennung."),
    ("ema_regime_days", "EMA-Regime: Periode (Tage)", "Nur Detektor 'ema': "
     "diese EMA bestimmt das Regime. 14 = Standard (ruhig, beste Holdout-"
     "Trefferquote im Test), 9 oder 5 = feiner fürs Daytrading (mehr Wechsel, "
     "weniger Lag)."),
    ("ema_regime_thr", "EMA-Regime: Trend-Schwelle (x Vola)", "Nur Detektor "
     "'ema': Steigung pro Tag >= x * Tagesvola => Auf-/Abwärtstrend; fällt sie "
     "unter die Hälfte davon zurück => Seitwärts (Hysterese)."),
    ("ema_regime_smooth_days", "EMA-Regime: Steigungs-Fenster (Tage)", "Nur "
     "Detektor 'ema': über so viele Tage wird die Steigung gemessen. Kleiner = "
     "reaktiver, größer = glatter."),
    ("ema_regime_persist_days", "EMA-Regime: Live-Stabilität (Tage)", "Nur "
     "Detektor 'ema': die Live-Sicht übernimmt einen Wechsel erst, wenn er so "
     "viele Tage stabil anliegt (Anti-Flicker / Bestätigungszeit, bis 30 Tage). "
     "Höher = ruhigere Live-Prediction, aber späteres Umschalten."),
    ("kombi_ema_days", "Kombi: EMA-Periode (Tage)", "Nur Detektor 'kombi': "
     "diese EMA bestimmt das Regime (Steigung relativ zur Tagesvola). "
     "14 = Standard für das 5-15-Tage-Phasenziel, 9 = feiner/reaktiver."),
    ("kombi_thr", "Kombi: Trend-Schwelle (x Vola)", "Nur Detektor 'kombi': "
     "EMA-Steigung pro Tag >= x * Tagesvola => Auf-/Abwärtstrend; zurück zu "
     "Seitwärts erst unter der Hälfte (Hysterese). Standard 0.18."),
    ("kombi_slope_days", "Kombi: Steigungs-Fenster (Tage)", "Nur Detektor "
     "'kombi': über so viele Tage wird die EMA-Steigung gemessen. Kleiner = "
     "reaktiver, größer = glatter. Standard 5."),
    ("kombi_persist_days", "Kombi: Live-Stabilität (Tage)", "Nur Detektor "
     "'kombi': die Live-Sicht übernimmt einen Wechsel erst, wenn er so viele "
     "Tage stabil anliegt – außer ein bestätigter Umkehrpunkt beschleunigt "
     "ihn (dann sofort)."),
    ("kombi_dominance_days", "Kombi: Trend-Dominanz (Tage)", "Nur Detektor "
     "'kombi': Seitwärts-Einschübe bis zu dieser Länge INNERHALB eines Trends "
     "bleiben Teil des Trends (live überbrückt, final absorbiert). Filtert "
     "Mini-Seitwärtsphasen im übergeordneten Trend heraus. Standard 3."),
    ("kombi_pivot_accel", "Kombi: Umkehrpunkte beschleunigen", "Nur Detektor "
     "'kombi': ein bestätigter Hoch-/Tiefpunkt (ATR-ZigZag) schaltet einen "
     "Wechsel, den die EMA-Steigung bereits anzeigt, SOFORT – er erzeugt nie "
     "eigenständig ein Regime (nur Beschleunigung, kein neues Signal)."),
    ("rev_atr_mult", "Umkehr-Schwelle (x ATR)", "Ein Hoch-/Tiefpunkt gilt als "
     "Umkehrpunkt, wenn der Kurs um dieses ATR-Vielfache dagegen läuft. "
     "Kleiner = mehr, kürzere Phasen."),
    ("persist_candles", "Persistenz (Kerzen)", "So viele Kerzen muss die "
     "Gegenbewegung anhalten, bevor der Wechsel bestätigt wird (2-3 empfohlen, "
     "filtert Fehlsignale)."),
    ("side_leg_atr_mult", "Seitwärts-Schwelle (x Umkehr-Schwelle)", "Bewegungen "
     "kleiner als dieses Vielfache der Umkehr-Schwelle gelten als Seitwärts "
     "(kein echter Trend)."),
    ("side_stall_days", "Stillstand bis Seitwärts (Tage)", "Macht der Kurs so "
     "lange kein neues Hoch/Tief mehr, beginnt die Seitwärtsphase (0 = automatisch)."),
    ("min_phase_days", "Mini-Phasen-Filter (Tage)", "Kürzere Phasen werden dem "
     "längeren Nachbarn zugeschlagen – weniger Mini-Regime, handelbarere "
     "Abschnitte. 0 = automatisch (~1% des Zeitraums)."),
    ("mtf_confirm", "Multi-Timeframe-Konsens", "Eine grobere Sicht (größere "
     "Umkehr-Schwelle) bestätigt die Richtung – widerspricht sie, wird der "
     "Trend-Score gedämpft (stabilere Phasen)."),
    ("mtf_mult", "Konsens-Schwelle (x fein)", "Faktor der groben Umkehr-Schwelle "
     "gegenüber der feinen (Standard 2.5)."),
    ("use_volume_confirm", "Volumen-Bestätigung", "Gegenbewegungen mit auffälligem "
     "Volumen bestätigen Umkehrpunkte schneller (zählen doppelt)."),
    ("volume_boost", "Volumen-Schwelle (x Ø)", "Ab diesem Vielfachen des "
     "durchschnittlichen Volumens gilt eine Kerze als auffällig."),
    ("use_ema_confirm", "EMA-Bestätigung (Live-Sicht)", "Steigungs-geprüfte "
     "EMA-Trendlinien überstimmen Seitwärts-Phasen der Live-Erkennung: mittlere "
     "EMA über/unter der langsamen, Kurs auf der richtigen Seite, Steigung der "
     "mittleren EMA klar über der Tagesvola und schnelle EMA in dieselbe "
     "Richtung. Empirisch getestet – trifft langsame Trends deutlich früher."),
    ("ema_fast_days", "EMA schnell (Tage)", "Kurzfristige Trendlinie (Standard 9). "
     "Ihre Steigungs-Richtung muss zum Signal passen (Frische-Check gegen "
     "nachlaufende EMAs am Trend-Ende)."),
    ("ema_mid_days", "EMA mittel (Tage)", "Mittlere Trendlinie (Standard 21). Ihre "
     "Steigung pro Tag – relativ zur Tagesvolatilität – ist das eigentliche "
     "Trend-Signal."),
    ("ema_slow_days", "EMA langsam (Tage)", "Langsame Trendlinie (Standard 50) als "
     "Richtungs-Anker: Signal nur, wenn die mittlere EMA auf der passenden Seite "
     "liegt (klassisches Kreuzungs-Prinzip, z.B. Golden/Death Cross)."),
    ("ema_slope_thr", "EMA-Steigungs-Schwelle (x Vola)", "Die mittlere EMA muss "
     "mindestens so stark pro Tag steigen/fallen (in Vielfachen der Tagesvola), "
     "damit das EMA-Signal zählt. Höher = konservativer, weniger Fehlsignale."),
    ("ema_persist_days", "EMA-Persistenz (Tage)", "Das EMA-Signal muss so viele "
     "Tage stabil anliegen, bevor es die Live-Sicht überstimmt (Anti-Flicker)."),
    ("chart_ema_days", "Chart-EMAs (Tage)", "Diese EMA-Linien werden mit der "
     "Analyse gespeichert und lassen sich im Chart einblenden (Standard: aus) – "
     "z.B. um Kreuzungen mit Regime-Wechseln zu vergleichen."),
    ("auto_adapt", "Fenster automatisch anpassen", "Horizonte, Bestätigungs- und "
     "Mindesthaltedauer skalieren mit dem analysierten Zeitraum (360 Tage brauchen "
     "andere Fenster als 2000 Tage). Manuell gesetzte Felder bleiben unangetastet."),
    ("adapt_profile", "Glättungs-Profil", "auto = drei Profile werden geprüft und das "
     "bestbewertete genommen · fein/standard/grob = feste Wahl · off = keine Anpassung."),
    ("horizons_days", "Trend-Horizonte (Tage)", "Über diese Zeiträume wird die "
     "Regressions-Steigung gemessen (Multi-Timeframe-Konsens)."),
    ("trend_t", "Trend-Schwelle (t-Wert)", "Ab welcher statistischer Signifikanz "
     "ein Trend als Trend gilt (2 ≈ 95%)."),
    ("trend_strong_t", "Schwelle 'starker Trend'", "Ab hier gilt der Trend als stark; "
     "ADX-Bestätigung ist dann nicht mehr nötig."),
    ("agreement_weight", "Gewicht Horizont-Konsens", "Wie stark uneinige Horizonte "
     "den Score abschwächen."),
    ("require_adx", "ADX-Bestätigung", "Trend nur bei ADX >= Minimum."),
    ("adx_min", "ADX-Minimum", "Trendstärke-Filter (klassisch 20-25)."),
    ("adx_period_days", "ADX-Periode (Tage)", "Länge der ADX-Glättung."),
    ("require_efficiency", "Effizienz-Filter", "Trend nur bei glattem Verlauf "
     "(Kaufman-Effizienz)."),
    ("efficiency_min", "Effizienz-Minimum", "0 = Chop, 1 = perfekt glatter Trend."),
    ("vol_metric", "Vola-Maß", "atr = ATR%, stdev = realisierte Vola."),
    ("vol_window_days", "Vola-Fenster (Tage)", "Messfenster der Volatilität."),
    ("vol_smooth_days", "Vola-Glättung (Tage)", "Glättet das Vola-Maß – weniger "
     "Sprünge zwischen den Vola-Stufen."),
    ("vol_ref_days", "Vola-Referenz (Tage)", "Vergleichsfenster für hoch/niedrig "
     "(rückblickend, kein Lookahead)."),
    ("vol_low_z", "Grenze niedrige Vola (z)", "z-Wert-Grenze nach unten."),
    ("vol_high_z", "Grenze hohe Vola (z)", "z-Wert-Grenze nach oben."),
    ("hysteresis", "Trend-Hysterese", "Einstiegsschwelle höher als Ausstiegsschwelle "
     "-> kein Flattern."),
    ("vol_hysteresis", "Vola-Hysterese", "Wie oben, für die Vola-Grenzen."),
    ("confirm_days", "Bestätigungsdauer (Tage)", "So lange muss ein neues Regime "
     "stabil sein, bevor umgeschaltet wird."),
    ("min_hold_days", "Mindesthaltedauer (Tage)", "Mindestdauer des aktuellen Regimes."),
    ("confidence_min", "Mindest-Confidence", "Wechsel nur ab dieser Sicherheit."),
    ("smooth_days", "Score-Glättung (Tage)", "Glättung gegen Kerzen-Rauschen."),
    ("require_range_break", "Range-Filter (Ausbruch nötig)", "Trend-Einstieg nur bei "
     "neuem Hoch/Tief oder wenn der lange Horizont bestätigt – verhindert, dass "
     "Schwingungen in einer Range als Trend gelten."),
    ("range_window_days", "Range-Fenster (Tage)", "Spanne für den Ausbruch-Test "
     "(0 = zweitlängster Horizont)."),
    ("range_lag_days", "Range-Versatz (Tage)", "Wie weit die Vergleichsspanne in der "
     "Vergangenheit endet (0 = kürzester Horizont)."),
    ("long_confirm_frac", "Bestätigung langer Horizont", "Anteil der Trend-Schwelle, "
     "den der längste Horizont erreichen muss."),
    ("gate_timeout_days", "Range-Filter Timeout (Tage)", "Hält der Score so lange, "
     "wird der Range-Filter übergangen (kein verpasster Trend)."),
    ("strong_bypass_range", "Starker Trend übergeht Filter", "Sehr starke Scores "
     "dürfen den Range-Filter überspringen (schnelle Crashs/Rallys)."),
    ("use_variance_ratio", "Varianz-Verhältnis nutzen", "Experimentell: unterdrückt "
     "Trends in statistisch mean-reverting Phasen."),
    ("vr_k_days", "VR Intervall (Tage)", "k-Bar-Renditen für das Varianz-Verhältnis."),
    ("vr_min", "VR Minimum", "Unter diesem Wert gilt der Markt als mean-reverting."),
    ("validate_min_segment_days", "Prüfung: min. Abschnitt (Tage)", "Kürzere "
     "Abschnitte werden bei der Plausibilitätsprüfung ignoriert."),
    ("validate_side_max_pct_per_day", "Prüfung: max. Drift seitwärts (%/Tag)",
     "Ab dieser Drift gilt ein Seitwärts-Label als verdächtig."),
    ("validate_vol_tol_mult", "Prüfung: Toleranz (x Vola)", "Wie stark ein Abschnitt "
     "gegen sein Label 'atmen' darf."),
]

# Gruppierung der Einstellungen nach Erkennungs-Prinzip (für die Oberfläche).
# detectors=None -> gilt für alle Detektoren; sonst Liste der Detektoren,
# in denen die Einstellung tatsächlich wirkt.
CONFIG_GROUPS: Dict[str, Dict] = {}


def _grp(group: str, detectors, *keys):
    for k in keys:
        CONFIG_GROUPS[k] = {"group": group, "detectors": detectors}


_grp("Allgemein", None,
     "regime_mode", "detector", "auto_adapt", "adapt_profile", "chart_ema_days")
_grp("Umkehrpunkte (Detektor 'reactive')", ["reactive"],
     "rev_atr_mult", "persist_candles", "side_leg_atr_mult", "side_stall_days",
     "mtf_confirm", "mtf_mult", "use_volume_confirm", "volume_boost")
_grp("EMA-Regime (Detektor 'ema')", ["ema"],
     "ema_regime_days", "ema_regime_thr", "ema_regime_smooth_days",
     "ema_regime_persist_days")
_grp("Kombi-Detektor (EMA + Umkehrpunkte)", ["kombi"],
     "kombi_ema_days", "kombi_thr", "kombi_slope_days", "kombi_persist_days",
     "kombi_dominance_days", "kombi_pivot_accel")
_grp("Phasen-Glättung", ["reactive", "ema", "kombi"], "min_phase_days")
_grp("EMA-Bestätigung (Live-Sicht)", ["reactive", "regression"],
     "use_ema_confirm", "ema_fast_days", "ema_mid_days", "ema_slow_days",
     "ema_slope_thr", "ema_persist_days")
_grp("Trend-Regression (Detektor 'regression')", ["regression"],
     "horizons_days", "trend_t", "trend_strong_t", "agreement_weight",
     "require_adx", "adx_min", "adx_period_days", "require_efficiency",
     "efficiency_min", "require_range_break", "range_window_days",
     "range_lag_days", "long_confirm_frac", "gate_timeout_days",
     "strong_bypass_range", "use_variance_ratio", "vr_k_days", "vr_min",
     "smooth_days")
_grp("Volatilität (5er/9er-Achse)", None,
     "vol_metric", "vol_window_days", "vol_smooth_days", "vol_ref_days",
     "vol_low_z", "vol_high_z", "vol_hysteresis")
_grp("Wechsel-Logik", None, "min_hold_days")
_grp("Wechsel-Logik (nur Detektor 'regression')", ["regression"],
     "hysteresis", "confirm_days", "confidence_min")
_grp("Prüfung (Validierung)", None,
     "validate_min_segment_days", "validate_side_max_pct_per_day",
     "validate_vol_tol_mult")


# ---------------------------------------------------------------- Taxonomie
def norm_mode(mode) -> int:
    try:
        m = int(mode)
    except (TypeError, ValueError):
        return 9
    return m if m in REGIME_MODES else 9


def sub_states(mode: int) -> List[Tuple[str, str]]:
    """Zweite Achse der Taxonomie je Modus."""
    mode = norm_mode(mode)
    if mode == 9:
        return list(VOL_STATES)
    if mode == 5:
        return list(STRENGTH_STATES)
    return [("", "")]


def regime_id(trend_idx: int, sub_idx: int = 1, mode: int = 9) -> int:
    t, s = int(trend_idx), int(sub_idx)
    mode = norm_mode(mode)
    if mode == 9:
        return t * 3 + s
    if mode == 3:
        return t
    # 5er-Modus: 0 stark ab · 1 leicht ab · 2 seitwärts · 3 leicht auf · 4 stark auf
    if t == 1:
        return 2
    return 2 + (t - 1) * (1 + s)


def split_id(rid: int, mode: int = 9) -> Tuple[int, int]:
    rid = int(rid)
    mode = norm_mode(mode)
    if mode == 9:
        return rid // 3, rid % 3
    if mode == 3:
        return min(max(rid, 0), 2), 0
    return {0: (0, 1), 1: (0, 0), 2: (1, 0), 3: (2, 0), 4: (2, 1)}.get(rid, (1, 0))


def regime_label(rid: int, mode: int = 9) -> str:
    mode = norm_mode(mode)
    t, s = split_id(rid, mode)
    if mode == 9:
        return f"{TREND_STATES[t][1]} · {VOL_STATES[s][1]}"
    if mode == 3:
        return TREND_STATES[t][1]
    if t == 1:
        return TREND_STATES[1][1]
    return f"{'Starker' if s == 1 else 'Leichter'} {TREND_STATES[t][1]}"


def regime_key(rid: int, mode: int = 9) -> str:
    mode = norm_mode(mode)
    t, s = split_id(rid, mode)
    tk = TREND_STATES[t][0]
    if mode == 3 or t == 1:
        return tk
    return f"{tk}_{sub_states(mode)[s][0]}"


def nnfx_regime(rid: int, mode: int = 9) -> str:
    """Abbildung der Regime auf die 3 NNFX-Regime."""
    mode = norm_mode(mode)
    t, s = split_id(rid, mode)
    if t != 1:
        return "trend"
    return "breakout" if (mode == 9 and s == 2) else "range"


NNFX_LABELS = {"trend": "Trend (NNFX)", "range": "Seitwärts (NNFX)",
               "breakout": "Volatilität/Breakout (NNFX)"}


def taxonomy(mode: int = 9) -> List[Dict]:
    mode = norm_mode(mode)
    subs = sub_states(mode)
    seen, out = set(), []
    for t in range(len(TREND_STATES)):
        for s in range(len(subs)):
            rid = regime_id(t, s, mode)
            if rid in seen:
                continue
            seen.add(rid)
            ti, si = split_id(rid, mode)
            entry = {"id": rid, "key": regime_key(rid, mode),
                     "label": regime_label(rid, mode),
                     "trend": TREND_STATES[ti][0],
                     "nnfx": nnfx_regime(rid, mode),
                     "nnfx_label": NNFX_LABELS[nnfx_regime(rid, mode)]}
            if mode == 9:
                entry["vol"] = VOL_STATES[si][0]
            elif mode == 5:
                entry["strength"] = "-" if ti == 1 else STRENGTH_STATES[si][0]
            out.append(entry)
    return sorted(out, key=lambda x: x["id"])


# ---------------------------------------------------------------- Konfiguration
def adaptive_overrides(total_days: float, profile: str) -> Dict:
    """Fenster-Einstellungen aus dem analysierten Zeitraum ableiten.
    Alle Werte in Tagen; Grenzen verhindern unsinnige Extreme."""
    p = ADAPT_PROFILES.get(profile)
    if not p:
        return {}
    d = max(float(total_days), 5.0)

    def clip(v, lo, hi):
        return round(float(min(max(v, lo), hi)), 3)

    hz = sorted({clip(d * f, 2.0, 500.0) for f in p["horizons"]})
    return {
        "horizons_days": hz,
        "min_hold_days": clip(d * p["hold"], 1.0, 30.0),
        "confirm_days": clip(d * p["confirm"], 0.25, 20.0),
        "smooth_days": clip(d * p["smooth"], 0.1, 15.0),
        "vol_ref_days": clip(d * p["vol_ref"], 20.0, 720.0),
        "vol_window_days": clip(d * p["vol_window"], 1.0, 40.0),
        "vol_smooth_days": clip(d * p["vol_window"] * 1.6, 1.0, 45.0),
        "gate_timeout_days": clip(d * p["hold"] * 3.0, 3.0, 240.0),
        # Reaktive Erkennung: Umkehr-Schwelle und Stall-Fenster je Profil
        "rev_atr_mult": p.get("rev", 3.0),
        "side_stall_days": clip(d * p.get("stall_frac", 0.008), 0.4, 12.0),
        # Prüfung: was als "eigener Abschnitt" gilt, hängt am Zeitraum
        "validate_min_segment_days": clip(d * 0.005, 0.5, 30.0),
    }


def resolve_config(config: Optional[Dict], timeframe: str, n_bars: int = 10 ** 9) -> Dict:
    """Nutzer-Konfiguration validieren und Tages-Angaben in Bars umrechnen.
    Horizonte, die für die Datenmenge zu lang sind, werden verworfen (mind. 1)."""
    user = {k: v for k, v in (config or {}).items() if v is not None}
    cfg = dict(DEFAULT_CONFIG)
    bpd = rf.bars_per_day(timeframe)
    total_days = float(n_bars) / max(bpd, 1e-9) if n_bars < 10 ** 8 else 0.0

    # Adaptive Basis ZUERST, damit manuell gesetzte Felder Vorrang behalten.
    cfg["regime_mode"] = norm_mode(user.get("regime_mode", cfg["regime_mode"]))
    auto = bool(user.get("auto_adapt", cfg["auto_adapt"]))
    prof = str(user.get("adapt_profile", cfg["adapt_profile"]) or "auto").lower()
    if prof not in ADAPT_PROFILES and prof not in ("auto", "off"):
        prof = "auto"
    cfg["auto_adapt"] = auto
    cfg["adapt_profile"] = prof
    applied = None
    if auto and prof != "off" and total_days >= 10:
        applied = prof if prof in ADAPT_PROFILES else "standard"
        cfg.update(adaptive_overrides(total_days, applied))
    cfg["adapt_applied"] = applied
    cfg["total_days"] = round(total_days, 2)

    for k, v in user.items():
        if k in cfg:
            cfg[k] = v

    def _f(key, lo, hi):
        cfg[key] = float(min(max(float(cfg[key]), lo), hi))

    hz = [float(h) for h in (cfg.get("horizons_days") or [])
          if isinstance(h, (int, float)) and float(h) > 0]
    hz = sorted({round(min(max(h, 0.25), 1000.0), 3) for h in hz})[:8] or [10.0, 30.0]
    _f("trend_t", 0.5, 10.0)
    _f("trend_strong_t", cfg["trend_t"], 20.0)
    _f("t_clip", 3.0, 50.0)
    _f("agreement_weight", 0.0, 1.0)
    _f("di_weight", 0.0, 0.5)
    _f("adx_min", 0.0, 60.0)
    _f("adx_period_days", 0.1, 30.0)
    _f("efficiency_min", 0.0, 1.0)
    _f("efficiency_days", 0.5, 200.0)
    _f("range_break_pos", 0.5, 1.0)
    _f("long_confirm_frac", 0.0, 3.0)
    _f("gate_timeout_days", 0.0, 365.0)
    cfg["strong_bypass_range"] = bool(cfg["strong_bypass_range"])
    cfg["use_variance_ratio"] = bool(cfg["use_variance_ratio"])
    _f("vr_k_days", 1.0, 120.0)
    _f("vr_min", 0.0, 2.0)
    cfg["vr_window_days"] = float(min(max(float(cfg["vr_window_days"]), 0.0), 2000.0))
    cfg["range_lag_days"] = float(min(max(float(cfg["range_lag_days"]), 0.0), 1000.0))
    cfg["require_range_break"] = bool(cfg["require_range_break"])
    cfg["range_window_days"] = float(min(max(float(cfg["range_window_days"]), 0.0), 1000.0))
    _f("smooth_days", 0.0, 30.0)
    _f("vol_window_days", 0.25, 60.0)
    _f("vol_smooth_days", 0.0, 60.0)
    _f("vol_ref_days", 5.0, 720.0)
    _f("vol_low_z", -5.0, 0.0)
    _f("vol_high_z", 0.0, 5.0)
    _f("vol_hysteresis", 0.0, 2.0)
    _f("hysteresis", 0.0, 0.9)
    _f("confirm_days", 0.0, 60.0)
    _f("min_hold_days", 0.0, 120.0)
    _f("confidence_min", 0.0, 0.99)
    _f("validate_side_max_pct_per_day", 0.01, 5.0)
    _f("validate_side_t", 1.0, 10.0)
    _f("validate_min_segment_days", 0.5, 120.0)
    _f("validate_tol_pct", 0.0, 20.0)
    _f("validate_tol_t", 0.0, 5.0)
    _f("validate_vol_tol_mult", 0.0, 10.0)
    cfg["require_adx"] = bool(cfg["require_adx"])
    cfg["require_efficiency"] = bool(cfg["require_efficiency"])
    cfg["use_di"] = bool(cfg["use_di"])
    cfg["vol_metric"] = "stdev" if str(cfg["vol_metric"]).lower() == "stdev" else "atr"
    # --- Reaktive Erkennung (Umkehrpunkte) ---
    det_in = str(cfg.get("detector") or "").lower()
    cfg["detector"] = (det_in if det_in in ("regression", "ema", "kombi")
                       else "reactive")
    _f("ema_regime_days", 2.0, 100.0)
    _f("ema_regime_thr", 0.05, 1.0)
    _f("ema_regime_smooth_days", 0.25, 10.0)
    _f("ema_regime_persist_days", 0.0, 30.0)
    _f("kombi_ema_days", 2.0, 100.0)
    _f("kombi_thr", 0.05, 1.0)
    _f("kombi_slope_days", 0.5, 20.0)
    _f("kombi_persist_days", 0.0, 30.0)
    _f("kombi_dominance_days", 0.0, 15.0)
    cfg["kombi_pivot_accel"] = bool(cfg.get("kombi_pivot_accel", True))
    _f("rev_atr_mult", 0.5, 15.0)
    _f("rev_min_pct", 0.0, 10.0)
    _f("rev_max_pct", 1.0, 60.0)
    try:
        cfg["persist_candles"] = min(max(int(cfg.get("persist_candles") or 3), 1), 20)
    except (TypeError, ValueError):
        cfg["persist_candles"] = 3
    _f("side_leg_atr_mult", 0.5, 6.0)
    _f("side_stall_days", 0.0, 60.0)
    _f("min_phase_days", 0.0, 90.0)
    cfg["mtf_confirm"] = bool(cfg.get("mtf_confirm", True))
    _f("mtf_mult", 1.2, 8.0)
    cfg["use_volume_confirm"] = bool(cfg.get("use_volume_confirm", True))
    _f("volume_boost", 1.1, 5.0)
    # --- EMA-Trendlinien ---
    cfg["use_ema_confirm"] = bool(cfg.get("use_ema_confirm", True))
    _f("ema_fast_days", 2.0, 100.0)
    _f("ema_mid_days", 3.0, 250.0)
    _f("ema_slow_days", 5.0, 500.0)
    if cfg["ema_mid_days"] <= cfg["ema_fast_days"]:
        cfg["ema_mid_days"] = round(cfg["ema_fast_days"] * 2.0, 2)
    if cfg["ema_slow_days"] <= cfg["ema_mid_days"]:
        cfg["ema_slow_days"] = round(cfg["ema_mid_days"] * 2.0, 2)
    _f("ema_slope_thr", 0.02, 2.0)
    _f("ema_persist_days", 0.0, 15.0)
    try:
        chart_emas = sorted({round(float(x), 1) for x in
                             (cfg.get("chart_ema_days") or [])
                             if 1.0 <= float(x) <= 1000.0})[:6]
    except (TypeError, ValueError):
        chart_emas = []
    cfg["chart_ema_days"] = chart_emas or list(DEFAULT_CONFIG["chart_ema_days"])

    # Horizonte in Bars; zu lange Horizonte für die Datenmenge entfernen
    usable = [h for h in hz if int(h * bpd) + 5 < n_bars]
    if not usable:
        usable = [min(hz)]
    bars = [max(int(round(h * bpd)), 8) for h in usable]
    weights = cfg.get("horizon_weights")
    if not (isinstance(weights, (list, tuple)) and len(weights) == len(usable)):
        weights = [1.0 + 0.25 * i for i in range(len(usable))]  # längere leicht höher
    weights = [max(float(w), 0.0) for w in weights]
    if sum(weights) <= 0:
        weights = [1.0] * len(usable)

    cfg["horizons_days"] = usable
    cfg["horizon_weights"] = weights
    cfg["horizon_bars"] = bars
    cfg["bars_per_day"] = bpd
    cfg["adx_period_bars"] = max(int(round(cfg["adx_period_days"] * bpd)), 5)
    cfg["vol_window_bars"] = max(int(round(cfg["vol_window_days"] * bpd)), 5)
    cfg["vol_smooth_bars"] = max(int(round(cfg["vol_smooth_days"] * bpd)), 1)
    cfg["vol_ref_bars"] = max(int(round(cfg["vol_ref_days"] * bpd)), 20)
    cfg["efficiency_bars"] = max(int(round(cfg["efficiency_days"] * bpd)), 5)
    cfg["range_bars"] = (max(int(round(cfg["range_window_days"] * bpd)), 10)
                         if cfg["range_window_days"] > 0
                         else (bars[-2] if len(bars) > 1 else bars[-1]))
    cfg["range_lag_bars"] = (max(int(round(cfg["range_lag_days"] * bpd)), 1)
                             if cfg["range_lag_days"] > 0 else max(min(bars), 1))
    cfg["gate_timeout_bars"] = max(int(round(cfg["gate_timeout_days"] * bpd)), 1)
    cfg["vr_k_bars"] = max(int(round(cfg["vr_k_days"] * bpd)), 2)
    cfg["vr_window_bars"] = (max(int(round(cfg["vr_window_days"] * bpd)), 20)
                             if cfg["vr_window_days"] > 0 else max(bars))
    cfg["smooth_bars"] = max(int(round(cfg["smooth_days"] * bpd)), 1)
    cfg["confirm_bars"] = max(int(round(cfg["confirm_days"] * bpd)), 1)
    cfg["min_hold_bars"] = max(int(round(cfg["min_hold_days"] * bpd)), 1)
    cfg["persist_bars"] = cfg["persist_candles"]
    cfg["ema_fast_bars"] = max(int(round(cfg["ema_fast_days"] * bpd)), 2)
    cfg["ema_mid_bars"] = max(int(round(cfg["ema_mid_days"] * bpd)), 3)
    cfg["ema_slow_bars"] = max(int(round(cfg["ema_slow_days"] * bpd)), 4)
    cfg["ema_persist_bars"] = max(int(round(cfg["ema_persist_days"] * bpd)), 1)
    cfg["stall_bars"] = (max(int(round(cfg["side_stall_days"] * bpd)), 2)
                         if cfg["side_stall_days"] > 0
                         else max(int(round(bpd * 1.5)), cfg["persist_bars"] * 6))
    cfg["warmup_bars"] = max(max(bars), cfg["adx_period_bars"] * 2,
                             cfg["vol_window_bars"] + 5)
    return cfg


# ---------------------------------------------------------------- Features
def compute_matrix(candles, cfg: Dict) -> Dict[str, np.ndarray]:
    """Alle Regime-Features je Kerze (rein rückblickend)."""
    high, low, close, vol = rf.ohlc(candles)
    n = len(close)
    logc = np.log(np.maximum(close, rf.EPS))
    tmax = cfg["t_clip"]
    bars, weights = cfg["horizon_bars"], cfg["horizon_weights"]
    wsum = float(sum(weights)) or 1.0

    t_stack, r2_stack, ret_stack = [], [], []
    for w in bars:
        _slope, t, r2 = rf.ols_stats(logc, w)
        t_stack.append(np.clip(t, -tmax, tmax))
        r2_stack.append(r2)
        ret = np.full(n, np.nan)
        if n > w:
            ret[w:] = (close[w:] / np.maximum(close[:-w], rf.EPS) - 1.0) * 100.0
        ret_stack.append(ret)
    T = np.vstack(t_stack) if t_stack else np.zeros((1, n))
    W = np.array(weights, dtype=float).reshape(-1, 1)
    t_w = np.nansum(T * W, axis=0) / wsum
    t_w = np.where(np.all(np.isnan(T), axis=0), np.nan, t_w)

    sign_w = np.sign(t_w)
    agree_num = np.nansum(np.where(np.sign(T) == sign_w, W, 0.0), axis=0)
    agreement = np.where(np.abs(sign_w) > 0, agree_num / wsum, 0.0)

    adx, pdi, mdi = rf.adx_di(high, low, close, cfg["adx_period_bars"])
    eff = rf.efficiency_ratio(close, cfg["efficiency_bars"])
    range_pos = rf.range_position(close, cfg["range_bars"])
    hi_lag, lo_lag = rf.donchian_lagged(close, cfg["range_bars"], cfg["range_lag_bars"])
    new_high = close >= hi_lag * 0.999
    new_low = close <= lo_lag * 1.001
    new_high = np.where(np.isfinite(hi_lag), new_high, False)
    new_low = np.where(np.isfinite(lo_lag), new_low, False)
    vr = (rf.variance_ratio(close, cfg["vr_k_bars"], cfg["vr_window_bars"])
          if cfg.get("use_variance_ratio") else np.full(n, np.nan))
    gate_ready = np.isfinite(hi_lag) & np.isfinite(lo_lag) & np.isfinite(t_stack[-1])

    if cfg["vol_metric"] == "stdev":
        vol_raw = rf.realized_vol_pct(close, cfg["vol_window_bars"], cfg["bars_per_day"])
    else:
        vol_raw = rf.atr_pct(high, low, close, cfg["vol_window_bars"])
    if cfg.get("vol_smooth_bars", 1) > 1:
        vmask = np.isfinite(vol_raw)
        if vmask.any():
            vs = vol_raw.copy()
            vs[vmask] = rf.ema(vol_raw[vmask], cfg["vol_smooth_bars"])
            vol_raw = vs
    vol_z = rf.rolling_zscore(vol_raw, cfg["vol_ref_bars"])
    daily_vol = rf.realized_vol_pct(close, cfg["vol_window_bars"], cfg["bars_per_day"])
    atr_arr = rf.atr_pct(high, low, close, cfg["vol_window_bars"])

    aw = cfg["agreement_weight"]
    score = t_w * ((1.0 - aw) + aw * agreement)
    if cfg["use_di"]:
        di_dir = np.sign(np.nan_to_num(pdi) - np.nan_to_num(mdi))
        match = np.where(di_dir == np.sign(score), 1.0, -1.0)
        score = score * (1.0 + cfg["di_weight"] * match)
    if cfg["smooth_bars"] > 1:
        valid = np.isfinite(score)
        if valid.any():
            sm = score.copy()
            sm[valid] = rf.ema(score[valid], cfg["smooth_bars"])
            score = sm

    primary = int(np.argmax(np.array(bars)))       # längster Horizont für Statistik
    trend_pct_per_day = ret_stack[primary] / max(cfg["horizons_days"][primary], 1e-9)
    t_long = t_stack[primary]

    # --- EMA-Trendlinien (schnell/mittel/langsam) + Steigung in %/Tag ---
    ema_feats = {}
    for key in ("fast", "mid", "slow"):
        span = int(cfg.get(f"ema_{key}_bars")
                   or max(int(round(float(DEFAULT_CONFIG[f"ema_{key}_days"])
                                    * cfg["bars_per_day"])), 2))
        e = rf.ema(close, span)
        k = max(int(round(span * 0.25)), 1)
        sl = np.zeros(n)
        if n > k:
            sl[k:] = ((e[k:] / np.maximum(e[:-k], rf.EPS) - 1.0) * 100.0
                      / (k / max(cfg["bars_per_day"], 1e-9)))
        ema_feats["ema_" + key] = e
        ema_feats["ema_slope_" + key] = sl

    return {"score": score, "t_weighted": t_w, "agreement": agreement,
            "adx": adx, "plus_di": pdi, "minus_di": mdi, "efficiency": eff,
            "range_pos": range_pos, "new_high": new_high, "new_low": new_low,
            "variance_ratio": vr, "gate_ready": gate_ready,
            "t_long": t_long,
            "vol_raw": vol_raw, "vol_z": vol_z, "daily_vol_pct": daily_vol,
            "r2": r2_stack[primary], "trend_pct_per_day": trend_pct_per_day,
            "t_per_horizon": T, "ret_per_horizon": np.vstack(ret_stack),
            "high": high, "low": low, "atr_pct": atr_arr, "volume": vol,
            "close": close, **ema_feats}


def _bands(f: Dict[str, np.ndarray], cfg: Dict):
    """Ein-/Ausstiegs-Bänder (Hysterese) + Confidence je Kerze."""
    score = f["score"]
    adx = f["adx"]
    eff = f["efficiency"]
    vz = f["vol_z"]
    thr, h = cfg["trend_t"], cfg["hysteresis"]
    strong = cfg["trend_strong_t"]

    adx_ok = np.ones(len(score), dtype=bool)
    if cfg["require_adx"]:
        adx_ok = (np.nan_to_num(adx, nan=0.0) >= cfg["adx_min"]) | \
                 (np.abs(np.nan_to_num(score)) >= strong)
    eff_ok = np.ones(len(score), dtype=bool)
    if cfg["require_efficiency"]:
        eff_ok = np.nan_to_num(eff, nan=0.0) >= cfg["efficiency_min"]
    ok = adx_ok & eff_ok

    up_ok = ok.copy()
    dn_ok = ok.copy()
    raw_up = score >= thr * (1 + h)
    raw_dn = score <= -thr * (1 + h)
    if cfg.get("require_range_break"):
        # Trend-EINSTIEG nur bei neuem Extrem (Ausbruch aus der alten Spanne)
        # ODER wenn der längste Horizont den Trend bestätigt ODER wenn der Score
        # lange genug hält / sehr stark ist. So werden Range-Schwingungen
        # gefiltert, echte Trendwenden aber nicht verschluckt.
        tl = np.nan_to_num(f.get("t_long"), nan=0.0)
        lc = cfg.get("long_confirm_frac", 0.75) * thr
        timeout = cfg.get("gate_timeout_bars", 10 ** 9)
        hold_up = rf.run_length(np.nan_to_num(raw_up, nan=False)) >= timeout
        hold_dn = rf.run_length(np.nan_to_num(raw_dn, nan=False)) >= timeout
        strong_ok = (np.abs(np.nan_to_num(score)) >= strong) \
            if cfg.get("strong_bypass_range") else np.zeros(len(score), dtype=bool)
        # Solange die Filter-Daten (langer Horizont / Spanne) noch nicht
        # vorliegen (Aufwärmphase), darf der Filter nicht blockieren.
        not_ready = ~np.asarray(f.get("gate_ready",
                                      np.ones(len(score), dtype=bool)), dtype=bool)
        up_ok = up_ok & (f["new_high"].astype(bool) | (tl >= lc) | hold_up
                        | strong_ok | not_ready)
        dn_ok = dn_ok & (f["new_low"].astype(bool) | (tl <= -lc) | hold_dn
                        | strong_ok | not_ready)
    if cfg.get("use_variance_ratio"):
        # Mean-reverting Markt (VR < vr_min) => kein Trend-Einstieg (Range)
        vr = f.get("variance_ratio")
        vr_ok = ~(np.isfinite(vr) & (vr < cfg["vr_min"]))
        up_ok = up_ok & vr_ok
        dn_ok = dn_ok & vr_ok

    enter_up = raw_up & up_ok
    enter_dn = raw_dn & dn_ok
    stay_up = score >= thr * (1 - h)
    stay_dn = score <= -thr * (1 - h)

    hv = cfg["vol_hysteresis"]
    lo_z, hi_z = cfg["vol_low_z"], cfg["vol_high_z"]
    enter_low = vz <= lo_z - hv
    stay_low = vz <= lo_z + hv
    enter_high = vz >= hi_z + hv
    stay_high = vz >= hi_z - hv

    # 5er-Modus: zweite Achse ist die Trendstärke (stark vs. leicht) mit eigener
    # Hysterese, damit ein Trend nicht ständig zwischen stark/leicht kippt.
    a_score = np.abs(np.nan_to_num(score))
    enter_strong = a_score >= strong * (1 + h)
    stay_strong = a_score >= strong * (1 - h)

    d_trend = np.clip(np.abs(np.abs(score) - thr) / max(thr, 1e-9), 0.0, 1.0)
    if norm_mode(cfg.get("regime_mode", 9)) == 9:
        d_edge = np.minimum(np.abs(vz - lo_z), np.abs(vz - hi_z))
        d_vol = np.clip(d_edge / 0.6, 0.0, 1.0)
        conf = np.clip(0.5 * d_trend + 0.25 * d_vol + 0.25 * f["agreement"], 0.0, 1.0)
    else:
        # 3/5 Regime: die Volatilitätsstufe spielt keine Rolle -> Sicherheit
        # allein aus Trend-Abstand und Horizont-Konsens.
        conf = np.clip(0.65 * d_trend + 0.35 * f["agreement"], 0.0, 1.0)

    valid = np.isfinite(score) & np.isfinite(vz) & (np.isfinite(adx) | ~cfg["require_adx"])
    return {"enter_up": np.nan_to_num(enter_up, nan=False).astype(bool),
            "enter_dn": np.nan_to_num(enter_dn, nan=False).astype(bool),
            "stay_up": np.nan_to_num(stay_up, nan=False).astype(bool),
            "stay_dn": np.nan_to_num(stay_dn, nan=False).astype(bool),
            "enter_low": np.nan_to_num(enter_low, nan=False).astype(bool),
            "stay_low": np.nan_to_num(stay_low, nan=False).astype(bool),
            "enter_high": np.nan_to_num(enter_high, nan=False).astype(bool),
            "stay_high": np.nan_to_num(stay_high, nan=False).astype(bool),
            "enter_strong": enter_strong.astype(bool),
            "stay_strong": stay_strong.astype(bool),
            "conf": np.nan_to_num(conf, nan=0.0), "valid": valid}


def _raw_state(b: Dict, i: int, mode: int = 9) -> Tuple[int, int]:
    t = 2 if b["enter_up"][i] else (0 if b["enter_dn"][i] else 1)
    if mode == 9:
        s = 2 if b["enter_high"][i] else (0 if b["enter_low"][i] else 1)
    elif mode == 5:
        s = 1 if (t != 1 and b["enter_strong"][i]) else 0
    else:
        s = 0
    return t, s


def _desired_state(b: Dict, i: int, cur_t: int, cur_v: int,
                   mode: int = 9) -> Tuple[int, int]:
    if cur_t == 2:
        t = 2 if b["stay_up"][i] else (0 if b["enter_dn"][i] else 1)
    elif cur_t == 0:
        t = 0 if b["stay_dn"][i] else (2 if b["enter_up"][i] else 1)
    else:
        t = 2 if b["enter_up"][i] else (0 if b["enter_dn"][i] else 1)
    if mode == 9:
        if cur_v == 2:
            v = 2 if b["stay_high"][i] else (0 if b["enter_low"][i] else 1)
        elif cur_v == 0:
            v = 0 if b["stay_low"][i] else (2 if b["enter_high"][i] else 1)
        else:
            v = 2 if b["enter_high"][i] else (0 if b["enter_low"][i] else 1)
    elif mode == 5:
        if t == 1:
            v = 0
        elif cur_v == 1 and cur_t == t:
            v = 1 if b["stay_strong"][i] else 0
        else:
            v = 1 if b["enter_strong"][i] else 0
    else:
        v = 0
    return t, v


def classify_arrays(f: Dict[str, np.ndarray], cfg: Dict,
                    conf_min: float = None, min_hold_bars: int = None,
                    return_detail: bool = False):
    """Klassifikation je Kerze. Standard-Detector "reactive": Erkennung über
    bestätigte Umkehrpunkte (services.regime_reactive) – reaktive Bestätigung
    statt Prediction. "regression": alter v2-Zustandsautomat.
    Rückgabe: (regime-ids (-1 = unbekannt), confidence[, detail])."""
    if str(cfg.get("detector") or "reactive") != "regression":
        from services import regime_reactive as rx
        hold = cfg["min_hold_bars"] if min_hold_bars is None \
            else max(int(min_hold_bars), 1)
        ids, conf, det = rx.classify(f, cfg, conf_min=conf_min,
                                     min_hold_bars=hold)
        return (ids, conf, det) if return_detail else (ids, conf)
    b = _bands(f, cfg)
    n = len(f["score"])
    mode = norm_mode(cfg.get("regime_mode", 9))
    cmin = cfg["confidence_min"] if conf_min is None else float(conf_min)
    hold = cfg["min_hold_bars"] if min_hold_bars is None else max(int(min_hold_bars), 1)
    confirm = cfg["confirm_bars"]
    out = np.full(n, -1, dtype=int)
    conf_out = np.zeros(n)
    cur_t = cur_v = None
    since = 0
    pend = None
    pend_n = 0
    for i in range(n):
        if not b["valid"][i]:
            out[i] = -1 if cur_t is None else regime_id(cur_t, cur_v, mode)
            conf_out[i] = 0.0 if cur_t is None else b["conf"][i]
            continue
        if cur_t is None:
            cur_t, cur_v = _raw_state(b, i, mode)
            since = i
        else:
            dt, dv = _desired_state(b, i, cur_t, cur_v, mode)
            if (dt, dv) == (cur_t, cur_v):
                pend, pend_n = None, 0
            else:
                if pend == (dt, dv):
                    pend_n += 1
                else:
                    pend, pend_n = (dt, dv), 1
                # Nur die Trendrichtung braucht die volle Mindesthaltedauer.
                # Im 9er-Modus darf die Volatilitätsstufe früher wechseln (sie
                # ist eine eigene, schnellere Achse); im 3er/5er-Modus gehört
                # die Stärke zur Regime-Identität -> volle Haltedauer.
                axis_only = dt == cur_t and mode == 9
                need_hold = 1 if axis_only else hold
                if pend_n >= confirm and (i - since) >= need_hold \
                        and b["conf"][i] >= cmin:
                    cur_t, cur_v = dt, dv
                    if not axis_only:
                        since = i
                    pend, pend_n = None, 0
        out[i] = regime_id(cur_t, cur_v, mode)
        conf_out[i] = b["conf"][i]
    return (out, conf_out, None) if return_detail else (out, conf_out)


def _hold_override(cfg: Dict, min_hold_days) -> Optional[int]:
    """Mindesthaltedauer aus dem Aufruf nur übernehmen, wenn KEINE automatische
    Anpassung aktiv ist – sonst würde der Oberflächen-Standard (z.B. 2 Tage) die
    zeitraum-abhängige Glättung aushebeln."""
    if min_hold_days is None or cfg.get("adapt_applied"):
        return None
    return max(int(round(float(min_hold_days) * cfg["bars_per_day"])), 1)


def classify_series(model: Dict, candles, conf_min: float = None,
                    min_hold_days: float = None) -> List[Optional[int]]:
    cfg = dict(model["config"])
    hold = _hold_override(cfg, min_hold_days)
    f = compute_matrix(candles, cfg)
    ids, _conf = classify_arrays(f, cfg, conf_min, hold)
    return [None if r < 0 else int(r) for r in ids]


def final_labels(model: Dict, candles) -> List[Optional[int]]:
    """Pivot-korrigierte Phasen (nur reaktiver Detector): jede Phase beginnt am
    bestätigten Hoch-/Tiefpunkt. Für die Analyse-Ansicht und die Regime-
    Strategie-Suche – Live/Backtests nutzen weiterhin classify_series."""
    cfg = dict(model["config"])
    if str(cfg.get("detector") or "reactive") == "regression":
        return classify_series(model, candles)
    from services import regime_reactive as rx
    f = compute_matrix(candles, cfg)
    ids = rx.final_ids_from(rx.classify(f, cfg)[2], f, cfg)
    return [None if r < 0 else int(r) for r in ids]


def reactive_payload(model: Dict, candles) -> Dict:
    """Live-Labels, pivot-korrigierte Final-Labels und Selbstkorrektur-Report
    in EINEM Durchlauf (für das Regime-Lab)."""
    from services import regime_reactive as rx
    cfg = dict(model["config"])
    f = compute_matrix(candles, cfg)
    return rx.full_payload(f, cfg, candles)


def segments_from_labels(labels) -> List[Tuple[int, int, int]]:
    segs = []
    start, cur = None, None
    for i, r in enumerate(labels):
        if r is None or r < 0:
            continue
        if cur is None:
            start, cur = i, r
        elif r != cur:
            segs.append((start, i, cur))
            start, cur = i, r
    if cur is not None:
        segs.append((start, len(labels), cur))
    return segs


# ---------------------------------------------------------------- Modell bauen
def _regime_stats(f: Dict, ids: np.ndarray, cfg: Dict) -> Dict[int, Dict]:
    out = {}
    for rid in sorted(set(int(x) for x in np.unique(ids) if x >= 0)):
        m = ids == rid
        if not m.any():
            continue
        def _mean(key):
            v = f[key][m]
            v = v[np.isfinite(v)]
            return float(np.mean(v)) if len(v) else 0.0
        out[rid] = {"bars": int(m.sum()),
                    "score": round(_mean("score"), 3),
                    "trend_pct_per_day": round(_mean("trend_pct_per_day"), 4),
                    "adx": round(_mean("adx"), 2),
                    "efficiency": round(_mean("efficiency"), 3),
                    "daily_vol_pct": round(_mean("daily_vol_pct"), 3),
                    "vol_pct": round(_mean("daily_vol_pct"), 3),
                    "vol_z": round(_mean("vol_z"), 3),
                    "agreement": round(_mean("agreement"), 3)}
    return out


def build_model(histories: Dict[str, List[Dict]], timeframe: str,
                config: Dict = None) -> Optional[Dict]:
    """Regime-"Modell" v2: die Taxonomie ist fest, trainiert werden nur die
    Statistiken/Anteile (für Anzeige und Plausibilitätsprüfung).

    Ist `adapt_profile == "auto"`, werden alle Glättungs-Profile geprüft und
    das mit der besten Bewertung genommen (siehe `_profile_quality`)."""
    lens = [len(c) for c in (histories or {}).values() if c is not None]
    if not lens or max(lens) < 60:
        return None
    n_bars = max(lens)
    base = dict(config or {})
    prof_req = str(base.get("adapt_profile") or DEFAULT_CONFIG["adapt_profile"]).lower()
    auto_on = bool(base.get("auto_adapt", DEFAULT_CONFIG["auto_adapt"]))
    adapt_report = None
    if auto_on and prof_req == "auto":
        cfgs = {p: resolve_config({**base, "adapt_profile": p}, timeframe, n_bars)
                for p in ADAPT_ORDER}
        scored = []
        for p, c in cfgs.items():
            q = _profile_quality(histories, c)
            scored.append({"profile": p, "label": ADAPT_PROFILES[p]["label"], **q})
        # Plausibilität ist die harte Hürde: ein Profil, dessen Labels dem
        # Kursverlauf widersprechen, gewinnt nie – egal wie gut die übrigen
        # Kennzahlen sind. Erst unter den bestandenen wird nach Qualität sortiert.
        scored.sort(key=lambda x: (-int(bool(x.get("passes"))), -x["quality"]))
        cfg = cfgs[scored[0]["profile"]]
        cfg["adapt_applied"] = scored[0]["profile"]
        adapt_report = {"chosen": scored[0]["profile"], "candidates": scored}
    else:
        cfg = resolve_config(base, timeframe, n_bars)
    mode = norm_mode(cfg.get("regime_mode", 9))
    agg: Dict[int, Dict] = {}
    total_bars = 0
    for candles in histories.values():
        if len(candles) < cfg["warmup_bars"] + 20:
            continue
        f = compute_matrix(candles, cfg)
        ids, _ = classify_arrays(f, cfg)
        stats = _regime_stats(f, ids, cfg)
        for rid, st in stats.items():
            a = agg.setdefault(rid, {"bars": 0, "acc": {}})
            a["bars"] += st["bars"]
            for k, v in st.items():
                if k == "bars":
                    continue
                a["acc"][k] = a["acc"].get(k, 0.0) + v * st["bars"]
        total_bars += int((ids >= 0).sum())
    if not agg or total_bars <= 0:
        return None
    subs = sub_states(mode)
    regimes = []
    for rid in sorted(agg.keys()):
        a = agg[rid]
        stats = {k: round(v / max(a["bars"], 1), 4) for k, v in a["acc"].items()}
        t, s = split_id(rid, mode)
        if mode == 5:
            strength = "-" if t == 1 else ("stark" if s == 1 else "leicht")
        else:
            strength = ("stark" if abs(stats.get("score", 0)) >= cfg["trend_strong_t"]
                        else "moderat")
        entry = {
            "id": rid, "label": regime_label(rid, mode), "key": regime_key(rid, mode),
            "trend": TREND_STATES[t][0],
            "nnfx": nnfx_regime(rid, mode),
            "nnfx_label": NNFX_LABELS[nnfx_regime(rid, mode)],
            "strength": strength if t != 1 else "-",
            "share_pct": round(a["bars"] / total_bars * 100, 1),
            "bars": a["bars"],
            "features": {"trend_pct": round(stats.get("trend_pct_per_day", 0)
                                            * cfg["horizons_days"][-1], 3),
                         "vol_pct": stats.get("daily_vol_pct", 0),
                         "efficiency": stats.get("efficiency", 0),
                         "rel_volume": 1.0},
            "stats": {**stats, "trend_strength": abs(stats.get("score", 0))},
        }
        # `vol` nur im 9er-Modus setzen – sonst würde die Oberfläche eine
        # Volatilitätsstufe anzeigen/einfärben, die es gar nicht gibt.
        if mode == 9:
            entry["vol"] = VOL_STATES[s][0]
        elif mode == 5 and t != 1:
            entry["strength_key"] = subs[s][0]
        regimes.append(entry)
    return {"engine": ENGINE, "timeframe": timeframe, "config": cfg,
            "regime_mode": mode,
            "lookback_bars": int(cfg["warmup_bars"]),
            "lookback_days": round(max(cfg["horizons_days"]), 2),
            "bars_per_day": cfg["bars_per_day"],
            "adapt": {"profile": cfg.get("adapt_applied"),
                      "total_days": cfg.get("total_days"),
                      "horizons_days": cfg.get("horizons_days"),
                      "min_hold_days": cfg.get("min_hold_days"),
                      "confirm_days": cfg.get("confirm_days"),
                      "report": adapt_report},
            "taxonomy": taxonomy(mode), "regimes": regimes,
            "n_samples": int(total_bars), "symbols": list((histories or {}).keys())}


def _profile_quality(histories: Dict[str, List[Dict]], cfg: Dict) -> Dict:
    """Bewertung eines Glättungs-Profils – nachvollziehbar aus drei Bausteinen:

    1. PLAUSIBILITÄT: Anteil der Bars, deren Label nicht zum Kursverlauf passt
       (`validate_labels`) -> möglichst 0.
    2. RÜCKBLICK-TREFFER: Übereinstimmung der Live-Erkennung mit der zentrierten
       Rückblick-Sicht (`ideal_labels`, nur zur Bewertung) -> möglichst 100%.
    3. ABSCHNITTSLÄNGE: durchschnittliche Regime-Dauer gegenüber einem Zielwert
       (Zeitraum / erwartete Anzahl Abschnitte) -> Rauschen und Trägheit werden
       gleichermaßen bestraft.
    """
    mode = norm_mode(cfg.get("regime_mode", 9))
    total_days = float(cfg.get("total_days") or 0) or 1.0
    target_segments = {3: 8.0, 5: 12.0, 9: 18.0}[mode]
    target_days = max(total_days / target_segments, 1.0)
    viol, agree, seglen, lf, n = [], [], [], [], 0
    for candles in histories.values():
        if len(candles) < cfg["warmup_bars"] + 20:
            continue
        f = compute_matrix(candles, cfg)
        ids, _, det = classify_arrays(f, cfg, return_detail=True)
        try:
            # Bewertet werden die FINAL-Labels (das sieht der Nutzer im Lab
            # und darauf laufen Validierung/Strategie-Suche) …
            if isinstance(det, dict) and det.get("final3") is not None:
                from services import regime_reactive as _rx
                det.setdefault("mode", mode)
                ids = _rx.final_ids_from(det, f, cfg)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"profile quality final ids: {e}")
        labels = [None if r < 0 else int(r) for r in ids]
        model = {"config": cfg, "engine": ENGINE}
        try:
            # … PLUS: wie gut trifft die kausale Live-Sicht die korrigierten
            # Phasen? (entscheidend für Paper-/Live-Trading)
            warm = int(det.get("warm") or 0)
            lv = det.get("live3")
            fn = det.get("final3")
            if lv is not None and fn is not None and len(lv) > warm:
                lf.append(float(np.mean(np.asarray(lv)[warm:]
                                        == np.asarray(fn)[warm:])) * 100.0)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"profile quality live/final: {e}")
        try:
            v = validate_labels(candles, labels, model)
            viol.append(v["violation_bars_pct"])
            seglen.append(v["avg_segment_days"])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"profile quality validate: {e}")
        try:
            ideal = ideal_labels(model, candles)
            a = agreement_with_ideal(labels, ideal, mode)
            if a.get("direction_pct") is not None:
                agree.append(a["direction_pct"])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"profile quality ideal: {e}")
        n += 1
    if not n:
        return {"quality": 0.0, "passes": False, "violation_bars_pct": None,
                "direction_pct": None, "avg_segment_days": None}
    v_pct = sum(viol) / len(viol) if viol else 50.0
    a_pct = sum(agree) / len(agree) if agree else 50.0
    lf_pct = sum(lf) / len(lf) if lf else 50.0
    seg = sum(seglen) / len(seglen) if seglen else target_days
    ratio = max(seg, 0.1) / target_days
    seg_score = 100.0 * math.exp(-abs(math.log(ratio)))
    quality = (0.28 * a_pct + 0.28 * (100.0 - v_pct) + 0.16 * seg_score
               + 0.28 * lf_pct)
    return {"quality": round(quality, 2),
            "passes": v_pct <= VALIDATE_PASS_PCT,
            "violation_bars_pct": round(v_pct, 2),
            "direction_pct": round(a_pct, 1),
            "live_final_pct": round(lf_pct, 1),
            "avg_segment_days": round(seg, 2),
            "target_segment_days": round(target_days, 2),
            "segment_score": round(seg_score, 1)}


# ---------------------------------------------------------------- Frühwarnung
def early_warning(f: Dict, cfg: Dict, i: int, cur_t: int, cur_v: int,
                  b: Dict = None) -> Dict:
    """Frühwarnung für einen Regime-Wechsel – bevor Bestätigungsdauer und
    Mindesthaltedauer greifen.

    Drei unabhängige Bausteine (rein mathematisch, kein Lookahead):
    1. KANDIDAT: welcher Zustand wäre nach den Hysterese-Bändern gerade gewollt
       und wie lange hält dieser Wunsch schon an (pending_bars).
    2. ABSTAND: wie weit ist der Trend-Score von der nächsten Schwelle entfernt
       (normiert auf die Schwelle).
    3. MOMENTUM: Steigung des Scores pro Tag (Regression über das
       Bestätigungsfenster) -> geschätzte Tage bis zur Schwelle (ETA).
    Daraus ein Wahrscheinlichkeits-Score 0..100 (Heuristik, klar dokumentiert).
    """
    b = b if b is not None else _bands(f, cfg)
    mode = norm_mode(cfg.get("regime_mode", 9))
    thr, h = cfg["trend_t"], cfg["hysteresis"]
    score = f["score"]
    n = len(score)
    if i < 5 or not np.isfinite(score[i]):
        return {"active": False}
    dt, dv = _desired_state(b, i, cur_t, cur_v, mode)
    pending = (dt, dv) != (cur_t, cur_v)
    pending_bars = 0
    if pending:
        j = i
        while j > 0 and b["valid"][j] and \
                _desired_state(b, j, cur_t, cur_v, mode) == (dt, dv):
            pending_bars += 1
            j -= 1
            if pending_bars > 5 * max(cfg["confirm_bars"], 1):
                break

    # Momentum des Scores über das Bestätigungsfenster
    w = max(int(cfg["confirm_bars"]) * 2, 6)
    lo = max(i - w + 1, 0)
    seg = score[lo:i + 1]
    seg = seg[np.isfinite(seg)]
    slope_per_day = 0.0
    if len(seg) >= 4:
        x = np.arange(len(seg), dtype=float)
        x -= x.mean()
        denom = float((x * x).sum()) or 1.0
        slope_per_bar = float((x * (seg - seg.mean())).sum() / denom)
        slope_per_day = slope_per_bar * cfg["bars_per_day"]

    s_now = float(score[i])
    # Nächste relevante Schwelle in Richtung der Bewegung
    if cur_t == 1:
        target = thr * (1 + h) if slope_per_day >= 0 else -thr * (1 + h)
        target_t = 2 if slope_per_day >= 0 else 0
    elif cur_t == 2:
        target = thr * (1 - h)          # nach unten verlassen
        target_t = 1
    else:
        target = -thr * (1 - h)
        target_t = 1
    gap = target - s_now
    eta_days = None
    if slope_per_day != 0 and (gap > 0) == (slope_per_day > 0):
        eta_days = round(abs(gap) / abs(slope_per_day), 1)
    dist_norm = min(abs(gap) / max(thr, 1e-9), 3.0)

    prob = 0.0
    if pending:
        prob += 45.0 * min(pending_bars / max(cfg["confirm_bars"], 1), 1.0)
    prob += 35.0 * max(0.0, 1.0 - dist_norm)
    if eta_days is not None:
        prob += 20.0 * max(0.0, 1.0 - min(eta_days / 10.0, 1.0))
    prob = round(min(prob, 99.0), 1)

    next_t = dt if pending else target_t
    next_v = dv if pending else cur_v
    rid_next = regime_id(next_t, next_v, mode)
    return {"active": prob >= 25.0 or pending,
            "next_regime": rid_next, "next_label": regime_label(rid_next, mode),
            "next_nnfx": nnfx_regime(rid_next, mode),
            "probability_pct": prob,
            "eta_days": eta_days,
            "pending": bool(pending), "pending_bars": int(pending_bars),
            "pending_days": round(pending_bars / max(cfg["bars_per_day"], 1e-9), 2),
            "confirm_days": round(cfg["confirm_bars"] / max(cfg["bars_per_day"], 1e-9), 2),
            "score": round(s_now, 3),
            "score_slope_per_day": round(slope_per_day, 3),
            "distance_to_threshold": round(float(gap), 3),
            "reason": _warning_text(rid_next, prob, eta_days, pending, pending_bars,
                                    cfg, slope_per_day, mode)}


def _warning_text(rid_next: int, prob: float, eta: Optional[float], pending: bool,
                  pending_bars: int, cfg: Dict, slope: float, mode: int = 9) -> str:
    parts = []
    if pending:
        d = pending_bars / max(cfg["bars_per_day"], 1e-9)
        parts.append(f"Kandidat {regime_label(rid_next, mode)} seit {d:.1f} Tagen "
                     f"(Bestätigung ab {cfg['confirm_bars'] / max(cfg['bars_per_day'], 1e-9):.1f} Tagen)")
    else:
        parts.append(f"Nächster wahrscheinlicher Zustand: {regime_label(rid_next, mode)}")
    parts.append(f"Score-Momentum {slope:+.2f}/Tag")
    if eta is not None:
        parts.append(f"Schwelle in ca. {eta:.1f} Tagen")
    parts.append(f"Wahrscheinlichkeit {prob:.0f}%")
    return " · ".join(parts)


# ---------------------------------------------------------------- Live-Status
def current_regime(model: Dict, candles, conf_min: float = None,
                   min_hold_days: float = None) -> Dict:
    cfg = dict(model["config"])
    hold = _hold_override(cfg, min_hold_days)
    f = compute_matrix(candles, cfg)
    ids, conf, det = classify_arrays(f, cfg, conf_min, hold, return_detail=True)
    last = next((i for i in range(len(ids) - 1, -1, -1) if ids[i] >= 0), None)
    if last is None:
        return {"regime": None, "confidence": 0.0, "similarities": [],
                "last_switch": None, "reason": "Zu wenig Daten für die Klassifikation"}
    rid = int(ids[last])
    switch_i = last
    while switch_i > 0 and ids[switch_i - 1] == rid:
        switch_i -= 1
    ts = candles[switch_i]["timestamp"] if switch_i < len(candles) else None
    score = float(f["score"][last]) if np.isfinite(f["score"][last]) else 0.0
    detail = {"score": round(score, 3),
              "trend_t": round(float(np.nan_to_num(f["t_weighted"][last])), 3),
              "agreement_pct": round(float(np.nan_to_num(f["agreement"][last])) * 100, 1),
              "adx": round(float(np.nan_to_num(f["adx"][last])), 2),
              "efficiency": round(float(np.nan_to_num(f["efficiency"][last])), 3),
              "vol_z": round(float(np.nan_to_num(f["vol_z"][last])), 3),
              "daily_vol_pct": round(float(np.nan_to_num(f["daily_vol_pct"][last])), 3),
              "per_horizon": [{"days": cfg["horizons_days"][h],
                               "t": round(float(np.nan_to_num(f["t_per_horizon"][h][last])), 2),
                               "change_pct": round(float(np.nan_to_num(
                                   f["ret_per_horizon"][h][last])), 2)}
                              for h in range(len(cfg["horizons_days"]))]}
    strength = ("stark" if abs(score) >= cfg["trend_strong_t"] else
                ("moderat" if abs(score) >= cfg["trend_t"] else "schwach"))
    mode = norm_mode(cfg.get("regime_mode", 9))
    t_idx, v_idx = split_id(rid, mode)
    if det is not None:
        from services import regime_reactive as rx
        p = det["probs"][last]
        detail["probabilities"] = {"down": round(float(p[0]) * 100, 1),
                                   "side": round(float(p[1]) * 100, 1),
                                   "up": round(float(p[2]) * 100, 1)}
        warn = rx.early_warning(det, cfg, last, mode)
    else:
        warn = early_warning(f, cfg, last, t_idx, v_idx)
    if warn.get("active") is not None:
        active_days = (last - switch_i) / max(cfg["bars_per_day"], 1e-9)
        warn["min_hold_days"] = round(cfg["min_hold_bars"] / max(cfg["bars_per_day"], 1e-9), 2)
        warn["hold_remaining_days"] = max(round(warn["min_hold_days"] - active_days, 2), 0.0)
    return {"regime": rid, "label": regime_label(rid, mode), "regime_mode": mode,
            "nnfx": nnfx_regime(rid, mode),
            "nnfx_label": NNFX_LABELS[nnfx_regime(rid, mode)],
            "strength": strength,
            "confidence": round(float(conf[last]) * 100, 1),
            "similarities": [], "details": detail,
            "early_warning": warn,
            "reason": _reason_text(rid, detail, strength, mode),
            "last_switch": ts, "active_since_bars": int(last - switch_i)}


def _reason_text(rid: int, d: Dict, strength: str, mode: int = 9) -> str:
    t, _s = split_id(rid, mode)
    dirn = {0: "fallend", 1: "seitwärts", 2: "steigend"}[t]
    hz = " · ".join(f"{h['days']:g}d {h['change_pct']:+.1f}%" for h in d["per_horizon"])
    return (f"Kurs {dirn} ({strength}) · Trend-t {d['trend_t']:+.2f} · "
            f"Konsens {d['agreement_pct']:.0f}% · ADX {d['adx']:.1f} · "
            f"Vola z {d['vol_z']:+.2f} · {hz}")


# ---------------------------------------------------------------- Validierung
def ideal_labels(model: Dict, candles) -> List[Optional[int]]:
    """NUR für die visuelle Prüfung: echte zentrierte Referenz (mit Zukunftssicht!).
    Zeigt, wo die Phasen im Rückblick "wirklich" lagen – niemals für Backtests
    oder Live verwenden. Implementierung: services.regime_truth (zentrierte
    OLS-t-Statistik, symmetrisches Fenster – der alte Shift-Trick verschob nur
    um den halben KÜRZESTEN Horizont und lieferte dadurch verzerrte Phasen)."""
    from services import regime_truth as rt
    cfg = dict(model["config"])
    return rt.centered_labels(candles, cfg, norm_mode(cfg.get("regime_mode", 9)))


def validate_labels(candles, labels, model: Dict) -> Dict:
    """Logische Prüfung der fertigen Regime: Passt das Label zum Kursverlauf?

    - Aufwärts-Regime müssen im Abschnitt netto gestiegen sein (Toleranz),
      Abwärts-Regime gefallen, Seitwärts darf pro Tag nur wenig driften.
    - Vola-Stufe muss zur gemessenen Volatilität des Abschnitts passen.
    Rückgabe enthält Verstöße (max. 25 Beispiele) + Kennzahlen.
    """
    cfg = dict(model["config"])
    mode = norm_mode(cfg.get("regime_mode", 9))
    _h, _l, close, _v = rf.ohlc(candles)
    bpd = cfg["bars_per_day"]
    tol_pct = cfg["validate_tol_pct"]
    tol_t = cfg["validate_tol_t"]
    side_max = cfg["validate_side_max_pct_per_day"]
    f = compute_matrix(candles, cfg)
    segs = segments_from_labels(labels)
    ctx_bars = cfg["horizon_bars"][0]      # Sichtfenster des Klassifikators
    long_bars = cfg["range_bars"]          # langer Kontext (Range vs. echter Trend)
    min_days = cfg["validate_min_segment_days"]
    vmult = cfg["validate_vol_tol_mult"]
    violations, checked, bad_bars, total_bars = [], 0, 0, 0
    dir_ok, dir_checked = 0, 0
    # Kaltstart: solange der längste Horizont noch nicht gefüllt ist, kann der
    # Klassifikator die Richtung noch nicht kennen (kein Lookahead). Abschnitte,
    # die komplett in dieser Aufwärmphase liegen, werden nicht als Verstoß
    # gezählt – sonst würde die Prüfung physikalisch Unmögliches verlangen.
    warm = int(cfg.get("warmup_bars") or 0)
    warmup_bars_skipped, warmup_segments = 0, 0
    for (s, e, rid) in segs:
        bars = e - s
        if e <= warm:
            warmup_bars_skipped += bars
            warmup_segments += 1
            continue
        total_bars += bars
        if bars / max(bpd, 1e-9) < min_days:
            continue
        checked += 1
        t_idx, v_idx = split_id(rid, mode)
        net = (float(close[e - 1]) / max(float(close[s]), rf.EPS) - 1.0) * 100.0
        days = bars / max(bpd, 1e-9)
        per_day = net / max(days, 1e-9)
        _sl, tseg, _r2 = rf.ols_stats(np.log(np.maximum(close[s:e], rf.EPS)), bars)
        t_seg = float(np.nan_to_num(tseg[-1]))
        vzs = f["vol_z"][s:e]
        vz = float(np.nanmean(vzs)) if np.isfinite(vzs).any() else 0.0
        dv = f["daily_vol_pct"][s:e]
        dvol = float(np.nanmean(dv)) if np.isfinite(dv).any() else 1.0
        # Toleranz: kurze Abschnitte dürfen gegen den Trend "atmen" (Vola-skaliert)
        tol = max(tol_pct, vmult * dvol * math.sqrt(max(days, 1.0)))
        c0 = max(s - ctx_bars, 0)
        ctx_net = (float(close[e - 1]) / max(float(close[c0]), rf.EPS) - 1.0) * 100.0
        l0 = max(s - long_bars, 0)
        long_net = (float(close[e - 1]) / max(float(close[l0]), rf.EPS) - 1.0) * 100.0
        long_days = (e - l0) / max(bpd, 1e-9)
        long_per_day = long_net / max(long_days, 1e-9)
        problems = []
        if t_idx == 2 and net < -tol and ctx_net < -tol and t_seg < -tol_t \
                and long_net < 0:
            problems.append(f"Label steigend, tatsächlich {net:+.2f}% im Abschnitt / "
                            f"{ctx_net:+.2f}% im Sichtfenster (t {t_seg:+.2f})")
        if t_idx == 0 and net > tol and ctx_net > tol and t_seg > tol_t \
                and long_net > 0:
            problems.append(f"Label fallend, tatsächlich {net:+.2f}% im Abschnitt / "
                            f"{ctx_net:+.2f}% im Sichtfenster (t {t_seg:+.2f})")
        # Seitwärts ist nur dann falsch, wenn der Kurs AUCH im langen Kontext
        # klar in dieselbe Richtung läuft (sonst ist es eine normale Schwingung
        # innerhalb einer Range).
        if t_idx == 1 and abs(per_day) > side_max \
                and abs(t_seg) > cfg["validate_side_t"] and abs(net) > tol \
                and abs(long_per_day) > side_max / 2 \
                and (long_per_day > 0) == (per_day > 0):
            problems.append(f"Label seitwärts, tatsächlich {per_day:+.2f}%/Tag "
                            f"(t {t_seg:+.2f}, langer Kontext {long_per_day:+.2f}%/Tag)")
        if t_idx != 1:
            dir_checked += 1
            if not problems:
                dir_ok += 1
        # Volatilitätsstufen gibt es nur im 9er-Modus
        if mode == 9 and v_idx == 0 and vz > cfg["vol_low_z"] + 1.0:
            problems.append(f"Label niedrige Vola, gemessen z {vz:+.2f}")
        if mode == 9 and v_idx == 2 and vz < cfg["vol_high_z"] - 1.0:
            problems.append(f"Label hohe Vola, gemessen z {vz:+.2f}")
        # 5er-Modus: "stark" muss auch messbar stärker sein – gemessen wie in
        # der Stärke-Achse: Netto-Bewegung pro Tag relativ zur Tagesvola.
        if mode == 5 and t_idx != 1:
            dv = f["daily_vol_pct"][s:e]
            dv = dv[np.isfinite(dv)]
            v = float(np.mean(dv)) if len(dv) else 2.0
            speed = abs(per_day) / max(v, 1e-9)
            enter = float(cfg.get("strong_speed_ratio") or 0.35)
            if v_idx == 1 and speed < 0.5 * enter:
                problems.append(
                    f"Label starker Trend, Tempo nur {speed:.2f}x Tagesvola")
        if problems:
            bad_bars += bars
            if len(violations) < 25:
                violations.append({
                    "from_ts": int(candles[s]["timestamp"]),
                    "to_ts": int(candles[min(e, len(candles) - 1)]["timestamp"]),
                    "regime": rid, "label": regime_label(rid, mode),
                    "days": round(days, 1), "net_pct": round(net, 2),
                    "context_net_pct": round(ctx_net, 2),
                    "pct_per_day": round(per_day, 3), "t": round(t_seg, 2),
                    "vol_z": round(vz, 2), "problems": problems})
    trend_segs = sum(1 for (_s, _e, r) in segs if split_id(r, mode)[0] != 1)
    return {"segments": len(segs), "checked": checked,
            "warmup_segments": warmup_segments,
            "warmup_bars_skipped": warmup_bars_skipped,
            "trend_segments": trend_segs, "trend_checked": dir_checked,
            "violations": violations, "violation_count": len(violations),
            "violation_bars_pct": round(bad_bars / max(total_bars, 1) * 100, 2),
            "direction_accuracy_pct": (round(dir_ok / dir_checked * 100, 1)
                                       if dir_checked else None),
            "regimes_seen": sorted({int(r) for (_s, _e, r) in segs}),
            "avg_segment_days": (round(sum(e - s for (s, e, _r) in segs)
                                       / max(len(segs), 1) / max(bpd, 1e-9), 2)
                                 if segs else 0.0),
            "switches": max(len(segs) - 1, 0),
            "pass_threshold_pct": VALIDATE_PASS_PCT,
            "passed": bad_bars / max(total_bars, 1) * 100 <= VALIDATE_PASS_PCT}


def agreement_with_ideal(labels, ideal, mode: int = 9) -> Dict:
    """Übereinstimmung der Live-Erkennung mit der (nur zur Prüfung erlaubten)
    Rückblick-Sicht – misst, wie schnell/genau die Erkennung ist."""
    n = min(len(labels), len(ideal))
    both = [(labels[i], ideal[i]) for i in range(n)
            if labels[i] is not None and ideal[i] is not None]
    if not both:
        return {"bars": 0}
    same = sum(1 for a, b in both if a == b)
    same_dir = sum(1 for a, b in both
                   if split_id(a, mode)[0] == split_id(b, mode)[0])
    return {"bars": len(both),
            "exact_pct": round(same / len(both) * 100, 1),
            "direction_pct": round(same_dir / len(both) * 100, 1)}


def engine_defaults(mode: int = None) -> Dict:
    m = norm_mode(mode if mode is not None else DEFAULT_REGIME_MODE)
    return {"config": {k: v for k, v in DEFAULT_CONFIG.items()},
            "meta": [{"key": k, "label": lbl, "help": hlp,
                      **CONFIG_GROUPS.get(k, {"group": "Allgemein",
                                              "detectors": None})}
                     for k, lbl, hlp in CONFIG_META],
            "regime_modes": [{"mode": mm, "count": mm,
                              "taxonomy": taxonomy(mm)} for mm in REGIME_MODES],
            "adapt_profiles": [{"key": k, "label": v["label"]}
                               for k, v in ADAPT_PROFILES.items()],
            "default_regime_mode": DEFAULT_REGIME_MODE,
            "taxonomy": taxonomy(m),
            "nnfx_labels": NNFX_LABELS}


def summarize(model: Dict) -> str:
    cfg = model.get("config") or {}
    prof = cfg.get("adapt_applied")
    if str(cfg.get("detector") or "reactive") == "kombi":
        return (f"Engine v2 · {norm_mode(cfg.get('regime_mode', 9))}-Regime-Modus · "
                f"Kombi-Detektor (EMA {cfg.get('kombi_ema_days', 14):g}d · Schwelle "
                f"{cfg.get('kombi_thr', 0.18):g}×Vola · Fenster "
                f"{cfg.get('kombi_slope_days', 5):g}d · Dominanz "
                f"{cfg.get('kombi_dominance_days', 3):g}d)"
                f"{f' (Profil {prof})' if prof else ''} · "
                f"{len(model.get('regimes') or [])} Regime aktiv")
    if str(cfg.get("detector") or "reactive") != "regression":
        return (f"Engine v2 · {norm_mode(cfg.get('regime_mode', 9))}-Regime-Modus · "
                f"Umkehrpunkt-Erkennung (reaktiv) · Schwelle "
                f"{cfg.get('rev_atr_mult', 3.0):g}×ATR · Persistenz "
                f"{cfg.get('persist_candles', 3)} Kerzen"
                f"{f' (Profil {prof})' if prof else ''} · "
                f"{len(model.get('regimes') or [])} Regime aktiv")
    hz = ", ".join(f"{h:g}d" for h in cfg.get("horizons_days") or [])
    return (f"Engine v2 · {norm_mode(cfg.get('regime_mode', 9))}-Regime-Modus · "
            f"Horizonte {hz}{f' (Profil {prof})' if prof else ''} · "
            f"Trend-Schwelle t={cfg.get('trend_t')} · ADX≥{cfg.get('adx_min')} · "
            f"{len(model.get('regimes') or [])} Regime aktiv")
