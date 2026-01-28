"""DBN archive loader for backtesting

Loads MBP-1 tick data from DBN archive files and aggregates
into OHLCV bars for signal detection backtesting.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List
import logging

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from config import get_databento_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DBNLoader:
    """Load and aggregate MBP-1 data from DBN archive for backtesting"""

    def __init__(self, archive_dir: Optional[str] = None):
        """Initialize loader

        Args:
            archive_dir: Path to DBN archive directory. Defaults to config.
        """
        if archive_dir:
            self.archive_dir = Path(archive_dir)
        else:
            db_config = get_databento_config()
            self.archive_dir = Path(db_config['database'].archive_dir)

        if not self.archive_dir.exists():
            logger.warning(f"Archive directory not found: {self.archive_dir}")

    def list_available_dates(self, prefix: str = "mbp1") -> List[str]:
        """List available dates in the archive

        Args:
            prefix: File prefix to filter (e.g., "mbp1", "trades")

        Returns:
            List of date strings (YYYY-MM-DD)
        """
        dates = []
        # Use .dbn.zst (zstd compressed) format
        for f in self.archive_dir.glob(f"{prefix}_*.dbn.zst"):
            try:
                # Extract date from filename (e.g., mbp1_2024-01-15.dbn.zst)
                # Remove .dbn.zst suffix to get stem
                stem = f.name.replace('.dbn.zst', '')
                date_str = stem.split('_')[-1]
                datetime.strptime(date_str, '%Y-%m-%d')
                if date_str not in dates:
                    dates.append(date_str)
            except Exception:
                continue

        return sorted(dates)

    def load_dbn_file(self, file_path: Path) -> pl.DataFrame:
        """Load a single DBN file into a Polars DataFrame

        Args:
            file_path: Path to DBN file

        Returns:
            Polars DataFrame with MBP-1 data
        """
        try:
            store = db.DBNStore.from_file(str(file_path))
            df_pandas = store.to_df()

            # Reset index if ts_event is in index
            if hasattr(df_pandas, 'index') and df_pandas.index.name:
                df_pandas = df_pandas.reset_index()

            # Convert to Polars
            df = pl.from_pandas(df_pandas)

            logger.info(f"  Loaded {len(df):,} records from {file_path.name}")
            return df

        except Exception as e:
            logger.error(f"Error loading {file_path}: {e}")
            return pl.DataFrame()

    def load_date_range(
        self,
        start_date: str,
        end_date: str,
        prefix: str = "mbp1"
    ) -> pl.DataFrame:
        """Load data for a date range

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            prefix: File prefix

        Returns:
            Combined DataFrame for the date range
        """
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')

        dfs = []
        current = start

        while current <= end:
            date_str = current.strftime('%Y-%m-%d')

            # Use .dbn.zst (zstd compressed) format
            file_path = self.archive_dir / f"{prefix}_{date_str}.dbn.zst"
            if file_path.exists():
                df = self.load_dbn_file(file_path)
                if len(df) > 0:
                    dfs.append(df)

            current += timedelta(days=1)

        if not dfs:
            logger.warning(f"No data found for {start_date} to {end_date}")
            return pl.DataFrame()

        return pl.concat(dfs)

    def load_recent_days(self, days: int = 7, prefix: str = "mbp1") -> pl.DataFrame:
        """Load data for recent N days

        Args:
            days: Number of days to load
            prefix: File prefix

        Returns:
            Combined DataFrame
        """
        end_date = datetime.utcnow().strftime('%Y-%m-%d')
        start_date = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
        return self.load_date_range(start_date, end_date, prefix)

    def aggregate_to_ohlcv(
        self,
        df: pl.DataFrame,
        timeframe: str = "15M",
        symbol: str = "MNQ"
    ) -> pl.DataFrame:
        """Aggregate MBP-1 ticks to OHLCV bars

        Args:
            df: MBP-1 DataFrame from DBN
            timeframe: Target timeframe (5M, 15M, 1H, 4H, 1D)
            symbol: Symbol to filter

        Returns:
            OHLCV DataFrame with orderflow metrics
        """
        if len(df) == 0:
            return pl.DataFrame()

        # Map timeframe to interval
        timeframe_map = {
            "5M": "5m",
            "15M": "15m",
            "1H": "1h",
            "4H": "4h",
            "1D": "1d",
        }
        interval = timeframe_map.get(timeframe, "15m")

        # Filter by symbol if column exists
        if "symbol" in df.columns:
            df = df.filter(pl.col("symbol").str.contains(symbol))

        # Determine timestamp column (ts_event or index)
        ts_col = "ts_event" if "ts_event" in df.columns else df.columns[0]

        # Ensure timestamp is datetime
        if df[ts_col].dtype != pl.Datetime:
            df = df.with_columns([
                pl.col(ts_col).cast(pl.Datetime("ns")).alias("timestamp")
            ])
        else:
            df = df.with_columns([pl.col(ts_col).alias("timestamp")])

        # Calculate mid price from bid/ask
        # DBN MBP-1 has bid_px_00, ask_px_00, bid_sz_00, ask_sz_00
        if "bid_px_00" in df.columns and "ask_px_00" in df.columns:
            df = df.with_columns([
                ((pl.col("bid_px_00") + pl.col("ask_px_00")) / 2).alias("mid_price"),
                # Calculate delta from size changes
                (pl.col("bid_sz_00") - pl.col("bid_sz_00").shift(1)).fill_null(0).alias("bid_change"),
                (pl.col("ask_sz_00") - pl.col("ask_sz_00").shift(1)).fill_null(0).alias("ask_change"),
            ])
            # Delta: negative ask change = buy, negative bid change = sell
            df = df.with_columns([
                (
                    pl.when(pl.col("ask_change") < 0).then(-pl.col("ask_change")).otherwise(0) -
                    pl.when(pl.col("bid_change") < 0).then(-pl.col("bid_change")).otherwise(0)
                ).alias("delta")
            ])
        else:
            # Fallback if columns not found
            logger.warning("Expected MBP-1 columns not found, using available data")
            df = df.with_columns([
                pl.lit(0.0).alias("mid_price"),
                pl.lit(0).alias("delta"),
            ])

        # Aggregate to OHLCV
        df_ohlcv = df.group_by_dynamic(
            "timestamp",
            every=interval,
            closed="left",
            label="left"
        ).agg([
            pl.col("mid_price").first().alias("open"),
            pl.col("mid_price").max().alias("high"),
            pl.col("mid_price").min().alias("low"),
            pl.col("mid_price").last().alias("close"),
            pl.count().alias("volume"),
            pl.col("delta").sum().alias("instant_delta"),
            # Calculate DOM imbalance if sizes available
            (
                pl.col("bid_sz_00").mean() /
                (pl.col("bid_sz_00").mean() + pl.col("ask_sz_00").mean())
            ).alias("dom_imbalance") if "bid_sz_00" in df.columns else pl.lit(0.5).alias("dom_imbalance"),
        ])

        # Add CVD (cumulative delta)
        df_ohlcv = df_ohlcv.with_columns([
            pl.col("instant_delta").cum_sum().alias("cvd"),
            pl.lit(symbol).alias("symbol"),
            pl.lit(timeframe).alias("timeframe"),
        ])

        # Reorder columns to match ohlcv_ticks schema
        df_ohlcv = df_ohlcv.select([
            "timestamp",
            "symbol",
            "timeframe",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "instant_delta",
            "dom_imbalance",
            "cvd",
        ])

        return df_ohlcv

    def load_for_backtest(
        self,
        start_date: str,
        end_date: str,
        timeframe: str = "15M",
        symbol: str = "MNQ"
    ) -> pl.DataFrame:
        """Load and aggregate data for backtesting

        Convenience method that loads DBN files and aggregates
        to the specified timeframe.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            timeframe: Target timeframe
            symbol: Symbol to filter

        Returns:
            OHLCV DataFrame ready for backtesting
        """
        logger.info(f"Loading backtest data: {start_date} to {end_date}, {timeframe}")

        # Load raw ticks from DBN
        df_ticks = self.load_date_range(start_date, end_date, "mbp1")

        if len(df_ticks) == 0:
            logger.warning("No data loaded")
            return pl.DataFrame()

        logger.info(f"Loaded {len(df_ticks):,} total ticks")

        # Aggregate to OHLCV
        df_ohlcv = self.aggregate_to_ohlcv(df_ticks, timeframe, symbol)

        logger.info(f"Aggregated to {len(df_ohlcv):,} {timeframe} bars")

        return df_ohlcv

    def load_historical_dbn(self, file_path: str, timeframe: str = "15M", symbol: str = "MNQ") -> pl.DataFrame:
        """Load a historical DBN file (from Databento download)

        Use this for large historical downloads from Databento.

        Args:
            file_path: Path to DBN/DBN.ZST file
            timeframe: Target timeframe for aggregation
            symbol: Symbol to filter

        Returns:
            OHLCV DataFrame
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"DBN file not found: {file_path}")

        logger.info(f"Loading historical DBN: {file_path}")
        df = self.load_dbn_file(path)

        if len(df) == 0:
            return pl.DataFrame()

        return self.aggregate_to_ohlcv(df, timeframe, symbol)


def main():
    """Demo/test the DBN loader"""
    loader = DBNLoader()

    # List available dates
    dates = loader.list_available_dates()
    print(f"\nAvailable dates in archive: {len(dates)}")
    if dates:
        print(f"  First: {dates[0]}")
        print(f"  Last: {dates[-1]}")

        # Load and aggregate sample data
        if len(dates) >= 2:
            df = loader.load_for_backtest(
                start_date=dates[0],
                end_date=dates[min(6, len(dates)-1)],
                timeframe="15M"
            )

            if len(df) > 0:
                print(f"\nSample data:")
                print(df.head(10))
    else:
        print("  No DBN files in archive yet")

    # Check for any existing historical DBN files
    archive_dir = loader.archive_dir.parent
    historical_files = list(archive_dir.glob("*.dbn*"))
    if historical_files:
        print(f"\nHistorical DBN files found: {len(historical_files)}")
        for f in historical_files[:5]:
            print(f"  {f.name}")


if __name__ == "__main__":
    main()
