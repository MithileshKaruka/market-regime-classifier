"""DuckDB storage layer for efficient querying"""
import logging
from pathlib import Path
from typing import Optional
import duckdb
import polars as pl

logger = logging.getLogger(__name__)


class DuckDBStorage:
    """Efficient storage and querying with DuckDB"""

    def __init__(self, db_path: str = None):
        """Initialize DuckDB connection

        Args:
            db_path: Path to DuckDB database file (default: backend/data/market_data.duckdb)
        """
        if db_path is None:
            # Use absolute path based on this file's location
            # storage.py is in backend/app/data/, so go up 2 levels to backend/
            backend_dir = Path(__file__).parent.parent.parent
            db_path = backend_dir / "data" / "market_data.duckdb"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True, parents=True)
        self.conn = duckdb.connect(str(self.db_path))
        # logger.info(f"DuckDB initialized at: {self.db_path}")

        # Create tables if they don't exist
        self._initialize_tables()

    def _initialize_tables(self):
        """Create necessary tables"""
        # OHLCV data with order flow metrics
        # Note: MBP-10 fields removed (bid/ask prices/sizes, spread, etc.)
        # DOM imbalance will come from real-time MBP-10 only
        # CVD will be calculated from trades data
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS order_book (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                timeframe VARCHAR,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                dom_imbalance DOUBLE,
                cvd DOUBLE,
                vwap DOUBLE,
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)

        # Regime classifications
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS regimes (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                timeframe VARCHAR,
                regime VARCHAR,
                confidence DOUBLE,
                key_signal VARCHAR,
                dom_imbalance DOUBLE,
                cvd DOUBLE,
                vwap DOUBLE,
                price DOUBLE,
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)

        # MBP tick data for order flow analysis (supports MBP-1 and MBP-10)
        # Stores aggregated metrics from raw MBP snapshots
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS mbp_ticks (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                mid_price DOUBLE,
                bid_price DOUBLE,
                ask_price DOUBLE,
                spread DOUBLE,
                bid_size INT,
                ask_size INT,
                total_bid_depth INT,
                total_ask_depth INT,
                dom_imbalance DOUBLE,
                delta DOUBLE,
                cvd DOUBLE,
                PRIMARY KEY (timestamp, symbol)
            )
        """)

        # Trades table for accurate CVD calculation from trade aggressor side
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                price DOUBLE,
                size INTEGER,
                side VARCHAR,
                signed_size INTEGER,
                delta BIGINT,
                PRIMARY KEY (timestamp, symbol, price, size)
            )
        """)

        # logger.info("Tables initialized successfully")

    def insert_order_book_data(
        self,
        df: pl.DataFrame,
        symbol: str = "MNQ",
        timeframe: str = "1M"
    ):
        """Insert order book data into database

        Args:
            df: Polars DataFrame with order book data
            symbol: Trading symbol
            timeframe: Data timeframe
        """
        logger.info(f"Inserting {len(df)} order book records")

        # Determine timestamp column name
        ts_col = "ts_event" if "ts_event" in df.columns else "timestamp"

        # Select and rename columns to match table schema
        df_insert = df.select([
            pl.col(ts_col).alias("timestamp"),
            pl.lit(symbol).alias("symbol"),
            pl.lit(timeframe).alias("timeframe"),
            pl.col("open"),
            pl.col("high"),
            pl.col("low"),
            pl.col("close"),
            pl.col("volume"),
            pl.col("dom_imbalance"),
            pl.col("cvd"),
            pl.col("vwap"),
        ])

        # Insert into DuckDB
        self.conn.execute("""
            INSERT OR REPLACE INTO order_book
            SELECT * FROM df_insert
        """)

        self.conn.commit()
        logger.info("Order book data inserted successfully")

    def insert_regime_data(
        self,
        df: pl.DataFrame,
        symbol: str = "MNQ"
    ):
        """Insert regime classification data

        Args:
            df: Polars DataFrame with regime data
            symbol: Trading symbol
        """
        logger.info(f"Inserting {len(df)} regime records")

        # Determine timestamp column name
        ts_col = "ts_event" if "ts_event" in df.columns else "timestamp"

        # Determine timeframe column (if exists)
        timeframe_val = df["timeframe"][0] if "timeframe" in df.columns else "1H"

        # Select and rename columns to match table schema
        df_insert = df.select([
            pl.col(ts_col).alias("timestamp"),
            pl.lit(symbol).alias("symbol"),
            pl.lit(timeframe_val).alias("timeframe"),
            pl.col("regime"),
            pl.col("confidence"),
            pl.col("key_signal"),
            pl.col("dom_imbalance"),
            pl.col("cvd"),
            pl.col("vwap"),
            pl.col("close").alias("price"),
        ])

        # Insert into DuckDB
        self.conn.execute("""
            INSERT OR REPLACE INTO regimes
            SELECT * FROM df_insert
        """)

        self.conn.commit()
        logger.info("Regime data inserted successfully")

    def get_latest_regimes(
        self,
        symbol: str = "MNQ",
        timeframes: Optional[list[str]] = None,
        use_cache: bool = True
    ) -> pl.DataFrame:
        """Get latest regime classification for each timeframe

        Args:
            symbol: Trading symbol
            timeframes: List of timeframes to query (default: all)
            use_cache: If True, try to get from in-memory cache first

        Returns:
            DataFrame with latest regime for each timeframe
        """
        if timeframes is None:
            timeframes = ["5M", "15M", "1H", "4H", "1D"]

        # Try cache first (for real-time data)
        if use_cache:
            try:
                from app.streaming.live_cache import get_cache
                cache = get_cache()
                cached_regimes = cache.get_all_regimes(symbol, timeframes)

                if len(cached_regimes) > 0:
                    # Convert to DataFrame
                    df = pl.DataFrame(cached_regimes)
                    logger.info(f"Retrieved {len(df)} regimes from cache")
                    return df
            except Exception as e:
                logger.warning(f"Cache lookup failed, falling back to DB: {e}")

        # Fall back to database
        timeframes_str = ", ".join([f"'{tf}'" for tf in timeframes])

        query = f"""
            SELECT
                timeframe,
                regime,
                confidence,
                key_signal,
                dom_imbalance,
                cvd,
                vwap,
                price,
                timestamp
            FROM regimes
            WHERE symbol = '{symbol}'
                AND timeframe IN ({timeframes_str})
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY timeframe
                ORDER BY timestamp DESC
            ) = 1
            ORDER BY
                CASE timeframe
                    WHEN '5M' THEN 1
                    WHEN '15M' THEN 2
                    WHEN '1H' THEN 3
                    WHEN '4H' THEN 4
                    WHEN '1D' THEN 5
                END
        """

        df = self.conn.execute(query).pl()
        # logger.info(f"Retrieved {len(df)} latest regimes from DB")
        return df

    def get_regime_history(
        self,
        symbol: str = "MNQ",
        timeframe: str = "1H",
        limit: int = 100
    ) -> pl.DataFrame:
        """Get historical regime classifications

        Args:
            symbol: Trading symbol
            timeframe: Timeframe to query
            limit: Number of records to return

        Returns:
            DataFrame with historical regimes
        """
        query = f"""
            SELECT *
            FROM regimes
            WHERE symbol = '{symbol}'
                AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT {limit}
        """

        df = self.conn.execute(query).pl()
        logger.info(f"Retrieved {len(df)} historical regimes")
        return df

    def get_order_flow_metrics(
        self,
        symbol: str = "MNQ",
        timeframe: str = "1H",
        limit: int = 1
    ) -> pl.DataFrame:
        """Get latest order flow metrics

        Args:
            symbol: Trading symbol
            timeframe: Timeframe to query
            limit: Number of records to return

        Returns:
            DataFrame with order flow metrics
        """
        query = f"""
            SELECT *
            FROM order_book
            WHERE symbol = '{symbol}'
                AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT {limit}
        """

        df = self.conn.execute(query).pl()
        return df

    def insert_mbp_ticks(
        self,
        df: pl.DataFrame,
        symbol: str = "MNQ"
    ):
        """Insert MBP-10 tick data into database

        Args:
            df: Polars DataFrame with MBP tick data
            symbol: Trading symbol
        """
        logger.info(f"Inserting {len(df)} MBP tick records")

        # Determine timestamp column name
        ts_col = "ts_event" if "ts_event" in df.columns else "timestamp"

        # Build column selection based on available columns
        columns = [
            pl.col(ts_col).alias("timestamp"),
            pl.lit(symbol).alias("symbol"),
        ]

        # Add available columns with defaults
        optional_cols = {
            "mid_price": 0.0,
            "bid_price": 0.0,
            "ask_price": 0.0,
            "spread": 0.0,
            "bid_size": 0,
            "ask_size": 0,
            "total_bid_depth": 0,
            "total_ask_depth": 0,
            "dom_imbalance": 0.5,
            "delta": 0.0,
            "cvd": 0.0,
        }

        for col, default in optional_cols.items():
            if col in df.columns:
                columns.append(pl.col(col))
            else:
                columns.append(pl.lit(default).alias(col))

        df_insert = df.select(columns)

        # Insert into DuckDB
        self.conn.execute("""
            INSERT OR REPLACE INTO mbp_ticks
            SELECT * FROM df_insert
        """)

        self.conn.commit()
        logger.info("MBP tick data inserted successfully")

    def get_mbp_ticks(
        self,
        symbol: str = "MNQ",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 1000
    ) -> pl.DataFrame:
        """Get MBP tick data for order flow analysis

        Args:
            symbol: Trading symbol
            start_time: Start timestamp (ISO format)
            end_time: End timestamp (ISO format)
            limit: Number of records to return

        Returns:
            DataFrame with MBP tick data
        """
        where_clauses = [f"symbol = '{symbol}'"]

        if start_time:
            where_clauses.append(f"timestamp >= '{start_time}'")
        if end_time:
            where_clauses.append(f"timestamp <= '{end_time}'")

        where_str = " AND ".join(where_clauses)

        query = f"""
            SELECT *
            FROM mbp_ticks
            WHERE {where_str}
            ORDER BY timestamp DESC
            LIMIT {limit}
        """

        df = self.conn.execute(query).pl()
        return df

    def get_mbp_tick_count(self, symbol: str = "MNQ") -> int:
        """Get total count of MBP ticks for a symbol

        Args:
            symbol: Trading symbol

        Returns:
            Total count of ticks
        """
        result = self.conn.execute(f"""
            SELECT COUNT(*) as cnt
            FROM mbp_ticks
            WHERE symbol = '{symbol}'
        """).fetchone()
        return result[0] if result else 0

    def insert_trades(
        self,
        df: pl.DataFrame,
        symbol: str = "MNQ"
    ):
        """Insert trade data into database

        Args:
            df: Polars DataFrame with trade data
            symbol: Trading symbol
        """
        logger.info(f"Inserting {len(df)} trade records")

        # Determine timestamp column name
        ts_col = "ts_event" if "ts_event" in df.columns else "timestamp"

        # Build column selection
        columns = [
            pl.col(ts_col).alias("timestamp"),
            pl.lit(symbol).alias("symbol"),
        ]

        # Required columns
        for col in ["price", "size", "side", "signed_size", "delta"]:
            if col in df.columns:
                columns.append(pl.col(col))
            else:
                # Default values
                if col == "side":
                    columns.append(pl.lit("N").alias(col))
                else:
                    columns.append(pl.lit(0).alias(col))

        df_insert = df.select(columns)

        # Insert into DuckDB
        self.conn.execute("""
            INSERT OR REPLACE INTO trades
            SELECT * FROM df_insert
        """)

        self.conn.commit()
        logger.info("Trade data inserted successfully")

    def get_trades(
        self,
        symbol: str = "MNQ",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 1000
    ) -> pl.DataFrame:
        """Get trade data

        Args:
            symbol: Trading symbol
            start_time: Start timestamp (ISO format)
            end_time: End timestamp (ISO format)
            limit: Number of records to return

        Returns:
            DataFrame with trade data
        """
        where_clauses = [f"symbol = '{symbol}'"]

        if start_time:
            where_clauses.append(f"timestamp >= '{start_time}'")
        if end_time:
            where_clauses.append(f"timestamp <= '{end_time}'")

        where_str = " AND ".join(where_clauses)

        query = f"""
            SELECT *
            FROM trades
            WHERE {where_str}
            ORDER BY timestamp DESC
            LIMIT {limit}
        """

        df = self.conn.execute(query).pl()
        return df

    def get_trade_count(self, symbol: str = "MNQ") -> int:
        """Get total count of trades for a symbol

        Args:
            symbol: Trading symbol

        Returns:
            Total count of trades
        """
        result = self.conn.execute(f"""
            SELECT COUNT(*) as cnt
            FROM trades
            WHERE symbol = '{symbol}'
        """).fetchone()
        return result[0] if result else 0

    def close(self):
        """Close database connection"""
        self.conn.close()
        # logger.info("DuckDB connection closed")

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
