"""
Historical Data Preload Utility

Downloads and loads historical data from Databento for initial system setup:
- OHLCV-1M: 5 years (for price history and backtesting)
- MBP-1: 60 days (for orderflow metrics)

Usage:
    # Estimate cost before downloading
    python scripts/data/preload_historical.py --estimate

    # Download and load all data
    python scripts/data/preload_historical.py --load

    # Download only OHLCV (5 years)
    python scripts/data/preload_historical.py --load --ohlcv-only

    # Download only MBP-1 (60 days)
    python scripts/data/preload_historical.py --load --mbp-only

    # Custom date ranges
    python scripts/data/preload_historical.py --load --ohlcv-start 2020-01-01 --mbp-days 90
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
from app.data.storage import DuckDBStorage
from config import get_secrets

# Default configuration
DEFAULT_OHLCV_YEARS = 5
DEFAULT_MBP_DAYS = 60
DATASET = "GLBX.MDP3"
SYMBOL = "MNQ.FUT"


def get_date_ranges(
    ohlcv_years: int = DEFAULT_OHLCV_YEARS,
    ohlcv_start: Optional[str] = None,
    mbp_days: int = DEFAULT_MBP_DAYS
) -> dict:
    """Calculate date ranges for data download

    Args:
        ohlcv_years: Years of OHLCV data to download
        ohlcv_start: Optional fixed start date for OHLCV
        mbp_days: Days of MBP-1 data to download

    Returns:
        Dict with start/end dates for each schema
    """
    today = datetime.utcnow().date()

    # OHLCV: 5 years back (or custom start)
    if ohlcv_start:
        ohlcv_start_date = datetime.strptime(ohlcv_start, '%Y-%m-%d').date()
    else:
        ohlcv_start_date = today - timedelta(days=ohlcv_years * 365)

    # MBP-1: 60 days back
    mbp_start_date = today - timedelta(days=mbp_days)

    return {
        'ohlcv': {
            'start': ohlcv_start_date.strftime('%Y-%m-%d'),
            'end': today.strftime('%Y-%m-%d'),
            'days': (today - ohlcv_start_date).days,
        },
        'mbp': {
            'start': mbp_start_date.strftime('%Y-%m-%d'),
            'end': today.strftime('%Y-%m-%d'),
            'days': (today - mbp_start_date).days,
        }
    }


def estimate_cost(api_key: str, date_ranges: dict) -> dict:
    """Estimate download cost from Databento

    Args:
        api_key: Databento API key
        date_ranges: Date ranges from get_date_ranges()

    Returns:
        Dict with cost estimates
    """
    print("\n" + "=" * 60)
    print("  Cost Estimation")
    print("=" * 60)

    client = db.Historical(api_key)
    costs = {}

    # OHLCV cost
    print(f"\nOHLCV-1M ({date_ranges['ohlcv']['days']} days)...")
    print(f"  Range: {date_ranges['ohlcv']['start']} to {date_ranges['ohlcv']['end']}")
    try:
        ohlcv_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            schema="ohlcv-1m",
            start=date_ranges['ohlcv']['start'],
            end=date_ranges['ohlcv']['end'],
        )
        costs['ohlcv'] = ohlcv_cost
        print(f"  Estimated cost: ${ohlcv_cost:.2f}")
    except Exception as e:
        print(f"  Error estimating: {e}")
        costs['ohlcv'] = None

    # MBP-1 cost
    print(f"\nMBP-1 ({date_ranges['mbp']['days']} days)...")
    print(f"  Range: {date_ranges['mbp']['start']} to {date_ranges['mbp']['end']}")
    try:
        mbp_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            schema="mbp-1",
            start=date_ranges['mbp']['start'],
            end=date_ranges['mbp']['end'],
        )
        costs['mbp'] = mbp_cost
        print(f"  Estimated cost: ${mbp_cost:.2f}")
    except Exception as e:
        print(f"  Error estimating: {e}")
        costs['mbp'] = None

    # Total
    total = 0
    if costs['ohlcv']:
        total += costs['ohlcv']
    if costs['mbp']:
        total += costs['mbp']

    print(f"\n{'='*60}")
    print(f"  TOTAL ESTIMATED COST: ${total:.2f}")
    print(f"{'='*60}")

    return costs


def download_ohlcv(api_key: str, start: str, end: str, output_dir: Path) -> Optional[Path]:
    """Download OHLCV-1M data from Databento

    Args:
        api_key: Databento API key
        start: Start date (YYYY-MM-DD)
        end: End date (YYYY-MM-DD)
        output_dir: Directory to save file

    Returns:
        Path to downloaded file, or None if failed
    """
    print(f"\nDownloading OHLCV-1M: {start} to {end}...")

    client = db.Historical(api_key)
    output_path = output_dir / f"ohlcv1m_{start}_to_{end}.dbn.zst"

    try:
        client.timeseries.get_range(
            dataset=DATASET,
            symbols=[SYMBOL],
            schema="ohlcv-1m",
            start=start,
            end=end,
            path=str(output_path),
        )

        if output_path.exists() and output_path.stat().st_size > 0:
            size_gb = output_path.stat().st_size / 1024 / 1024 / 1024
            print(f"  Downloaded: {output_path.name} ({size_gb:.2f} GB)")
            return output_path
        else:
            print(f"  No data downloaded")
            return None

    except Exception as e:
        print(f"  Error downloading: {e}")
        return None


def download_mbp(api_key: str, start: str, end: str, output_dir: Path) -> Optional[Path]:
    """Download MBP-1 data from Databento

    Args:
        api_key: Databento API key
        start: Start date (YYYY-MM-DD)
        end: End date (YYYY-MM-DD)
        output_dir: Directory to save file

    Returns:
        Path to downloaded file, or None if failed
    """
    print(f"\nDownloading MBP-1: {start} to {end}...")

    client = db.Historical(api_key)
    output_path = output_dir / f"mbp1_{start}_to_{end}.dbn.zst"

    try:
        client.timeseries.get_range(
            dataset=DATASET,
            symbols=[SYMBOL],
            schema="mbp-1",
            start=start,
            end=end,
            path=str(output_path),
        )

        if output_path.exists() and output_path.stat().st_size > 0:
            size_gb = output_path.stat().st_size / 1024 / 1024 / 1024
            print(f"  Downloaded: {output_path.name} ({size_gb:.2f} GB)")
            return output_path
        else:
            print(f"  No data downloaded")
            return None

    except Exception as e:
        print(f"  Error downloading: {e}")
        return None


def load_ohlcv_file(file_path: Path):
    """Load OHLCV file into database"""
    from scripts.data.load_historical_data import (
        load_ohlcv_from_dbn,
        filter_ohlcv_data,
        resample_to_timeframe,
        ensure_ohlcv_table,
        insert_ohlcv_data,
        TIMEFRAMES,
    )

    print(f"\nLoading OHLCV into database...")

    with DuckDBStorage() as storage:
        ensure_ohlcv_table(storage)

        # Load and process
        df_1m = load_ohlcv_from_dbn(file_path)
        df_1m = filter_ohlcv_data(df_1m)

        print("Inserting OHLCV data...")
        for tf in TIMEFRAMES:
            df_tf = resample_to_timeframe(df_1m, tf)
            insert_ohlcv_data(storage, df_tf, tf)
            print(f"  {tf}: {len(df_tf):,} bars")

        storage.conn.commit()

        # Create index
        print("Creating index...")
        storage.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_ticks_lookup
            ON ohlcv_ticks (symbol, timeframe, timestamp)
        """)
        storage.conn.commit()


def load_mbp_file(file_path: Path):
    """Load MBP file into database"""
    from scripts.data.load_historical_data import (
        load_mbp1_from_dbn,
        process_mbp1_to_ticks,
        ensure_mbp_table,
        ensure_ohlcv_table,
        insert_mbp_ticks,
        aggregate_mbp_to_ohlcv,
    )

    print(f"\nLoading MBP-1 into database...")

    with DuckDBStorage() as storage:
        ensure_mbp_table(storage)
        ensure_ohlcv_table(storage)

        # Load and process
        df_mbp = load_mbp1_from_dbn(file_path)
        df_ticks = process_mbp1_to_ticks(df_mbp)

        print(f"Inserting {len(df_ticks):,} MBP ticks...")
        insert_mbp_ticks(storage, df_ticks)
        storage.conn.commit()

        # Aggregate to OHLCV
        aggregate_mbp_to_ohlcv(storage)
        storage.conn.commit()


def print_summary():
    """Print database summary after loading"""
    from scripts.data.load_historical_data import print_summary as loader_summary

    with DuckDBStorage() as storage:
        loader_summary(storage)


def main():
    parser = argparse.ArgumentParser(
        description='Preload historical data from Databento',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Estimate cost before downloading
  python scripts/data/preload_historical.py --estimate

  # Download and load all data (5yr OHLCV + 60d MBP-1)
  python scripts/data/preload_historical.py --load

  # Download only OHLCV
  python scripts/data/preload_historical.py --load --ohlcv-only

  # Custom ranges
  python scripts/data/preload_historical.py --load --ohlcv-years 3 --mbp-days 90
        """
    )
    parser.add_argument('--estimate', action='store_true',
                        help='Estimate cost without downloading')
    parser.add_argument('--load', action='store_true',
                        help='Download and load data')
    parser.add_argument('--ohlcv-only', action='store_true',
                        help='Only download OHLCV data')
    parser.add_argument('--mbp-only', action='store_true',
                        help='Only download MBP-1 data')
    parser.add_argument('--ohlcv-years', type=int, default=DEFAULT_OHLCV_YEARS,
                        help=f'Years of OHLCV data (default: {DEFAULT_OHLCV_YEARS})')
    parser.add_argument('--ohlcv-start', type=str,
                        help='Fixed start date for OHLCV (YYYY-MM-DD)')
    parser.add_argument('--mbp-days', type=int, default=DEFAULT_MBP_DAYS,
                        help=f'Days of MBP-1 data (default: {DEFAULT_MBP_DAYS})')
    parser.add_argument('--keep-files', action='store_true',
                        help='Keep downloaded DBN files after loading')

    args = parser.parse_args()

    if not args.estimate and not args.load:
        parser.print_help()
        return

    print("=" * 60)
    print("  Historical Data Preload Utility")
    print("=" * 60)

    # Get API key
    try:
        secrets = get_secrets()
        api_key = secrets.api_key
    except Exception as e:
        print(f"\n[ERROR] Could not load Databento API key: {e}")
        print("  Make sure config/secrets.yaml exists with your API key")
        return

    # Calculate date ranges
    date_ranges = get_date_ranges(
        ohlcv_years=args.ohlcv_years,
        ohlcv_start=args.ohlcv_start,
        mbp_days=args.mbp_days
    )

    print(f"\nData ranges:")
    print(f"  OHLCV-1M: {date_ranges['ohlcv']['start']} to {date_ranges['ohlcv']['end']} ({date_ranges['ohlcv']['days']} days)")
    print(f"  MBP-1:    {date_ranges['mbp']['start']} to {date_ranges['mbp']['end']} ({date_ranges['mbp']['days']} days)")

    # Estimate cost
    if args.estimate:
        estimate_cost(api_key, date_ranges)
        print("\nTo proceed with download, run with --load flag")
        return

    # Download and load
    if args.load:
        # Confirm with user
        print("\n" + "=" * 60)
        print("  Ready to Download")
        print("=" * 60)

        if not args.ohlcv_only and not args.mbp_only:
            print("\nThis will download:")
            print(f"  - {date_ranges['ohlcv']['days']} days of OHLCV-1M data")
            print(f"  - {date_ranges['mbp']['days']} days of MBP-1 data")
        elif args.ohlcv_only:
            print(f"\nThis will download {date_ranges['ohlcv']['days']} days of OHLCV-1M data")
        elif args.mbp_only:
            print(f"\nThis will download {date_ranges['mbp']['days']} days of MBP-1 data")

        print("\nNote: Run with --estimate first to check costs")

        response = input("\nProceed? [y/N]: ").strip().lower()
        if response != 'y':
            print("Aborted.")
            return

        # Setup output directory
        output_dir = Path(__file__).parent.parent.parent / "data"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Download and load OHLCV
        if not args.mbp_only:
            ohlcv_path = download_ohlcv(
                api_key,
                date_ranges['ohlcv']['start'],
                date_ranges['ohlcv']['end'],
                output_dir
            )
            if ohlcv_path:
                load_ohlcv_file(ohlcv_path)
                if not args.keep_files:
                    ohlcv_path.unlink()
                    print(f"  Cleaned up: {ohlcv_path.name}")

        # Download and load MBP-1
        if not args.ohlcv_only:
            mbp_path = download_mbp(
                api_key,
                date_ranges['mbp']['start'],
                date_ranges['mbp']['end'],
                output_dir
            )
            if mbp_path:
                load_mbp_file(mbp_path)
                if not args.keep_files:
                    mbp_path.unlink()
                    print(f"  Cleaned up: {mbp_path.name}")

        # Print summary
        print_summary()

        print("\n" + "=" * 60)
        print("  Preload Complete!")
        print("=" * 60)


if __name__ == "__main__":
    main()
