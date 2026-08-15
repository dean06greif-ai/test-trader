"""
Free crypto news via public RSS feeds (no API key needed).
Cached for 10 minutes. Used by the AI trading engine.
"""
import aiohttp
import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from typing import Dict, List

logger = logging.getLogger(__name__)

FEEDS = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt", "https://decrypt.co/feed"),
]

CACHE_TTL = 600  # 10 min


def _parse_rss(xml_text: str, source: str, limit: int = 15) -> List[Dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "source": source,
                "published": (item.findtext("pubDate") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
            })
            if len(items) >= limit:
                break
    except Exception as e:
        logger.warning(f"RSS parse failed for {source}: {e}")
    return items


class NewsFeed:
    def __init__(self):
        self._cache: List[Dict] = []
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def get_headlines(self, limit: int = 20) -> List[Dict]:
        async with self._lock:
            if self._cache and (time.time() - self._cached_at) < CACHE_TTL:
                return self._cache[:limit]
            headlines: List[Dict] = []
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    for source, url in FEEDS:
                        try:
                            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                                if resp.status != 200:
                                    continue
                                text = await resp.text()
                            headlines.extend(_parse_rss(text, source, limit=12))
                        except Exception as e:
                            logger.warning(f"News fetch failed ({source}): {e}")
            except Exception as e:
                logger.warning(f"News session failed: {e}")
            if headlines:
                self._cache = headlines
                self._cached_at = time.time()
            return self._cache[:limit]


news_feed = NewsFeed()
