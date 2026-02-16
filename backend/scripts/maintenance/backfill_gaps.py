"""
Gap Detection and Backfill Utility

Detects gaps in ohlcv_ticks data and optionally backfills from Databento.
Downloads both OHLCV-1M (price/volume) and MBP-1 (orderflow metrics).

Usage:
    # Check for gaps only
    python scripts/maintenance/backfill_gaps.py --check

    # Backfill gaps from Databento (downloads OHLCV + MBP-1)
    python scripts/maintenance/backfill_gaps.py --backfill

    # Backfill specific date range
    python scripts/maintenance/backfill_gaps.py --backfill --start 2024-01-15 --end 2024-01-16

    # Backfill only OHLCV data (no orderflow)
    python scripts/maintenance/backfill_gaps.py --backfill --ohlcv-only

    # Backfill only MBP-1 data (orderflow only, overlay on existing OHLCV)
    python scripts/maintenance/backfill_gaps.py --backfill --mbp-only

    # Clean and re-download data for a specific date (useful when live ingestion had issues)
    python scripts/maintenance/backfill_gaps.py --clean --backfill --start 2025-02-11 --end 2025-02-11

    # Just clean data for a date (no re-download)
    python scripts/maintenance/backfill_gaps.py --clean --start 2025-02-11 --end 2025-02-11
"""
import sys
import argparse
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from app.data.storage import DuckDBStorage
from config import get_secrets, get_config

# Database paths
DB_PATH = Path(__file__).parent.parent.parent / "data" / "market_data.duckdb"
BACKUP_DIR = Path(__file__).parent.parent.parent / "data" / "backups"


def backup_database() -> Path | None:
    """Create a backup of the current database before destructive operations.

    Returns:
        Path to backup file, or None if no database exists
    """
    print("\n" + "=" * 60)
    print("  Creating Database Backup")
    print("=" * 60)

    # Find the actual database file (may be in different locations)
    possible_paths = [
        DB_PATH,
        Path("/app/data/market_data.duckdb"),  # Docker container path
    ]

    db_file = None
    for path in possible_paths:
        if path.exists():
            db_file = path
            break

    if not db_file:
        print("  No existing database found - skipping backup")
        return None

    # Create backup directory
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Create timestamped backup
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = BACKUP_DIR / f"market_data_backup_{timestamp}.duckdb"

    try:
        size_mb = db_file.stat().st_size / 1024 / 1024
        print(f"  Source: {db_file} ({size_mb:.1f} MB)")

        shutil.copy2(db_file, backup_path)

        if backup_path.exists():
            backup_size = backup_path.stat().st_size / 1024 / 1024
            print(f"  Backup created: {backup_path.name} ({backup_size:.1f} MB)")
            return backup_path
        else:
            print("  ERROR: Backup file was not created")
            return None
    except Exception as e:
        print(f"  ERROR: Failed to create backup: {e}")
        return None


# Expected gaps (CME closed times - weekends, holidays)
# CME Globex is open Sunday 5pm CT - Friday 4pm CT
# Daily maintenance: 4pm-5pm CT (Mon-Thu)


def detect_gaps(timeframe: str = "1H", max_gap_bars: int = 2) -> List[Tuple[datetime, datetime]]:
    """Detect gaps in ohlcv_ticks data

    Args:
        timeframe: Timeframe to check (1H recommended for gap detection)
        max_gap_bars: Number of missing bars to consider a gap

    Returns:
        List of (gap_start, gap_end) tuples
    """
    print(f"\nDetecting gaps in {timeframe} data...")

    # Timeframe to expected interval
    intervals = {
        "5M": timedelta(minutes=5),
        "15M": timedelta(minutes=15),
        "1H": timedelta(hours=1),
        "4H": timedelta(hours=4),
        "1D": timedelta(days=1),
    }
    expected_interval = intervals.get(timeframe, timedelta(hours=1))

    with DuckDBStorage() as storage:
        # Get all timestamps ordered
        df = storage.conn.execute(f"""
            SELECT timestamp
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            ORDER BY timestamp
        """).pl()

        if len(df) == 0:
            print("  No data found")
            return []

        timestamps = df["timestamp"].to_list()
        print(f"  Data range: {timestamps[0]} to {timestamps[-1]}")
        print(f"  Total bars: {len(timestamps):,}")

    gaps = []
    for i in range(1, len(timestamps)):
        actual_gap = timestamps[i] - timestamps[i-1]
        expected_gap = expected_interval

        # Allow for CME daily maintenance (1 hour gap) and weekends
        if actual_gap > expected_gap * max_gap_bars:
            # Check if this is a weekend gap (expected)
            prev_ts = timestamps[i-1]
            curr_ts = timestamps[i]

            # Skip weekend gaps (Friday evening to Sunday evening)
            if prev_ts.weekday() == 4 and curr_ts.weekday() == 6:
                continue
            # Skip daily maintenance gaps (4pm-5pm CT, roughly 21:00-22:00 UTC)
            if actual_gap <= timedelta(hours=2) and prev_ts.hour in [20, 21, 22]:
                continue

            gaps.append((prev_ts, curr_ts))

    return gaps


def print_gaps(gaps: List[Tuple[datetime, datetime]]):
    """Print detected gaps"""
    if not gaps:
        print("\n  No unexpected gaps detected!")
        return

    print(f"\n  Found {len(gaps)} gap(s):")
    for start, end in gaps:
        duration = end - start
        print(f"    {start} -> {end} ({duration})")


def get_dates_to_backfill(gaps: List[Tuple[datetime, datetime]]) -> List[str]:
    """Get unique dates that need backfilling"""
    dates = set()
    for start, end in gaps:
        current = start.date()
        end_date = end.date()
        while current <= end_date:
            dates.add(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)
    return sorted(dates)


def clean_data_for_date(date: str, symbol: str = "MNQ") -> dict:
    """Clean all data for a specific date

    Removes data from:
    - ohlcv_ticks (all timeframes)
    - mbp_ticks

    Args:
        date: Date string (YYYY-MM-DD)
        symbol: Symbol to clean

    Returns:
        Dict with counts of deleted rows per table
    """
    print(f"\n  Cleaning data for {date}...")

    # Parse date for timestamp range
    dt = datetime.strptime(date, '%Y-%m-%d')
    start_ts = dt
    end_ts = dt + timedelta(days=1)

    deleted = {}

    with DuckDBStorage() as storage:
        # Clean ohlcv_ticks
        try:
            result = storage.conn.execute(f"""
                DELETE FROM ohlcv_ticks
                WHERE symbol = '{symbol}'
                AND timestamp >= '{start_ts}'
                AND timestamp < '{end_ts}'
            """)
            deleted['ohlcv_ticks'] = result.fetchone()[0] if result else 0
            print(f"    ohlcv_ticks: {deleted['ohlcv_ticks']:,} rows deleted")
        except Exception as e:
            print(f"    ohlcv_ticks: error - {e}")
            deleted['ohlcv_ticks'] = 0

        # Clean mbp_ticks
        try:
            result = storage.conn.execute(f"""
                DELETE FROM mbp_ticks
                WHERE symbol = '{symbol}'
                AND timestamp >= '{start_ts}'
                AND timestamp < '{end_ts}'
            """)
            deleted['mbp_ticks'] = result.fetchone()[0] if result else 0
            print(f"    mbp_ticks: {deleted['mbp_ticks']:,} rows deleted")
        except Exception as e:
            print(f"    mbp_ticks: error - {e}")
            deleted['mbp_ticks'] = 0

        storage.conn.commit()

    return deleted


def clean_date_range(start_date: str, end_date: str, symbol: str = "MNQ"):
    """Clean data for a date range

    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        symbol: Symbol to clean
    """
    print(f"\n{'='*60}")
    print(f"  Cleaning data from {start_date} to {end_date}")
    print(f"{'='*60}")

    dates = []
    current = datetime.strptime(start_date, '%Y-%m-%d').date()
    end = datetime.strptime(end_date, '%Y-%m-%d').date()
    while current <= end:
        dates.append(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)

    total_deleted = {'ohlcv_ticks': 0, 'mbp_ticks': 0}

    for date in dates:
        deleted = clean_data_for_date(date, symbol)
        for table, count in deleted.items():
            total_deleted[table] += count

    print(f"\n  Total cleaned:")
    for table, count in total_deleted.items():
        print(f"    {table}: {count:,} rows")

    return total_deleted


def download_from_databento(
    date: str,
    output_dir: Path,
    api_key: str,
    schema: str = "mbp-1",
    dataset: str = "GLBX.MDP3",
    symbol: str = "MNQ.c.0"
) -> Optional[Path]:
    """Download data from Databento for a specific date

    Args:
        date: Date string (YYYY-MM-DD)
        output_dir: Directory to save the file
        api_key: Databento API key
        schema: Data schema (mbp-1 or ohlcv-1m)
        dataset: Databento dataset
        symbol: Symbol to download (default MNQ.c.0 for continuous front-month)

    Returns:
        Path to downloaded file, or None if failed
    """
    try:
        schema_name = schema.replace("-", "")
        print(f"  Downloading {schema} for {date}...")

        client = db.Historical(api_key)

        # Parse date
        dt = datetime.strptime(date, '%Y-%m-%d')
        start = dt.strftime('%Y-%m-%dT00:00:00')
        end = (dt + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00')

        output_path = output_dir / f"{schema_name}_{date}.dbn.zst"

        # Remove existing file if present
        if output_path.exists():
            output_path.unlink()

        # Download data using continuous contract symbology
        client.timeseries.get_range(
            dataset=dataset,
            symbols=[symbol],
            stype_in="continuous",
            schema=schema,
            start=start,
            end=end,
            path=str(output_path),
        )

        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"    Downloaded: {output_path.name} ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")
            return output_path
        else:
            print(f"    No data available for {date}")
            return None

    except Exception as e:
        print(f"    Error downloading {date}: {e}")
        return None


def load_ohlcv_file(file_path: Path):
    """Load an OHLCV file using the historical data loader"""
    from scripts.data.load_historical_data import (
        load_ohlcv_from_dbn,
        filter_ohlcv_data,
        resample_to_timeframe,
        ensure_ohlcv_table,
        insert_ohlcv_data,
        TIMEFRAMES,
    )

    with DuckDBStorage() as storage:
        ensure_ohlcv_table(storage)

        # Load and process OHLCV
        df_1m = load_ohlcv_from_dbn(file_path)
        df_1m = filter_ohlcv_data(df_1m)

        print("  Inserting OHLCV data...")
        for tf in TIMEFRAMES:
            df_tf = resample_to_timeframe(df_1m, tf)
            insert_ohlcv_data(storage, df_tf, tf)
            print(f"    {tf}: {len(df_tf):,} bars")

        storage.conn.commit()


def load_mbp_file(file_path: Path):
    """Load an MBP file and overlay orderflow onto existing OHLCV"""
    from scripts.data.load_historical_data import (
        load_mbp1_from_dbn,
        process_mbp1_to_ticks,
        ensure_mbp_table,
        ensure_ohlcv_table,
        insert_mbp_ticks,
        aggregate_mbp_to_ohlcv,
    )

    with DuckDBStorage() as storage:
        ensure_mbp_table(storage)
        ensure_ohlcv_table(storage)

        # Load and process MBP
        df_mbp = load_mbp1_from_dbn(file_path)
        df_ticks = process_mbp1_to_ticks(df_mbp)

        print(f"  Inserting {len(df_ticks):,} MBP ticks...")
        insert_mbp_ticks(storage, df_ticks)
        storage.conn.commit()

        # Aggregate to OHLCV (this overlays orderflow onto existing bars)
        aggregate_mbp_to_ohlcv(storage)
        storage.conn.commit()


def backfill_gaps(
    gaps: List[Tuple[datetime, datetime]],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    ohlcv_only: bool = False,
    mbp_only: bool = False
):
    """Backfill gaps from Databento

    Downloads both OHLCV-1M (for price/volume) and MBP-1 (for orderflow metrics).

    Args:
        gaps: List of detected gaps
        start_date: Optional start date override
        end_date: Optional end date override
        ohlcv_only: Only download OHLCV data
        mbp_only: Only download MBP data
    """
    # Get API key
    try:
        secrets = get_secrets()
        api_key = secrets.api_key
    except Exception as e:
        print(f"\n[ERROR] Could not load Databento API key: {e}")
        print("  Make sure config/secrets.yaml exists with your API key")
        return

    # Determine dates to backfill
    if start_date and end_date:
        dates = []
        current = datetime.strptime(start_date, '%Y-%m-%d').date()
        end = datetime.strptime(end_date, '%Y-%m-%d').date()
        while current <= end:
            dates.append(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)
    else:
        dates = get_dates_to_backfill(gaps)

    if not dates:
        print("\nNo dates to backfill")
        return

    print(f"\nBackfilling {len(dates)} date(s): {dates[0]} to {dates[-1]}")

    # Setup output directory
    output_dir = Path(__file__).parent.parent.parent / "data" / "backfill"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Download and load each date
    for date in dates:
        print(f"\n--- Processing {date} ---")

        # Step 1: Download and load OHLCV (price + volume)
        if not mbp_only:
            ohlcv_path = download_from_databento(date, output_dir, api_key, schema="ohlcv-1m")
            if ohlcv_path:
                load_ohlcv_file(ohlcv_path)

        # Step 2: Download and load MBP-1 (orderflow metrics)
        if not ohlcv_only:
            mbp_path = download_from_databento(date, output_dir, api_key, schema="mbp-1")
            if mbp_path:
                load_mbp_file(mbp_path)

    # Step 3: Re-aggregate 4H and 1D bars to CME session boundaries
    # (OHLCV loading uses UTC boundaries, this fixes to CME session start: 23:00 UTC)
    # Note: We call reaggregate_timeframe() directly instead of main() because
    # main() uses argparse which would conflict with this script's arguments
    if not mbp_only:
        print("\n  Re-aggregating 4H/1D to CME session boundaries...")
        try:
            from scripts.maintenance.reaggregate_timeframes import reaggregate_timeframe
            reaggregate_timeframe('5M', '4H')
            reaggregate_timeframe('5M', '1D')
            print("  Reaggregation complete.")
        except Exception as e:
            print(f"  Warning: Reaggregation failed: {e}")

    print("\n" + "=" * 60)
    print("  Backfill complete!")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='Detect and backfill data gaps')
    parser.add_argument('--check', action='store_true', help='Check for gaps only')
    parser.add_argument('--backfill', action='store_true', help='Backfill gaps from Databento')
    parser.add_argument('--clean', action='store_true', help='Clean existing data before backfill (requires --start/--end)')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--timeframe', type=str, default='1H', help='Timeframe for gap detection')
    parser.add_argument('--ohlcv-only', action='store_true', help='Only download OHLCV data')
    parser.add_argument('--mbp-only', action='store_true', help='Only download MBP-1 data')
    parser.add_argument('--symbol', type=str, default='MNQ', help='Symbol to process')
    parser.add_argument('--no-backup', action='store_true', help='Skip database backup (not recommended)')

    args = parser.parse_args()

    # Validate clean requires date range
    if args.clean and (not args.start or not args.end):
        print("[ERROR] --clean requires both --start and --end dates")
        print("  Example: --clean --start 2025-02-11 --end 2025-02-11")
        return

    if not args.check and not args.backfill and not args.clean:
        args.check = True  # Default to check

    print("=" * 60)
    print("  Gap Detection & Backfill Utility")
    print("=" * 60)

    # Create backup before destructive operations
    backup_path = None
    if (args.clean or args.backfill) and not args.no_backup:
        backup_path = backup_database()
        if backup_path:
            print(f"\n  Restore command if needed:")
            print(f"    cp {backup_path} {DB_PATH}")

    # Clean data if requested
    if args.clean:
        clean_date_range(args.start, args.end, args.symbol)

    # Backfill if requested
    if args.backfill:
        if args.start and args.end:
            # Use specified date range
            backfill_gaps(
                [],  # No gaps, using explicit dates
                args.start,
                args.end,
                ohlcv_only=args.ohlcv_only,
                mbp_only=args.mbp_only
            )
        else:
            # Detect gaps first
            gaps = detect_gaps(args.timeframe)
            print_gaps(gaps)
            backfill_gaps(
                gaps,
                args.start,
                args.end,
                ohlcv_only=args.ohlcv_only,
                mbp_only=args.mbp_only
            )
    elif args.check:
        # Just check for gaps
        gaps = detect_gaps(args.timeframe)
        print_gaps(gaps)
        if gaps:
            print("\nTo backfill these gaps, run:")
            print("  python scripts/maintenance/backfill_gaps.py --backfill")


if __name__ == "__main__":
    main()
