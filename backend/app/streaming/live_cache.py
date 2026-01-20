"""In-memory cache for real-time market data"""
import logging
from datetime import datetime
from typing import Dict, Optional
import threading

logger = logging.getLogger(__name__)


class LiveBarCache:
    """Thread-safe in-memory cache for current bars across all timeframes"""

    def __init__(self):
        self._lock = threading.RLock()
        # Structure: {timeframe: {symbol: bar_dict}}
        self._bars: Dict[str, Dict[str, dict]] = {}
        # Structure: {timeframe: {symbol: regime_dict}}
        self._regimes: Dict[str, Dict[str, dict]] = {}

        logger.info("LiveBarCache initialized")

    def update_bar(self, timeframe: str, symbol: str, bar: dict):
        """Update the current bar for a timeframe/symbol"""
        with self._lock:
            if timeframe not in self._bars:
                self._bars[timeframe] = {}
            self._bars[timeframe][symbol] = bar.copy()

    def update_regime(self, timeframe: str, symbol: str, regime: dict):
        """Update the current regime classification"""
        with self._lock:
            if timeframe not in self._regimes:
                self._regimes[timeframe] = {}
            self._regimes[timeframe][symbol] = regime.copy()

    def get_bar(self, timeframe: str, symbol: str) -> Optional[dict]:
        """Get current bar for timeframe/symbol"""
        with self._lock:
            if timeframe in self._bars and symbol in self._bars[timeframe]:
                return self._bars[timeframe][symbol].copy()
            return None

    def get_regime(self, timeframe: str, symbol: str) -> Optional[dict]:
        """Get current regime for timeframe/symbol"""
        with self._lock:
            if timeframe in self._regimes and symbol in self._regimes[timeframe]:
                return self._regimes[timeframe][symbol].copy()
            return None

    def get_all_regimes(self, symbol: str, timeframes: list[str]) -> list[dict]:
        """Get regimes for all requested timeframes"""
        with self._lock:
            results = []
            for tf in timeframes:
                regime = self.get_regime(tf, symbol)
                if regime:
                    results.append(regime)
            return results

    def clear(self):
        """Clear all cached data"""
        with self._lock:
            self._bars.clear()
            self._regimes.clear()
            logger.info("Cache cleared")


# Global singleton instance
_cache = LiveBarCache()


def get_cache() -> LiveBarCache:
    """Get the global cache instance"""
    return _cache
