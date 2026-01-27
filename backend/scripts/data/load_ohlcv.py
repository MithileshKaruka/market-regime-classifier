"""
Load OHLCV data from DBN file and store in DuckDB
This is for chart display - clean OHLCV candlestick data
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import databento as db
import polars as pl
from datetime import datetime
from app.data.storage import DuckDBStorage

# Configuration
DATA_FILE = Path("C:/Users/jthlbg2/market-regime-classifier/backend/data/glbx-mdp3-20210116-20260115.ohlcv-1m.dbn.zst")
SYMBOL = "MNQ"


def load_ohlcv_from_dbn(file_path: Path) -> pl.DataFrame:
    """
    Load OHLCV data from DBN file and build continuous contract

    Args:
        file_path: Path to DBN file

    Returns:
        Polars DataFrame with OHLCV data
    """
    print(f"\n[1/4] Loading OHLCV data from {file_path.name}...")
    print(f"       File size: {file_path.stat().st_size / 1024 / 1024 / 1024:.2f} GB")

    # Load DBN file
    store = db.DBNStore.from_file(str(file_path))

    # Convert to DataFrame using databento's method
    print(f"[2/4] Converting to DataFrame...")
    df = store.to_df()

    print(f"       Records loaded: {len(df):,}")
    print(f"       Columns: {list(df.columns)}")

    # OHLCV schema uses index as timestamp, not ts_event column
    # The index is a DatetimeIndex representing the bar start time
    if hasattr(df, 'index') and isinstance(df.index, type(df.index)):
        print(f"       Using DataFrame index as timestamp")
        # Reset index to make timestamp a column
        df = df.reset_index()
        # Rename index column to ts_event for consistency
        if 'index' in df.columns:
            df = df.rename(columns={'index': 'ts_event'})
        print(f"       Date range: {df['ts_event'].min()} to {df['ts_event'].max()}")

    # Convert to Polars (if pandas)
    if not isinstance(df, pl.DataFrame):
        df = pl.from_pandas(df)

    # Build continuous contract using daily volume leader
    # This prevents per-bar contract switching during rollover
    print(f"       Building continuous contract from daily volume leader...")

    # Filter out spread contracts (contain '-')
    df = df.filter(~pl.col("symbol").str.contains("-"))
    print(f"       Outright contracts only: {len(df):,} records")

    # Add date column for daily aggregation
    df = df.with_columns([
        pl.col("ts_event").dt.truncate("1d").alias("date")
    ])

    # Calculate daily volume per symbol
    daily_volume = df.group_by(["date", "symbol"]).agg([
        pl.col("volume").sum().alias("daily_volume")
    ])

    # Find the front month (highest daily volume) for each day
    front_month = daily_volume.group_by("date").agg([
        pl.all().sort_by("daily_volume", descending=True).first()
    ]).select(["date", "symbol"]).rename({"symbol": "front_symbol"})

    print(f"       Identified front month for {len(front_month)} days")

    # Join back to get only front month bars
    df = df.join(front_month, on="date", how="left")
    df = df.filter(pl.col("symbol") == pl.col("front_symbol"))

    # Show contract switches
    front_month_sorted = front_month.sort("date")
    contracts = front_month_sorted["front_symbol"].to_list()
    dates = front_month_sorted["date"].to_list()

    current = contracts[0]
    print(f"       Contract switches:")
    for d, c in zip(dates, contracts):
        if c != current:
            print(f"         {d.date()}: {current} -> {c}")
            current = c

    # Select final columns and drop helper columns
    df_result = df.select([
        "ts_event", "open", "high", "low", "close", "volume"
    ])

    # Sort by timestamp for time series operations
    df_result = df_result.sort("ts_event")

    print(f"       Continuous contract bars: {len(df_result):,}")

    return df_result


def prepare_ohlcv_data(df: pl.DataFrame) -> pl.DataFrame:
    """
    Prepare OHLCV data for storage

    Args:
        df: Raw OHLCV DataFrame from DBN

    Returns:
        Cleaned DataFrame with proper column names
    """
    print(f"[3/4] Preparing OHLCV data...")

    # Ensure timestamp is datetime
    if df["ts_event"].dtype != pl.Datetime:
        df = df.with_columns([
            pl.col("ts_event").cast(pl.Datetime("ns")).alias("ts_event")
        ])

    # Standard OHLCV columns (databento format)
    # Expected columns: ts_event, open, high, low, close, volume
    required_cols = ["ts_event", "open", "high", "low", "close", "volume"]

    # Verify all columns exist
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Select only what we need
    df = df.select(required_cols)

    print(f"       Data prepared: {len(df):,} bars (before filtering)")

    # Filter out settlement artifacts
    # Settlement/rollover creates corrupted OHLC values (negative or ~$225)
    # Also filter bars with abnormally large wicks
    bars_before = len(df)
    df = df.filter(
        # Valid price range (filter negative values and extreme outliers)
        (pl.col("open") > 0) & (pl.col("open") >= 10000) & (pl.col("open") <= 30000) &
        (pl.col("high") > 0) & (pl.col("high") >= 10000) & (pl.col("high") <= 30000) &
        (pl.col("low") > 0) & (pl.col("low") >= 10000) & (pl.col("low") <= 30000) &
        (pl.col("close") > 0) & (pl.col("close") >= 10000) & (pl.col("close") <= 30000) &
        # Filter abnormal wicks (settlement artifacts create huge ranges)
        # 1-minute bars should not have >2% high-low range
        # (Normal MNQ 1-minute bars rarely exceed 1%)
        (((pl.col("high") - pl.col("low")) / pl.col("close")) < 0.02) &
        # Valid OHLC relationships
        (pl.col("low") <= pl.col("open")) &
        (pl.col("low") <= pl.col("close")) &
        (pl.col("high") >= pl.col("open")) &
        (pl.col("high") >= pl.col("close"))
    )
    bars_after = len(df)
    bars_filtered = bars_before - bars_after

    print(f"       Filtered out {bars_filtered:,} corrupted bars (settlement artifacts)")
    print(f"       Clean bars: {bars_after:,}")
    print(f"       First timestamp: {df['ts_event'][0]}")
    print(f"       Last timestamp: {df['ts_event'][-1]}")

    return df


def resample_to_timeframe(df: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    """
    Resample 1-minute OHLCV to higher timeframe

    Args:
        df: 1-minute OHLCV data
        timeframe: Target timeframe (5M, 15M, 1H, 4H, 1D)

    Returns:
        Resampled DataFrame
    """
    # Map timeframe to polars duration
    timeframe_map = {
        "1M": "1m",
        "5M": "5m",
        "15M": "15m",
        "1H": "1h",
        "4H": "4h",
        "1D": "1d"
    }

    if timeframe not in timeframe_map:
        raise ValueError(f"Invalid timeframe: {timeframe}")

    duration = timeframe_map[timeframe]

    print(f"       Resampling to {timeframe}...")

    # Resample OHLCV
    df_resampled = df.group_by_dynamic(
        "ts_event",
        every=duration,
        closed="left",
        label="left"
    ).agg([
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
    ])

    # Remove any null rows (incomplete periods at the end)
    df_resampled = df_resampled.drop_nulls()

    print(f"       Resampled to {len(df_resampled):,} bars")

    return df_resampled


def store_ohlcv_data(df: pl.DataFrame, timeframe: str, symbol: str = SYMBOL):
    """
    Store OHLCV data in DuckDB

    Args:
        df: OHLCV DataFrame
        timeframe: Timeframe label
        symbol: Trading symbol
    """
    print(f"[4/4] Storing {timeframe} data in DuckDB...")

    with DuckDBStorage() as storage:
        # Add placeholder columns for order flow metrics (will be calculated later from trades data)
        df_with_metrics = df.with_columns([
            pl.lit(0.5).alias("dom_imbalance"),  # Placeholder
            pl.lit(0.0).alias("cvd"),  # Will calculate from trades
            pl.col("close").alias("vwap"),  # Placeholder, will calculate properly later
        ])

        storage.insert_order_book_data(df_with_metrics, symbol=symbol, timeframe=timeframe)

    print(f"       Stored {len(df):,} {timeframe} bars")


def main():
    """Main execution flow"""
    print("=" * 70)
    print("  OHLCV Data Loader - Market Regime Classifier")
    print("=" * 70)
    print(f"\nSymbol: {SYMBOL}")
    print(f"Data file: {DATA_FILE.name}")
    print(f"Timeframes: 1M, 5M, 15M, 1H, 4H, 1D")

    if not DATA_FILE.exists():
        print(f"\n[ERROR] Data file not found: {DATA_FILE}")
        return

    try:
        # Load raw 1-minute OHLCV data
        df_1m = load_ohlcv_from_dbn(DATA_FILE)
        df_1m = prepare_ohlcv_data(df_1m)

        # Store 1-minute data
        store_ohlcv_data(df_1m, "1M")

        # Resample and store higher timeframes
        timeframes = ["5M", "15M", "1H", "4H", "1D"]

        print(f"\n{'='*70}")
        print(f"  Resampling to higher timeframes")
        print(f"{'='*70}")

        for tf in timeframes:
            print(f"\n--- {tf} Timeframe ---")
            df_tf = resample_to_timeframe(df_1m, tf)
            store_ohlcv_data(df_tf, tf)

        print(f"\n{'='*70}")
        print(f"  Data Loading Complete!")
        print(f"{'='*70}")
        print(f"\nLoaded timeframes:")
        print(f"  - 1M:  {len(df_1m):,} bars")

        for tf in timeframes:
            df_tf = resample_to_timeframe(df_1m, tf)
            print(f"  - {tf}:  {len(df_tf):,} bars")

        print(f"\nNext steps:")
        print(f"1. Start backend: uvicorn app.main:app --reload")
        print(f"2. Open chart at: http://localhost:8000")
        print(f"3. Later: Add trades data for CVD calculation")

    except Exception as e:
        print(f"\n[ERROR] Failed to load OHLCV data: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
