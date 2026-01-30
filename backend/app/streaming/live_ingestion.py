"""Live data ingestion from Databento stream with MBP-1 schema

This module handles real-time market data streaming using:
- MBP-1: Top-of-book quotes for DOM imbalance, delta, and OHLCV

Data is aggregated into ohlcv_ticks table and pushed via WebSocket.
Raw MBP-1 data is archived to DBN files for backtesting.
"""
import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
import databento as db
import polars as pl


def normalize_symbol(symbol: str) -> str:
    """Normalize symbol to root form for database storage

    Handles multiple formats:
    - "MNQH26" -> "MNQ" (strips contract month code H/M/U/Z + 2 digits)
    - "MNQH6" -> "MNQ" (strips short format: H/M/U/Z + 1 digit)
    - "MNQ.FUT" -> "MNQ" (strips .FUT suffix)
    - "MNQ.c.0" -> "MNQ" (strips .c.0 continuous suffix)
    - "MNQ" -> "MNQ" (unchanged)

    Args:
        symbol: Raw symbol from config or Databento

    Returns:
        Normalized root symbol (e.g., "MNQ")
    """
    if not symbol:
        return symbol
    # First strip any .suffix (e.g., .FUT, .c.0)
    base = symbol.split('.')[0]
    # Then strip contract month code (H/M/U/Z followed by 1-2 digits at end)
    return re.sub(r'[HMUZ]\d{1,2}$', '', base)

from app.features.order_flow import OrderFlowCalculator
from app.classifiers.regime import RegimeClassifier
from app.data.storage import DuckDBStorage
from app.streaming.live_cache import get_cache
from app.api.websocket import get_manager as get_ws_manager
from config import get_config, get_secrets, get_databento_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LiveDataIngestion:
    """Handles real-time streaming from Databento with MBP-1

    Subscribes to MBP-1 schema for:
    - Top-of-book quotes (best bid/ask)
    - DOM imbalance calculation
    - Delta from quote size changes
    - OHLCV from mid-price

    Data is aggregated into ohlcv_ticks table and pushed via WebSocket.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        dataset: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        db_path: Optional[str] = None,
        flush_interval_seconds: Optional[float] = None,
        on_bar_update: Optional[Callable] = None,
        on_bar_close: Optional[Callable] = None,
        on_signal: Optional[Callable] = None,
    ):
        # Load config
        config = get_config()
        streaming_config = config.streaming
        secrets = get_secrets()
        db_config = get_databento_config()

        self.api_key = api_key or secrets.api_key
        # Load dataset and symbols from databento_config.yaml (preferred) or fall back to app config
        db_streaming = db_config.get('streaming', {})
        self.dataset = dataset or db_streaming.get('dataset', streaming_config.dataset)
        self.symbols = symbols or db_streaming.get('symbols', streaming_config.default_symbols)
        self.timeframes = timeframes or streaming_config.default_timeframes
        self.db_path = db_path or db_config['database'].main_db
        self.flush_interval = flush_interval_seconds or streaming_config.flush_interval_seconds
        self._dom_smoothing = streaming_config.dom_smoothing_factor
        self._max_buffer = streaming_config.max_buffer_size

        # Callbacks for WebSocket push (use provided or default to WebSocket manager)
        self._ws_manager = get_ws_manager()
        self.on_bar_update = on_bar_update or self._default_bar_update
        self.on_bar_close = on_bar_close or self._default_bar_close
        self.on_signal = on_signal or self._default_signal

        # Calculators - MBP-1 uses 1 level
        self.quote_calculator = OrderFlowCalculator(levels=1)
        self.classifier = RegimeClassifier()
        self.cache = get_cache()

        # DBN archive directory
        self.archive_dir = Path(db_config['database'].archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._current_dbn_path: Optional[Path] = None
        self._current_dbn_date: Optional[str] = None

        # Current bars in memory (one per timeframe per symbol)
        self.current_bars: Dict[str, Dict[str, Optional[Dict]]] = {}
        for tf in self.timeframes:
            self.current_bars[tf] = {}
            for symbol in self.symbols:
                self.current_bars[tf][symbol] = None

        # Rolling CVD windows per timeframe (from config)
        self.cvd_windows = getattr(config.regime, 'cvd_windows', {
            "5M": 288, "15M": 96, "1H": 24, "4H": 30, "1D": 5
        })

        # Rolling delta buffers for CVD calculation (deque per timeframe/symbol)
        self.delta_buffers: Dict[str, Dict[str, deque]] = {}
        for tf in self.timeframes:
            window_size = self.cvd_windows.get(tf, 100)
            self.delta_buffers[tf] = {s: deque(maxlen=window_size) for s in self.symbols}

        # Track previous quote for delta calculation
        self.prev_quotes: Dict[str, Dict[str, Any]] = {s: {} for s in self.symbols}

        # Track last completed bar's close for spike filtering across bar boundaries
        self.last_bar_close: Dict[str, Dict[str, Optional[float]]] = {}
        for tf in self.timeframes:
            self.last_bar_close[tf] = {s: None for s in self.symbols}

        # Instrument ID to symbol mapping (populated from SymbolMappingMsg)
        self.instrument_id_to_symbol: Dict[int, str] = {}

        # Initialize last_bar_close from database for spike filtering on first tick
        self._init_last_bar_close_from_db()

        logger.info(f"LiveDataIngestion initialized for {self.symbols}")
        logger.info(f"  Timeframes: {self.timeframes}")
        logger.info(f"  Schema: mbp-1")
        logger.info(f"  DB path: {self.db_path}")

    def _init_last_bar_close_from_db(self):
        """Load last bar close prices from database for spike filtering

        This ensures the first tick after startup has a reference price
        to filter against, preventing spikes at service restart.
        """
        try:
            with DuckDBStorage(db_path=self.db_path) as storage:
                for tf in self.timeframes:
                    for symbol in self.symbols:
                        # Query for normalized symbol (MNQ, not MNQH26 or MNQ.FUT)
                        db_symbol = normalize_symbol(symbol)
                        query = f"""
                            SELECT close FROM ohlcv_ticks
                            WHERE symbol = '{db_symbol}' AND timeframe = '{tf}'
                            ORDER BY timestamp DESC LIMIT 1
                        """
                        result = storage.conn.execute(query).fetchone()
                        if result:
                            self.last_bar_close[tf][symbol] = result[0]
                            logger.info(f"Loaded last close for {tf}:{symbol} = {result[0]:.2f}")
        except Exception as e:
            logger.warning(f"Could not load last bar close from DB: {e}")

    async def _default_bar_update(self, timeframe: str, symbol: str, bar: dict):
        """Default callback: push bar update via WebSocket"""
        # Normalize symbol for WebSocket (consistent with database: MNQ not MNQ.c.0)
        ws_symbol = normalize_symbol(symbol)
        await self._ws_manager.send_bar_update(timeframe, ws_symbol, bar)

    async def _default_bar_close(self, timeframe: str, symbol: str, bar: dict):
        """Default callback: push bar close via WebSocket"""
        # Normalize symbol for WebSocket (consistent with database: MNQ not MNQ.c.0)
        ws_symbol = normalize_symbol(symbol)
        await self._ws_manager.send_bar_close(timeframe, ws_symbol, bar)

    async def _default_signal(self, timeframe: str, symbol: str, signal: dict):
        """Default callback: push signal via WebSocket"""
        await self._ws_manager.send_signal(timeframe, symbol, signal)

    def get_timeframe_interval(self, timeframe: str) -> timedelta:
        """Convert timeframe string to timedelta"""
        mapping = {
            "1M": timedelta(minutes=1),
            "5M": timedelta(minutes=5),
            "15M": timedelta(minutes=15),
            "1H": timedelta(hours=1),
            "4H": timedelta(hours=4),
            "1D": timedelta(days=1),
        }
        return mapping.get(timeframe, timedelta(minutes=1))

    def truncate_timestamp(self, ts: datetime, timeframe: str) -> datetime:
        """Truncate timestamp to timeframe boundary"""
        if timeframe == "1M":
            return ts.replace(second=0, microsecond=0)
        elif timeframe == "5M":
            return ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        elif timeframe == "15M":
            return ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
        elif timeframe == "1H":
            return ts.replace(minute=0, second=0, microsecond=0)
        elif timeframe == "4H":
            return ts.replace(hour=(ts.hour // 4) * 4, minute=0, second=0, microsecond=0)
        elif timeframe == "1D":
            return ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return ts

    def extract_quote(self, record) -> dict:
        """Extract quote data from MBP-1 record"""
        # MBP-1 has a single level
        levels = record.levels

        quote = {
            'ts_event': record.ts_event,
            'instrument_id': record.instrument_id,
        }

        if levels and len(levels) > 0:
            level = levels[0]
            quote['bid_px_00'] = level.bid_px / 1_000_000_000.0
            quote['bid_sz_00'] = level.bid_sz
            quote['ask_px_00'] = level.ask_px / 1_000_000_000.0
            quote['ask_sz_00'] = level.ask_sz
        else:
            quote['bid_px_00'] = 0.0
            quote['bid_sz_00'] = 0
            quote['ask_px_00'] = 0.0
            quote['ask_sz_00'] = 0

        return quote

    def extract_trade(self, record) -> dict:
        """Extract trade data from Trades record"""
        trade = {
            'ts_event': record.ts_event,
            'instrument_id': record.instrument_id,
            'price': record.price / 1_000_000_000.0,
            'size': record.size,
            'side': record.side,  # 'A' = ask (buy aggressor), 'B' = bid (sell aggressor)
        }
        return trade

    def calculate_delta_from_quote(self, quote: dict, prev_quote: dict) -> int:
        """Calculate delta from quote size changes

        Delta is inferred from changes in bid/ask sizes:
        - Decrease in ask size = buy aggression (positive delta)
        - Decrease in bid size = sell aggression (negative delta)
        """
        if not prev_quote:
            return 0

        bid_change = quote.get('bid_sz_00', 0) - prev_quote.get('bid_sz_00', 0)
        ask_change = quote.get('ask_sz_00', 0) - prev_quote.get('ask_sz_00', 0)

        # If ask size decreased, someone bought (positive delta)
        # If bid size decreased, someone sold (negative delta)
        delta = 0
        if ask_change < 0:
            delta += abs(ask_change)  # Buy aggression
        if bid_change < 0:
            delta -= abs(bid_change)  # Sell aggression

        return delta

    def update_bar_with_quote(self, bar: Optional[dict], quote: dict, delta: int, bar_timestamp: datetime, last_close: Optional[float] = None) -> Optional[dict]:
        """Update an existing bar with a new MBP-1 quote

        Returns None if quote is invalid (bad spread, price out of range)

        Args:
            bar: Current bar to update (None if starting new bar)
            quote: MBP-1 quote data
            delta: Calculated delta from quote change
            bar_timestamp: Timestamp for the bar
            last_close: Previous bar's close price for spike filtering when starting new bar
        """
        bid_px = quote.get('bid_px_00', 0)
        ask_px = quote.get('ask_px_00', 0)

        # Skip invalid quotes
        if bid_px <= 0 or ask_px <= 0:
            return bar

        mid_price = (bid_px + ask_px) / 2
        spread = ask_px - bid_px

        # Filter bad quotes: wide spread (>0.5% of price) or price out of range
        if spread / mid_price > 0.005 or mid_price < 10000 or mid_price > 50000:
            return bar  # Keep existing bar unchanged

        # Filter spike quotes: reject if price moves >1% from reference price
        # Use current bar's close, or previous bar's close if starting new bar
        reference_price = None
        if bar is not None and bar.get('close'):
            reference_price = bar['close']
        elif last_close is not None:
            reference_price = last_close

        if reference_price is not None:
            price_change_pct = abs(mid_price - reference_price) / reference_price
            if price_change_pct > 0.01:  # >1% move in single tick is suspicious
                logger.warning(f"Spike rejected: {mid_price:.2f} vs ref {reference_price:.2f} ({price_change_pct*100:.2f}%)")
                return bar  # Keep existing bar unchanged

        # Calculate DOM imbalance
        total_bid = quote.get('bid_sz_00', 0)
        total_ask = quote.get('ask_sz_00', 0)
        dom_imbalance = total_bid / (total_bid + total_ask) if (total_bid + total_ask) > 0 else 0.5

        if bar is None:
            # Create new bar
            return {
                'timestamp': bar_timestamp,
                'open': mid_price,
                'high': mid_price,
                'low': mid_price,
                'close': mid_price,
                'volume': 1,
                'instant_delta': delta,
                'dom_imbalance': dom_imbalance,
                'total_bid_depth': total_bid,
                'total_ask_depth': total_ask,
                'tick_count': 1,
            }
        else:
            # Update existing bar
            bar['high'] = max(bar['high'], mid_price)
            bar['low'] = min(bar['low'], mid_price)
            bar['close'] = mid_price
            bar['volume'] += 1
            bar['instant_delta'] += delta
            bar['tick_count'] += 1
            # Update DOM imbalance (exponential moving average)
            bar['dom_imbalance'] = self._dom_smoothing * bar['dom_imbalance'] + (1 - self._dom_smoothing) * dom_imbalance
            # Update depth (simple average)
            bar['total_bid_depth'] = (bar['total_bid_depth'] * (bar['tick_count'] - 1) + total_bid) / bar['tick_count']
            bar['total_ask_depth'] = (bar['total_ask_depth'] * (bar['tick_count'] - 1) + total_ask) / bar['tick_count']
            return bar

    async def process_mbp_tick(self, quote: dict, symbol: str):
        """Process an MBP-1 tick and update bars for all timeframes"""
        try:
            # Calculate delta from quote change
            prev_quote = self.prev_quotes.get(symbol, {})
            delta = self.calculate_delta_from_quote(quote, prev_quote)
            self.prev_quotes[symbol] = quote

            # Convert timestamp
            tick_ts = datetime.fromtimestamp(quote['ts_event'] / 1e9)

            # Update bars for each timeframe
            for tf in self.timeframes:
                bar_ts = self.truncate_timestamp(tick_ts, tf)
                current_bar = self.current_bars[tf][symbol]

                # Check if we need a new bar
                if current_bar is None or current_bar['timestamp'] != bar_ts:
                    # Save previous bar if exists (bar closed)
                    if current_bar is not None:
                        # Add completed bar's delta to rolling buffer
                        self.delta_buffers[tf][symbol].append(current_bar['instant_delta'])
                        # Calculate rolling CVD from buffer
                        current_bar['cvd'] = sum(self.delta_buffers[tf][symbol])

                        # Store last close for spike filtering on next bar
                        self.last_bar_close[tf][symbol] = current_bar['close']

                        # Store completed bar
                        await self._store_completed_bar(tf, symbol, current_bar)

                        # Callback for bar close
                        if self.on_bar_close:
                            await self.on_bar_close(tf, symbol, current_bar)

                    # Start new bar (pass last_close for spike filtering)
                    last_close = self.last_bar_close[tf][symbol]
                    current_bar = self.update_bar_with_quote(None, quote, delta, bar_ts, last_close=last_close)
                    self.current_bars[tf][symbol] = current_bar
                else:
                    # Update existing bar
                    current_bar = self.update_bar_with_quote(current_bar, quote, delta, bar_ts)
                    self.current_bars[tf][symbol] = current_bar

                # Skip if bar is None (invalid quote)
                if current_bar is None:
                    continue

                # Update rolling CVD on current bar (historical + current bar's delta)
                current_bar['cvd'] = sum(self.delta_buffers[tf][symbol]) + current_bar['instant_delta']

                # Update cache
                self.cache.update_bar(tf, symbol, current_bar)

                # Callback for bar update
                if self.on_bar_update:
                    await self.on_bar_update(tf, symbol, current_bar)

        except Exception as e:
            logger.error(f"Error processing MBP tick: {e}", exc_info=True)

    async def _store_completed_bar(self, timeframe: str, symbol: str, bar: dict):
        """Store a completed bar to the database"""
        try:
            # Normalize symbol for storage (MNQH26/MNQ.FUT -> MNQ for consistency with historical)
            db_symbol = normalize_symbol(symbol)

            with DuckDBStorage(db_path=self.db_path) as storage:
                df = pl.DataFrame([{
                    'timestamp': bar['timestamp'],
                    'symbol': db_symbol,
                    'timeframe': timeframe,
                    'open': bar['open'],
                    'high': bar['high'],
                    'low': bar['low'],
                    'close': bar['close'],
                    'volume': bar['volume'],
                    'instant_delta': bar['instant_delta'],
                    'dom_imbalance': bar['dom_imbalance'],
                    'total_bid_depth': bar['total_bid_depth'],
                    'total_ask_depth': bar['total_ask_depth'],
                    'cvd': bar['cvd'],
                }])
                storage.insert_ohlcv_ticks(df, symbol=db_symbol, timeframe=timeframe)

            # Classify regime
            regime_type, confidence, key_signal = self.classifier.classify(
                dom_imbalance=bar['dom_imbalance'],
                cvd=bar['cvd'],
                price=bar['close'],
                vwap=bar['close'],  # Using close as VWAP proxy
            )

            # Update regime cache
            # Handle both enum and string return types from classifier
            regime_str = regime_type.value if hasattr(regime_type, 'value') else str(regime_type)
            self.cache.update_regime(timeframe, symbol, {
                'timeframe': timeframe,
                'symbol': symbol,
                'regime': regime_str,
                'confidence': confidence,
                'key_signal': key_signal,
                'dom_imbalance': bar['dom_imbalance'],
                'delta': bar['instant_delta'],
                'timestamp': bar['timestamp']
            })

        except Exception as e:
            logger.error(f"Error storing completed bar: {e}", exc_info=True)

    def get_dbn_path(self) -> Path:
        """Get the current compressed DBN archive file path (rotates daily)

        Uses .dbn.zst (zstd compression) for minimal disk footprint.
        If file exists, adds a timestamp suffix to create a new file.
        """
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        if self._current_dbn_date != today:
            self._current_dbn_date = today
            base_path = self.archive_dir / f"mbp1_{today}.dbn.zst"

            # If file exists, add timestamp suffix to avoid collision
            if base_path.exists():
                timestamp = datetime.now(timezone.utc).strftime('%H%M%S')
                self._current_dbn_path = self.archive_dir / f"mbp1_{today}_{timestamp}.dbn.zst"
                logger.info(f"Existing archive found, using: {self._current_dbn_path}")
            else:
                self._current_dbn_path = base_path
                logger.info(f"DBN archive file (zstd compressed): {self._current_dbn_path}")

        return self._current_dbn_path

    async def start_streaming(self):
        """Start consuming live market data from MBP-1 schema

        Raw MBP-1 data is automatically archived to daily DBN files
        using Databento's native streaming. Records are also processed
        in real-time to update ohlcv_ticks and push via WebSocket.
        """
        if not self.api_key:
            raise ValueError("Databento API key not configured. Set DATABENTO_API_KEY or update secrets.yaml")

        logger.info("=" * 60)
        logger.info("Starting live data ingestion (MBP-1)...")
        logger.info(f"  Symbols: {self.symbols}")
        logger.info(f"  Timeframes: {self.timeframes}")
        logger.info(f"  Archive dir: {self.archive_dir}")
        logger.info("=" * 60)

        client = db.Live(key=self.api_key)

        try:
            # Add DBN file stream for archiving raw data
            dbn_path = self.get_dbn_path()
            client.add_stream(str(dbn_path))
            logger.info(f"Archiving to DBN: {dbn_path}")

            # Subscribe to MBP-1 (top-of-book quotes)
            # Get stype_in from config (raw_symbol for specific contracts, parent for continuous)
            db_config = get_databento_config()
            stype_in = db_config.get('streaming', {}).get('stype_in', 'raw_symbol')
            client.subscribe(
                dataset=self.dataset,
                schema="mbp-1",
                symbols=self.symbols,
                stype_in=stype_in,
            )
            logger.info(f"Subscribed to MBP-1 with stype_in={stype_in}")

            tick_count = 0
            last_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

            async for record in client:
                record_type = type(record).__name__

                if record_type == "SymbolMappingMsg":
                    # Map instrument_id to config symbol
                    instrument_id = record.instrument_id
                    raw_symbol = record.stype_in_symbol
                    # Match by normalized root symbol (MNQH6 -> MNQ matches MNQ.c.0 -> MNQ)
                    raw_root = normalize_symbol(raw_symbol)
                    config_symbol = None
                    for s in self.symbols:
                        if normalize_symbol(s) == raw_root:
                            config_symbol = s
                            break
                    if config_symbol:
                        self.instrument_id_to_symbol[instrument_id] = config_symbol
                        logger.info(f"Symbol mapping: instrument_id={instrument_id} -> {raw_symbol} (root: {raw_root}, config: {config_symbol})")
                    else:
                        logger.warning(f"Unknown symbol from Databento: {raw_symbol} (root: {raw_root}), instrument_id={instrument_id}")

                elif record_type == "MBP1Msg":
                    # Quote update - process and update bars
                    quote = self.extract_quote(record)
                    # Look up symbol from instrument_id
                    instrument_id = record.instrument_id
                    symbol = self.instrument_id_to_symbol.get(instrument_id)
                    if symbol is None:
                        logger.warning(f"Unknown instrument_id: {instrument_id}, skipping tick")
                        continue
                    await self.process_mbp_tick(quote, symbol=symbol)
                    tick_count += 1

                # Check for daily file rotation
                current_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if current_date != last_date:
                    # Rotate to new daily DBN file
                    new_dbn_path = self.get_dbn_path()
                    client.add_stream(str(new_dbn_path))
                    logger.info(f"Rotated DBN archive to: {new_dbn_path}")
                    last_date = current_date

                # Log progress
                if tick_count % 10000 == 0:
                    logger.info(f"Processed {tick_count:,} ticks")

        except KeyboardInterrupt:
            logger.info("\nShutting down...")
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            raise
        finally:
            if hasattr(client, 'stop'):
                client.stop()
            logger.info("Stream closed")


async def main():
    """Entry point for live streaming service"""
    # Config is loaded automatically from secrets.yaml and databento_config.yaml
    ingestion = LiveDataIngestion()
    await ingestion.start_streaming()


if __name__ == "__main__":
    asyncio.run(main())
