"""Zentrale Konstanten & Umgebungs-Setup (aus server.py verschoben).

Das Asset-Universum selbst liegt in ``core.instruments`` – hier werden nur die
etablierten Namen re-exportiert, damit bestehender Code unverändert läuft.
"""
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from core.instruments import (  # noqa: F401  (öffentliches Re-Export-API)
    ALL_SYMBOLS,
    BACKTEST_SYMBOLS,
    OTHER_INSTRUMENTS,
    OTHER_YAHOO,
    TOP_10_COINS,
    TRADABLE_SYMBOLS,
)

load_dotenv()

BERLIN = ZoneInfo("Europe/Berlin")

POLL_INTERVAL = 12
