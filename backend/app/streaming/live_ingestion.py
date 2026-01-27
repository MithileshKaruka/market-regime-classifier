"""Live data ingestion from Databento stream with MBP-1 + Trades schemas

This module handles real-time market data streaming using:
- MBP-1: Top-of-book quotes for DOM imbalance and spread
- Trades: Individual trade executions for accurate CVD/delta

These schemas are available on Databento's personal plan for live streaming.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import databento as db
import polars as pl

from app.features.order_flow import OrderFlowCalculator
from app.features.trade_flow import TradeFlowCalculator, merge_quotes_and_trades
from app.classifiers.regime import RegimeClassifier
from app.data.storage import DuckDBStorage
from app.streaming.live_cache import get_cache
from config import get_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LiveDataIngestion:
    """Handles real-time streaming from Databento with MBP-1 + Trades

    Subscribes to two schemas:
    - mbp-1: Top-of-book quotes (best bid/ask)
    - trades: Individual trade executions

    Combines both to calculate:
    - DOM imbalance from quotes
    - True CVD/delta from trade aggressor side
    - OHLCV from trade prices
    """

    def __init__(
        self,
        api_key: str,
        dataset: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        db_path: str = "/data/live.duckdb",
        flush_interval_seconds: Optional[float] = None
    ):
        # Load defaults from config
        config = get_config()
        streaming_config = config.streaming

        self.api_key = api_key
        self.dataset = dataset or streaming_config.dataset
        self.symbols = symbols or streaming_config.default_symbols
        self.timeframes = timeframes or streaming_config.default_timeframes
        self.db_path = db_path
        self.flush_interval = flush_interval_seconds or streaming_config.flush_interval_seconds
        self._dom_smoothing = streaming_config.dom_smoothing_factor

        # Calculators - MBP-1 uses 1 level
        self.quote_calculator = OrderFlowCalculator(levels=1)
        self.trade_calculator = TradeFlowCalculator()
        self.classifier = RegimeClassifier()
        self.cache = get_cache()

        # Separate buffers for quotes and trades
        self.quote_buffer = []
        self.trade_buffer = []
        self.last_flush_time = datetime.utcnow()

        # Current bars in memory (one per timeframe per symbol)
        self.current_bars: Dict[str, Dict[str, Optional[Dict]]] = {}
        for tf in self.timeframes:
            self.current_bars[tf] = {}
            for symbol in self.symbols:
                self.current_bars[tf][symbol] = None

        # Track latest quote for each symbol (for combining with trades)
        self.latest_quotes: Dict[str, Dict[str, Any]] = {s: {} for s in self.symbols}

        logger.info(f"LiveDataIngestion initialized for {self.symbols}")
        logger.info(f"  Timeframes: {self.timeframes}")
        logger.info(f"  Schemas: mbp-1 (quotes) + trades")

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

    def update_bar_with_trade(self, bar: Optional[dict], trade: dict, quote: dict) -> dict:
        """Update an existing bar with a new trade"""
        price = trade['price']
        size = trade['size']
        signed_size = size if trade['side'] == 'A' else -size

        # Get DOM imbalance from latest quote
        dom_imbalance = 0.5
        if quote:
            total_bid = quote.get('bid_sz_00', 0)
            total_ask = quote.get('ask_sz_00', 0)
            if total_bid + total_ask > 0:
                dom_imbalance = total_bid / (total_bid + total_ask)

        if bar is None:
            # Create new bar
            return {
                'timestamp': trade['bar_timestamp'],
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': size,
                'dom_imbalance': dom_imbalance,
                'delta': signed_size,
                'vwap': price,
                'trade_count': 1,
                'price_volume_sum': price * size,
            }
        else:
            # Update existing bar
            bar['high'] = max(bar['high'], price)
            bar['low'] = min(bar['low'], price)
            bar['close'] = price
            bar['volume'] += size
            bar['delta'] += signed_size
            bar['trade_count'] += 1
            bar['price_volume_sum'] += price * size
            bar['vwap'] = bar['price_volume_sum'] / bar['volume']
            # Update DOM imbalance (exponential moving average using config smoothing factor)
            bar['dom_imbalance'] = self._dom_smoothing * bar['dom_imbalance'] + (1 - self._dom_smoothing) * dom_imbalance
            return bar

    async def process_quote(self, quote: dict, symbol: str):
        """Process a quote update - just store latest for combining with trades"""
        self.latest_quotes[symbol] = quote
        self.quote_buffer.append(quote)

    async def process_trade(self, trade: dict, symbol: str):
        """Process a trade and update bars"""
        try:
            # Get latest quote for this symbol
            latest_quote = self.latest_quotes.get(symbol, {})

            # Convert timestamp
            trade_ts = datetime.fromtimestamp(trade['ts_event'] / 1e9)

            # Update bars for each timeframe
            for tf in self.timeframes:
                bar_ts = self.truncate_timestamp(trade_ts, tf)
                trade['bar_timestamp'] = bar_ts

                current_bar = self.current_bars[tf][symbol]

                # Check if we need a new bar
                if current_bar is None or current_bar['timestamp'] != bar_ts:
                    # Save previous bar if exists
                    if current_bar is not None:
                        # Classify regime for completed bar
                        regime = self.classifier.classify_single(
                            dom_imbalance=current_bar['dom_imbalance'],
                            delta=current_bar['delta'],
                            vwap=current_bar['vwap'],
                            price=current_bar['close']
                        )

                        # Update cache
                        self.cache.update_bar(tf, symbol, current_bar)
                        self.cache.update_regime(tf, symbol, {
                            'timeframe': tf,
                            'symbol': symbol,
                            'regime': regime['regime'],
                            'confidence': regime['confidence'],
                            'key_signal': regime['key_signal'],
                            'dom_imbalance': current_bar['dom_imbalance'],
                            'delta': current_bar['delta'],
                            'timestamp': current_bar['timestamp']
                        })

                    # Start new bar
                    current_bar = self.update_bar_with_trade(None, trade, latest_quote)
                    self.current_bars[tf][symbol] = current_bar
                else:
                    # Update existing bar
                    current_bar = self.update_bar_with_trade(current_bar, trade, latest_quote)
                    self.current_bars[tf][symbol] = current_bar

                    # Update cache with current bar
                    self.cache.update_bar(tf, symbol, current_bar)

                    # Classify and cache regime
                    regime = self.classifier.classify_single(
                        dom_imbalance=current_bar['dom_imbalance'],
                        delta=current_bar['delta'],
                        vwap=current_bar['vwap'],
                        price=current_bar['close']
                    )
                    self.cache.update_regime(tf, symbol, {
                        'timeframe': tf,
                        'symbol': symbol,
                        'regime': regime['regime'],
                        'confidence': regime['confidence'],
                        'key_signal': regime['key_signal'],
                        'dom_imbalance': current_bar['dom_imbalance'],
                        'delta': current_bar['delta'],
                        'timestamp': current_bar['timestamp']
                    })

            # Add to buffer for DB flush
            self.trade_buffer.append(trade)

        except Exception as e:
            logger.error(f"Error processing trade: {e}", exc_info=True)

    async def flush_to_database(self):
        """Flush buffered data to DuckDB"""
        if len(self.trade_buffer) == 0:
            return

        try:
            logger.info(f"Flushing {len(self.trade_buffer)} trades to database...")

            # Convert trade buffer to DataFrame
            df_trades = pl.DataFrame(self.trade_buffer)

            # Calculate trade features
            df_trades = self.trade_calculator.calculate_all_features(df_trades)

            # Convert quote buffer if available
            df_quotes = None
            if self.quote_buffer:
                df_quotes = pl.DataFrame(self.quote_buffer)
                df_quotes = self.quote_calculator.calculate_all_features(df_quotes)

            # Store for each timeframe
            with DuckDBStorage(db_path=self.db_path) as storage:
                for tf in self.timeframes:
                    df_tf = self.trade_calculator.resample_to_timeframe(df_trades, tf)

                    if df_quotes is not None and len(df_quotes) > 0:
                        df_quotes_tf = self.quote_calculator.resample_to_timeframe(df_quotes, tf)
                        df_tf = merge_quotes_and_trades(df_quotes_tf, df_tf, tf)

                    if len(df_tf) > 0:
                        df_tf = df_tf.with_columns([
                            pl.lit(tf).alias("timeframe")
                        ])

                        # Classify regimes
                        df_classified = self.classifier.classify_dataframe(df_tf)

                        # Store in DuckDB
                        storage.insert_order_book_data(df_tf, symbol="MNQ", timeframe=tf)
                        storage.insert_regime_data(df_classified, symbol="MNQ")

            logger.info(f"✓ Flushed to database")

            # Clear buffers
            self.trade_buffer.clear()
            self.quote_buffer.clear()

        except Exception as e:
            logger.error(f"Error flushing to database: {e}", exc_info=True)

    async def check_and_flush_database(self):
        """Check if it's time to flush to database"""
        now = datetime.utcnow()
        elapsed = (now - self.last_flush_time).total_seconds()

        if elapsed >= self.flush_interval:
            await self.flush_to_database()
            self.last_flush_time = now

    async def start_streaming(self):
        """Start consuming live market data from both schemas"""
        logger.info("="*60)
        logger.info("Starting live data ingestion (MBP-1 + Trades)...")
        logger.info(f"  Symbols: {self.symbols}")
        logger.info(f"  Timeframes: {self.timeframes}")
        logger.info(f"  DB flush interval: {self.flush_interval}s")
        logger.info("="*60)

        client = db.Live(key=self.api_key)

        try:
            # Subscribe to MBP-1 (top-of-book quotes)
            client.subscribe(
                dataset=self.dataset,
                schema="mbp-1",
                symbols=self.symbols,
                stype_in="parent",
            )
            logger.info("✓ Subscribed to MBP-1 (quotes)")

            # Subscribe to Trades
            client.subscribe(
                dataset=self.dataset,
                schema="trades",
                symbols=self.symbols,
                stype_in="parent",
            )
            logger.info("✓ Subscribed to Trades")

            quote_count = 0
            trade_count = 0

            async for record in client:
                record_type = type(record).__name__

                if record_type == "MBP1Msg":
                    # Quote update
                    quote = self.extract_quote(record)
                    await self.process_quote(quote, symbol="MNQ")
                    quote_count += 1

                elif record_type == "TradeMsg":
                    # Trade execution
                    trade = self.extract_trade(record)
                    await self.process_trade(trade, symbol="MNQ")
                    trade_count += 1

                # Periodic flush to database
                await self.check_and_flush_database()

                # Log progress
                if (quote_count + trade_count) % 1000 == 0:
                    logger.info(
                        f"Processed {quote_count:,} quotes, {trade_count:,} trades, "
                        f"buffer: {len(self.trade_buffer)}"
                    )

        except KeyboardInterrupt:
            logger.info("\nShutting down...")
            await self.flush_to_database()
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            raise
        finally:
            client.close()
            logger.info("Stream closed")


async def main():
    """Entry point for live streaming service"""
    import os

    api_key = os.getenv("DATABENTO_API_KEY")
    if not api_key:
        raise ValueError("DATABENTO_API_KEY environment variable not set")

    db_path = os.getenv("LIVE_DB_PATH", "data/live.duckdb")

    ingestion = LiveDataIngestion(
        api_key=api_key,
        db_path=db_path,
        flush_interval_seconds=1.0
    )

    await ingestion.start_streaming()


if __name__ == "__main__":
    asyncio.run(main())
