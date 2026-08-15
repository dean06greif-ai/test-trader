"""Geteilter Laufzeit-Zustand: Service-Instanzen + In-Memory-Caches.

`db` wird im Lifespan (server.py) gesetzt und ist danach überall über
`state.db` erreichbar. Alle Objekte hier werden nur verschoben, keine
Logik-Änderung gegenüber der alten server.py.
"""
import asyncio
from typing import Dict, List

from core import config  # noqa: F401  (lädt .env vor den Service-Imports)
from services.market_data import MarketDataFeed
from services.strategy_scanner import StrategyScanner
from services.telegram_bot import TelegramNotifier
from services.bitunix_trade import BitunixTradeClient, AutoTradeManager

scanner = StrategyScanner()
telegram = TelegramNotifier()
feed = MarketDataFeed()
trade_client = BitunixTradeClient()
autotrader = AutoTradeManager(trade_client)

db = None  # AsyncIOMotorDatabase – wird im Lifespan gesetzt

websocket_clients: List = []
open_signal_evals: List[Dict] = []   # in-memory outcome tracking for today's signals
scanner_running = asyncio.Event()

# global admin "kill-switches" (toggles). When ON they act as a regulator that
# temporarily stops the bot from opening new trades / emitting new signals
# without disabling per-coin/strategy configuration.
control_state: Dict[str, bool] = {"trades_paused": False, "signals_paused": False}

# In-memory cache of (strategy_id, symbol) -> enabled. Missing entry defaults
# to True so per-strategy behaviour is unchanged for any combo that has not
# been explicitly toggled off. Persisted in `strategy_coin_toggles`.
strategy_coin_toggles: Dict[tuple, bool] = {}


def toggle_enabled(strategy_id: str, symbol: str) -> bool:
    """Return whether (strategy, coin) auto-trade is enabled. Default True."""
    if not strategy_id or not symbol:
        return True
    return strategy_coin_toggles.get((strategy_id, symbol), True)
