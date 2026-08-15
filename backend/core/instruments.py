"""Asset-Universum der Plattform – EINE Quelle der Wahrheit.

Jedes handelbare/analysierbare Instrument wird hier genau einmal beschrieben:

  * ``bitunix``      Kontraktname für ECHTE Orders (None = kein Bitunix-Kontrakt,
                     d.h. nur Scanner/Backtester/Optimizer/Paper)
  * ``live_source``  Quelle der Live-Kurse ("exchange" = Fallback-Kette
                     Bitunix->Binance->OKX, "bitunix", "binance", "yahoo")
  * ``hist_source``  Quelle der 1m-Historie ("binance", "bitunix", "yahoo")
  * ``max_hist_days`` realistisch verfügbare Historie (nur informativ für die UI)

Neue Assets werden AUSSCHLIESSLICH hier ergänzt – Scanner, Backtester,
Optimizer, Sidebar und Trade-Layer leiten ihre Listen daraus ab.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

# ---- Gruppen (Reiter in der Sidebar) ----
GROUP_CRYPTO = "TOP 10 COINS"
GROUP_RESOURCES = "RESOURCES"
GROUP_INDICES = "INDICES"
GROUP_FOREX = "FOREX"

GROUP_ORDER = [GROUP_CRYPTO, GROUP_RESOURCES, GROUP_INDICES, GROUP_FOREX]


@dataclass(frozen=True)
class Instrument:
    symbol: str                     # App-interner Schlüssel (überall verwendet)
    name: str                       # Anzeigename
    group: str
    bitunix: Optional[str]          # Bitunix-Kontrakt für Live-Orders
    live_source: str                # exchange | bitunix | binance | yahoo
    live_ref: str                   # Symbol/Ticker bei der Live-Quelle
    hist_source: str                # binance | bitunix | yahoo
    hist_ref: str                   # Symbol/Ticker bei der Historien-Quelle
    max_hist_days: int = 5500

    @property
    def tradable(self) -> bool:
        """Echte Live-Orders nur mit vorhandenem Bitunix-Kontrakt."""
        return bool(self.bitunix)


def _crypto(symbol: str, name: str) -> Instrument:
    return Instrument(symbol=symbol, name=name, group=GROUP_CRYPTO, bitunix=symbol,
                      live_source="exchange", live_ref=symbol,
                      hist_source="binance", hist_ref=symbol)


def _resource(symbol: str, name: str, yahoo: str, bitunix: str, days: int) -> Instrument:
    """Rohstoffe: Live-Kurse wie bisher von Yahoo, Historie von Bitunix."""
    return Instrument(symbol=symbol, name=name, group=GROUP_RESOURCES, bitunix=bitunix,
                      live_source="yahoo", live_ref=yahoo,
                      hist_source="bitunix", hist_ref=bitunix, max_hist_days=days)


def _index(symbol: str, name: str, days: int) -> Instrument:
    """US-Indizes/Index-ETFs: bei Bitunix als USDT-Perp gelistet."""
    return Instrument(symbol=symbol, name=name, group=GROUP_INDICES, bitunix=symbol,
                      live_source="bitunix", live_ref=symbol,
                      hist_source="bitunix", hist_ref=symbol, max_hist_days=days)


def _forex(symbol: str, name: str) -> Instrument:
    """Forex: Bitunix listet keine FX-Kontrakte -> Kurse/Historie von Yahoo,
    handelbar im Scanner/Backtester/Optimizer und im Paper-Modus."""
    return Instrument(symbol=symbol, name=name, group=GROUP_FOREX, bitunix=None,
                      live_source="yahoo", live_ref=f"{symbol}=X",
                      hist_source="yahoo", hist_ref=f"{symbol}=X", max_hist_days=30)


INSTRUMENTS: List[Instrument] = [
    # ---- Krypto ----
    _crypto("BTCUSDT", "Bitcoin"),
    _crypto("ETHUSDT", "Ethereum"),
    _crypto("BNBUSDT", "BNB"),
    _crypto("SOLUSDT", "Solana"),
    _crypto("XRPUSDT", "XRP"),
    _crypto("ADAUSDT", "Cardano"),
    _crypto("DOGEUSDT", "Dogecoin"),
    _crypto("AVAXUSDT", "Avalanche"),
    _crypto("DOTUSDT", "Polkadot"),
    _crypto("POLUSDT", "Polygon"),
    # ---- Rohstoffe ----
    _resource("GOLD", "Gold", "GC=F", "XAUUSDT", 150),
    _resource("SILVER", "Silver", "SI=F", "XAGUSDT", 180),
    _resource("OIL", "Oil (WTI)", "CL=F", "CLUSDT", 110),
    # ---- Indizes ----
    _index("QQQUSDT", "Nasdaq 100 (QQQ)", 110),
    _index("SPYUSDT", "S&P 500 (SPY)", 110),
    # ---- Forex ----
    _forex("EURUSD", "Euro / US-Dollar"),
    _forex("GBPUSD", "Pfund / US-Dollar"),
    _forex("USDJPY", "US-Dollar / Yen"),
    _forex("AUDUSD", "Aussie / US-Dollar"),
    _forex("USDCAD", "US-Dollar / Kanada-Dollar"),
    _forex("USDCHF", "US-Dollar / Franken"),
    _forex("NZDUSD", "Kiwi / US-Dollar"),
]

BY_SYMBOL: Dict[str, Instrument] = {i.symbol: i for i in INSTRUMENTS}

ALL_SYMBOLS: List[str] = [i.symbol for i in INSTRUMENTS]
TOP_10_COINS: List[str] = [i.symbol for i in INSTRUMENTS if i.group == GROUP_CRYPTO]
TRADABLE_SYMBOLS: List[str] = [i.symbol for i in INSTRUMENTS if i.tradable]
# Alles mit Historien-Quelle ist im Backtester/Optimizer wählbar.
BACKTEST_SYMBOLS: List[str] = list(ALL_SYMBOLS)

# ---- Rückwärtskompatible Aliase (bisheriges core.config-API) ----
OTHER_INSTRUMENTS = [{"symbol": i.symbol, "yahoo": i.live_ref, "name": i.name}
                     for i in INSTRUMENTS if i.live_source == "yahoo"]
OTHER_YAHOO: Dict[str, str] = {i["symbol"]: i["yahoo"] for i in OTHER_INSTRUMENTS}

SYMBOL_MAP: Dict[str, str] = {i.symbol: i.bitunix for i in INSTRUMENTS
                              if i.bitunix and i.bitunix != i.symbol}


def get(symbol: str) -> Optional[Instrument]:
    return BY_SYMBOL.get((symbol or "").upper())


def is_tradable(symbol: str) -> bool:
    inst = get(symbol)
    return bool(inst and inst.tradable)


def groups() -> List[Dict]:
    """Gruppen für die Sidebar/UI – Reihenfolge wie in GROUP_ORDER."""
    out = []
    for name in GROUP_ORDER:
        items = [i for i in INSTRUMENTS if i.group == name]
        if not items:
            continue
        out.append({
            "name": name,
            "symbols": [{"symbol": i.symbol, "name": i.name, "tradable": i.tradable,
                         "max_hist_days": i.max_hist_days} for i in items],
        })
    return out


def history_days_cap(symbol: str, days: int) -> int:
    inst = get(symbol)
    return min(days, inst.max_hist_days) if inst else days


# ---- Live-Kurse: eine Route für alle Anlageklassen ----
async def fetch_live_candles(symbol: str, limit: int = 200,
                             yahoo_range: str = "1d") -> List[Dict]:
    """1m-Kerzen für den Scanner – wählt die Quelle anhand des Instruments."""
    from core.state import feed  # lazy: vermeidet Import-Zyklus
    inst = get(symbol)
    if inst is None:
        return await feed.fetch(symbol, limit)
    if inst.live_source == "yahoo":
        candles = await feed.fetch_commodity(inst.live_ref, yahoo_range)
        if not candles and yahoo_range == "1d":
            # Wochenende/Feiertag: Markt zu -> letzter Handelstag statt leerem Chart
            candles = await feed.fetch_commodity(inst.live_ref, "5d")
        return candles
    if inst.live_source == "exchange":
        return await feed.fetch(inst.live_ref, limit)
    return await feed.fetch_from(inst.live_source, inst.live_ref, limit)
