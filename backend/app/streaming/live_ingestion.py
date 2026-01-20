"""Live data ingestion from Databento stream with in-memory caching"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
import databento as db
import polars as pl

from app.features.order_flow import OrderFlowCalculator
from app.classifiers.regime import RegimeClassifier
from app.data.storage import DuckDBStorage
from app.streaming.live_cache import get_cache

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LiveDataIngestion:
    """Handles real-time streaming from Databento with in-memory caching"""

    def __init__(
        self,
        api_key: str,
        dataset: str = "GLBX.MDP3",
        symbols: list[str] = None,
        timeframes: list[str] = None,
        db_path: str = "/data/live.duckdb",
        flush_interval_seconds: float = 1.0
    ):
        self.api_key = api_key
        self.dataset = dataset
        self.symbols = symbols or ["MNQ"]
        self.timeframes = timeframes or ["5M", "15M", "1H", "4H", "1D"]
        self.db_path = db_path
        self.flush_interval = flush_interval_seconds

        self.calculator = OrderFlowCalculator()
        self.classifier = RegimeClassifier()
        self.cache = get_cache()

        # Tick buffer for batch DB writes
        self.tick_buffer = []
        self.last_flush_time = datetime.utcnow()

        # Current bars in memory (one per timeframe per symbol)
        self.current_bars = {}
        for tf in self.timeframes:
            self.current_bars[tf] = {}
            for symbol in self.symbols:
                self.current_bars[tf][symbol] = None

        logger.info(f"LiveDataIngestion initialized for {self.symbols} on timeframes {self.timeframes}")

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

    def extract_tick(self, record) -> dict:
        """Extract tick data from MBP-10 record"""
        levels = record.levels

        tick = {
            'ts_event': record.ts_event,
            'instrument_id': record.instrument_id,
        }

        # Extract 10 levels
        for i in range(10):
            if i < len(levels):
                level = levels[i]
                tick[f'bid_px_{i:02d}'] = level.bid_px / 1_000_000_000.0
                tick[f'bid_sz_{i:02d}'] = level.bid_sz
                tick[f'bid_ct_{i:02d}'] = level.bid_ct
                tick[f'ask_px_{i:02d}'] = level.ask_px / 1_000_000_000.0
                tick[f'ask_sz_{i:02d}'] = level.ask_sz
                tick[f'ask_ct_{i:02d}'] = level.ask_ct
            else:
                tick[f'bid_px_{i:02d}'] = 0.0
                tick[f'bid_sz_{i:02d}'] = 0
                tick[f'bid_ct_{i:02d}'] = 0
                tick[f'ask_px_{i:02d}'] = 0.0
                tick[f'ask_sz_{i:02d}'] = 0
                tick[f'ask_ct_{i:02d}'] = 0

        return tick

    def update_bar_with_tick(self, bar: dict, tick_features: dict) -> dict:
        """Update an existing bar with a new tick"""
        if bar is None:
            # Create new bar
            return {
                'timestamp': tick_features['timestamp'],
                'open': tick_features['mid_price'],
                'high': tick_features['mid_price'],
                'low': tick_features['mid_price'],
                'close': tick_features['mid_price'],
                'volume': tick_features.get('volume', 0),
                'dom_imbalance': tick_features['dom_imbalance'],
                'delta': tick_features['delta'],
                'vwap': tick_features['vwap'],
            }
        else:
            # Update existing bar
            bar['high'] = max(bar['high'], tick_features['mid_price'])
            bar['low'] = min(bar['low'], tick_features['mid_price'])
            bar['close'] = tick_features['mid_price']
            bar['volume'] += tick_features.get('volume', 0)
            # Rolling average for DOM imbalance, delta, vwap
            bar['dom_imbalance'] = (bar['dom_imbalance'] + tick_features['dom_imbalance']) / 2
            bar['delta'] += tick_features['delta']
            bar['vwap'] = (bar['vwap'] + tick_features['vwap']) / 2
            return bar

    async def process_tick(self, tick: dict, symbol: str):
        """Process a single tick and update in-memory bars"""
        try:
            # Convert to DataFrame for feature calculation
            df_tick = pl.DataFrame([tick])

            # Calculate order flow features
            df_tick = self.calculator.calculate_all_features(df_tick)
            tick_features = df_tick.row(0, named=True)

            # Convert ts_event to datetime
            tick_ts = datetime.fromtimestamp(tick_features['ts_event'] / 1e9)

            # Update bars for each timeframe
            for tf in self.timeframes:
                bar_ts = self.truncate_timestamp(tick_ts, tf)

                # Get current bar
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
                    current_bar = self.update_bar_with_tick(None, {
                        'timestamp': bar_ts,
                        'mid_price': tick_features['mid_price'],
                        'volume': 1,
                        'dom_imbalance': tick_features['dom_imbalance'],
                        'delta': tick_features['delta'],
                        'vwap': tick_features['vwap'],
                    })
                    self.current_bars[tf][symbol] = current_bar
                else:
                    # Update existing bar
                    current_bar = self.update_bar_with_tick(current_bar, {
                        'timestamp': bar_ts,
                        'mid_price': tick_features['mid_price'],
                        'volume': 1,
                        'dom_imbalance': tick_features['dom_imbalance'],
                        'delta': tick_features['delta'],
                        'vwap': tick_features['vwap'],
                    })
                    self.current_bars[tf][symbol] = current_bar

                    # Also update cache with current (incomplete) bar
                    self.cache.update_bar(tf, symbol, current_bar)

                    # Classify and cache regime for current bar
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
            self.tick_buffer.append(tick)

        except Exception as e:
            logger.error(f"Error processing tick: {e}", exc_info=True)

    async def flush_to_database(self):
        """Flush buffered ticks to DuckDB (runs in background)"""
        if len(self.tick_buffer) == 0:
            return

        try:
            logger.info(f"Flushing {len(self.tick_buffer)} ticks to database...")

            # Convert buffer to DataFrame
            df = pl.DataFrame(self.tick_buffer)

            # Calculate features
            df = self.calculator.calculate_all_features(df)

            # Store for each timeframe
            with DuckDBStorage(db_path=self.db_path) as storage:
                for tf in self.timeframes:
                    df_tf = self.calculator.resample_to_timeframe(df, tf)

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

            # Clear buffer
            self.tick_buffer.clear()

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
        """Start consuming live market data"""
        logger.info("="*60)
        logger.info("Starting live data ingestion...")
        logger.info(f"  Symbols: {self.symbols}")
        logger.info(f"  Timeframes: {self.timeframes}")
        logger.info(f"  DB flush interval: {self.flush_interval}s")
        logger.info("="*60)

        client = db.Live(key=self.api_key)

        try:
            # Subscribe to MBP-10 data
            client.subscribe(
                dataset=self.dataset,
                schema="mbp-10",
                symbols=self.symbols,
                stype_in="parent",
            )

            logger.info("✓ Subscribed to live stream")

            tick_count = 0

            async for record in client:
                tick = self.extract_tick(record)
                await self.process_tick(tick, symbol="MNQ")

                tick_count += 1

                # Periodic flush to database
                await self.check_and_flush_database()

                # Log progress
                if tick_count % 1000 == 0:
                    logger.info(f"Processed {tick_count:,} ticks, buffer: {len(self.tick_buffer)}")

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
        flush_interval_seconds=1.0  # Flush to DB every 1 second
    )

    await ingestion.start_streaming()


if __name__ == "__main__":
    asyncio.run(main())
