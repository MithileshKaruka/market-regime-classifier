"""
Weekly Database Reload Script

Designed to run every Saturday night when markets are closed.
Downloads fresh data only if the cost is $0 (data already in Databento cache).

Data downloaded:
- OHLCV-1M: 5 years of price history
- MBP-1: 14 days of orderflow data (DOM imbalance, quote-inferred delta)
- Trades: 14 days of trade data (accurate delta, trade flow metrics)

Safety features:
- Backs up database before any changes
- Only deletes backup after successful reload
- Automatic restore if reload fails

Usage:
    # Check cost only (dry run)
    python scripts/maintenance/weekly_reload.py --check

    # Run full reload (only proceeds if cost is $0)
    python scripts/maintenance/weekly_reload.py --reload

    # Force reload even if cost > $0 (use with caution)
    python scripts/maintenance/weekly_reload.py --reload --force
"""
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
from config import get_secrets

# Configuration
OHLCV_YEARS = 5       # 5 years of OHLCV data
MBP_DAYS = 14         # 14 days of MBP-1 data
TRADES_DAYS = 14      # 14 days of trades data (for accurate delta/trade flow metrics)
DATASET = "GLBX.MDP3"
SYMBOL = "MNQ.c.0"    # Continuous front-month contract
STYPE_IN = "continuous"
MAX_ALLOWED_COST = 0.0  # Only proceed if cost is $0

# Backup configuration
BACKUP_DIR = Path(__file__).parent.parent.parent / "data" / "backups"
DB_PATH = Path(__file__).parent.parent.parent / "data" / "market_data.duckdb"


def backup_database() -> Path | None:
    """Create a backup of the current database

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
        Path(__file__).parent.parent.parent / "data" / "market_data.db",
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
        # Copy database file
        size_mb = db_file.stat().st_size / 1024 / 1024
        print(f"  Source: {db_file} ({size_mb:.1f} MB)")

        shutil.copy2(db_file, backup_path)

        if backup_path.exists():
            print(f"  Backup created: {backup_path.name}")
            return backup_path
        else:
            print("  ERROR: Backup file was not created")
            return None

    except Exception as e:
        print(f"  ERROR: Failed to create backup: {e}")
        return None


def restore_database(backup_path: Path) -> bool:
    """Restore database from backup

    Args:
        backup_path: Path to backup file

    Returns:
        True if restore successful
    """
    print("\n" + "=" * 60)
    print("  Restoring Database from Backup")
    print("=" * 60)

    if not backup_path or not backup_path.exists():
        print("  ERROR: Backup file not found")
        return False

    # Find the target database path
    possible_paths = [
        DB_PATH,
        Path("/app/data/market_data.duckdb"),
    ]

    target_path = None
    for path in possible_paths:
        if path.parent.exists():
            target_path = path
            break

    if not target_path:
        target_path = DB_PATH
        target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        print(f"  Restoring from: {backup_path.name}")
        shutil.copy2(backup_path, target_path)
        print(f"  Restored to: {target_path}")
        return True

    except Exception as e:
        print(f"  ERROR: Failed to restore: {e}")
        return False


def cleanup_backup(backup_path: Path):
    """Delete backup file after successful reload

    Args:
        backup_path: Path to backup file to delete
    """
    print("\n" + "=" * 60)
    print("  Cleaning Up Backup")
    print("=" * 60)

    if not backup_path:
        print("  No backup to clean up")
        return

    if not backup_path.exists():
        print("  Backup already removed")
        return

    try:
        size_mb = backup_path.stat().st_size / 1024 / 1024
        backup_path.unlink()
        print(f"  Deleted: {backup_path.name} ({size_mb:.1f} MB freed)")
    except Exception as e:
        print(f"  WARNING: Could not delete backup: {e}")
        print(f"  Manual cleanup: rm {backup_path}")


def cleanup_old_backups(keep_count: int = 2):
    """Remove old backup files, keeping only the most recent ones

    Args:
        keep_count: Number of recent backups to keep
    """
    if not BACKUP_DIR.exists():
        return

    backups = sorted(BACKUP_DIR.glob("market_data_backup_*.duckdb"), reverse=True)

    if len(backups) <= keep_count:
        return

    print(f"\n  Cleaning old backups (keeping {keep_count} most recent)...")
    for old_backup in backups[keep_count:]:
        try:
            size_mb = old_backup.stat().st_size / 1024 / 1024
            old_backup.unlink()
            print(f"    Deleted: {old_backup.name} ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"    WARNING: Could not delete {old_backup.name}: {e}")


def get_date_ranges() -> dict:
    """Calculate date ranges for data download"""
    today = datetime.now(timezone.utc).date()

    ohlcv_start = today - timedelta(days=OHLCV_YEARS * 365)
    mbp_start = today - timedelta(days=MBP_DAYS)
    trades_start = today - timedelta(days=TRADES_DAYS)

    return {
        'ohlcv': {
            'start': ohlcv_start.strftime('%Y-%m-%d'),
            'end': today.strftime('%Y-%m-%d'),
            'days': (today - ohlcv_start).days,
        },
        'mbp': {
            'start': mbp_start.strftime('%Y-%m-%d'),
            'end': today.strftime('%Y-%m-%d'),
            'days': (today - mbp_start).days,
        },
        'trades': {
            'start': trades_start.strftime('%Y-%m-%d'),
            'end': today.strftime('%Y-%m-%d'),
            'days': (today - trades_start).days,
        }
    }


def estimate_cost(api_key: str, date_ranges: dict) -> tuple[float, dict]:
    """Estimate download cost from Databento

    Returns:
        Tuple of (total_cost, cost_details)
    """
    print("\n" + "=" * 60)
    print("  Cost Estimation")
    print("=" * 60)

    client = db.Historical(api_key)
    costs = {'ohlcv': 0.0, 'mbp': 0.0, 'trades': 0.0}

    # OHLCV cost
    print(f"\nOHLCV-1M ({date_ranges['ohlcv']['days']} days)...")
    print(f"  Range: {date_ranges['ohlcv']['start']} to {date_ranges['ohlcv']['end']}")
    try:
        ohlcv_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="ohlcv-1m",
            start=date_ranges['ohlcv']['start'],
            end=date_ranges['ohlcv']['end'],
        )
        costs['ohlcv'] = float(ohlcv_cost)
        print(f"  Estimated cost: ${ohlcv_cost:.2f}")
    except Exception as e:
        print(f"  Error estimating: {e}")
        costs['ohlcv'] = -1  # Error flag

    # MBP-1 cost
    print(f"\nMBP-1 ({date_ranges['mbp']['days']} days)...")
    print(f"  Range: {date_ranges['mbp']['start']} to {date_ranges['mbp']['end']}")
    try:
        mbp_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="mbp-1",
            start=date_ranges['mbp']['start'],
            end=date_ranges['mbp']['end'],
        )
        costs['mbp'] = float(mbp_cost)
        print(f"  Estimated cost: ${mbp_cost:.2f}")
    except Exception as e:
        print(f"  Error estimating: {e}")
        costs['mbp'] = -1  # Error flag

    # Trades cost
    print(f"\nTrades ({date_ranges['trades']['days']} days)...")
    print(f"  Range: {date_ranges['trades']['start']} to {date_ranges['trades']['end']}")
    try:
        trades_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="trades",
            start=date_ranges['trades']['start'],
            end=date_ranges['trades']['end'],
        )
        costs['trades'] = float(trades_cost)
        print(f"  Estimated cost: ${trades_cost:.2f}")
    except Exception as e:
        print(f"  Error estimating: {e}")
        costs['trades'] = -1  # Error flag

    # Check for errors
    if costs['ohlcv'] < 0 or costs['mbp'] < 0 or costs['trades'] < 0:
        print(f"\n{'='*60}")
        print(f"  ERROR: Could not estimate costs")
        print(f"{'='*60}")
        return -1, costs

    total = costs['ohlcv'] + costs['mbp'] + costs['trades']

    print(f"\n{'='*60}")
    print(f"  TOTAL ESTIMATED COST: ${total:.2f}")
    print(f"{'='*60}")

    return total, costs


def reset_database():
    """Reset database to empty state"""
    print("\n" + "=" * 60)
    print("  Resetting Database")
    print("=" * 60)

    from app.data.storage import DuckDBStorage

    with DuckDBStorage() as storage:
        # Drop and recreate tables
        print("\nDropping existing tables...")
        storage.conn.execute("DROP TABLE IF EXISTS ohlcv_ticks")
        storage.conn.execute("DROP TABLE IF EXISTS mbp_ticks")
        storage.conn.execute("DROP TABLE IF EXISTS regimes")
        storage.conn.commit()

        print("Creating fresh tables...")
        storage.conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv_ticks (
                timestamp TIMESTAMP NOT NULL,
                symbol VARCHAR NOT NULL,
                timeframe VARCHAR NOT NULL,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                instant_delta DOUBLE,
                dom_imbalance DOUBLE,
                total_bid_depth DOUBLE,
                total_ask_depth DOUBLE,
                cvd BIGINT,
                trade_flow_ratio DOUBLE,
                buy_trades INTEGER,
                sell_trades INTEGER,
                large_trade_count INTEGER,
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)

        storage.conn.execute("""
            CREATE TABLE IF NOT EXISTS regimes (
                timestamp TIMESTAMP NOT NULL,
                symbol VARCHAR NOT NULL,
                timeframe VARCHAR NOT NULL,
                regime VARCHAR,
                regime_score DOUBLE,
                trend_score DOUBLE,
                momentum_score DOUBLE,
                volatility_score DOUBLE,
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)

        storage.conn.commit()
        print("Database reset complete!")


def download_and_load_ohlcv(api_key: str, start: str, end: str):
    """Download and load OHLCV data"""
    print("\n" + "=" * 60)
    print("  Downloading OHLCV Data")
    print("=" * 60)

    from scripts.data.preload_historical import (
        download_ohlcv,
        load_ohlcv_file,
    )

    output_dir = Path(__file__).parent.parent.parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    ohlcv_path = download_ohlcv(api_key, start, end, output_dir)

    if ohlcv_path:
        load_ohlcv_file(ohlcv_path)
        # Clean up file
        ohlcv_path.unlink()
        print(f"  Cleaned up: {ohlcv_path.name}")
        return True
    else:
        print("  ERROR: Failed to download OHLCV data")
        return False


def download_and_load_mbp(api_key: str, start: str, end: str):
    """Download and load MBP data using chunked approach"""
    print("\n" + "=" * 60)
    print("  Downloading MBP-1 Data")
    print("=" * 60)

    from scripts.data.preload_historical import download_and_load_mbp_chunked

    try:
        download_and_load_mbp_chunked(
            api_key,
            start,
            end,
            hours_per_chunk=4  # 4-hour chunks for memory efficiency
        )
        return True
    except Exception as e:
        print(f"  ERROR: Failed to download MBP data: {e}")
        return False


def download_and_load_trades(api_key: str, start: str, end: str):
    """Download and load trades data using chunked approach

    Trades data provides accurate delta calculation and trade flow metrics:
    - True delta from actual trade sides (not quote inference)
    - Trade flow ratio (buy/sell proportion)
    - Large trade detection (institutional activity)
    """
    print("\n" + "=" * 60)
    print("  Downloading Trades Data")
    print("=" * 60)

    from scripts.data.preload_historical import download_and_load_trades_chunked

    try:
        download_and_load_trades_chunked(
            api_key,
            start,
            end,
            hours_per_chunk=4  # 4-hour chunks for memory efficiency
        )
        return True
    except Exception as e:
        print(f"  ERROR: Failed to download trades data: {e}")
        return False


def print_summary() -> dict:
    """Print database summary and return stats

    Returns:
        Dict with bar counts per timeframe
    """
    from app.data.storage import DuckDBStorage

    print("\n" + "=" * 60)
    print("  Database Summary")
    print("=" * 60)

    stats = {}

    with DuckDBStorage() as storage:
        result = storage.conn.execute("""
            SELECT
                timeframe,
                COUNT(*) as bars,
                MIN(timestamp) as start_time,
                MAX(timestamp) as end_time
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ'
            GROUP BY timeframe
            ORDER BY timeframe
        """).fetchall()

        print("\nOHLCV Data:")
        for row in result:
            print(f"  {row[0]}: {row[1]:,} bars | {row[2]} to {row[3]}")
            stats[row[0]] = row[1]

        # Check orderflow coverage
        result = storage.conn.execute("""
            SELECT
                timeframe,
                COUNT(*) as total,
                SUM(CASE WHEN dom_imbalance IS NOT NULL THEN 1 ELSE 0 END) as with_orderflow
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ'
            GROUP BY timeframe
            ORDER BY timeframe
        """).fetchall()

        print("\nOrderflow Coverage:")
        for row in result:
            pct = (row[2] / row[1] * 100) if row[1] > 0 else 0
            print(f"  {row[0]}: {row[2]:,}/{row[1]:,} ({pct:.1f}%)")

    return stats


def verify_data_loaded(min_bars: dict = None) -> bool:
    """Verify that data was loaded correctly

    Args:
        min_bars: Minimum expected bars per timeframe

    Returns:
        True if data passes validation
    """
    print("\n" + "=" * 60)
    print("  Verifying Data Integrity")
    print("=" * 60)

    # Default minimum bars (approximate for configured OHLCV_YEARS)
    if min_bars is None:
        # Scale based on configured years
        min_bars = {
            '5M': int(20000 * OHLCV_YEARS),    # ~20k bars per year
            '15M': int(6000 * OHLCV_YEARS),    # ~6k bars per year
            '1H': int(1600 * OHLCV_YEARS),     # ~1.6k bars per year
            '4H': int(400 * OHLCV_YEARS),      # ~400 bars per year
            '1D': int(100 * OHLCV_YEARS),      # ~100 bars per year (trading days)
        }

    from app.data.storage import DuckDBStorage

    all_passed = True

    with DuckDBStorage() as storage:
        for timeframe, min_count in min_bars.items():
            result = storage.conn.execute(f"""
                SELECT COUNT(*) FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            """).fetchone()

            actual_count = result[0] if result else 0

            if actual_count >= min_count:
                print(f"  [OK] {timeframe}: {actual_count:,} bars (min: {min_count:,})")
            else:
                print(f"  [FAIL] {timeframe}: {actual_count:,} bars (min: {min_count:,})")
                all_passed = False

        # Check for recent data (within last 7 days for MBP coverage)
        result = storage.conn.execute("""
            SELECT MAX(timestamp) FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '15M'
        """).fetchone()

        if result and result[0]:
            latest = result[0]
            # Check if latest data is reasonably recent (within 3 days to account for weekends)
            if hasattr(latest, 'timestamp'):
                latest_ts = latest.timestamp()
            else:
                latest_ts = latest

            now = datetime.now(timezone.utc).timestamp()
            days_old = (now - latest_ts) / 86400 if isinstance(latest_ts, (int, float)) else 0

            if days_old <= 3:
                print(f"  [OK] Latest data: {latest} (within {days_old:.1f} days)")
            else:
                print(f"  [WARN] Latest data: {latest} ({days_old:.1f} days old)")

    return all_passed


def clean_archive_files():
    """Clean up old archive files to free disk space"""
    print("\n" + "=" * 60)
    print("  Cleaning Archive Files")
    print("=" * 60)

    # These paths are for Docker volume - adjust if running locally
    archive_paths = [
        Path("/app/data/archive"),  # Inside container
        Path(__file__).parent.parent.parent / "data" / "archive",  # Local
    ]

    for archive_dir in archive_paths:
        if archive_dir.exists():
            import shutil
            size_before = sum(f.stat().st_size for f in archive_dir.glob("**/*") if f.is_file())
            shutil.rmtree(archive_dir)
            archive_dir.mkdir(parents=True, exist_ok=True)
            print(f"  Cleaned: {archive_dir} ({size_before / 1024 / 1024:.1f} MB freed)")


def main():
    parser = argparse.ArgumentParser(
        description='Weekly database reload (Saturday night maintenance)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Configuration:
  OHLCV: {OHLCV_YEARS} years
  MBP-1: {MBP_DAYS} days
  Trades: {TRADES_DAYS} days
  Max cost: ${MAX_ALLOWED_COST:.2f}

Examples:
  # Check cost only (dry run)
  python scripts/maintenance/weekly_reload.py --check

  # Run full reload (only if cost is $0)
  python scripts/maintenance/weekly_reload.py --reload

  # Force reload even if cost > $0
  python scripts/maintenance/weekly_reload.py --reload --force
        """
    )
    parser.add_argument('--check', action='store_true',
                        help='Check cost only (dry run)')
    parser.add_argument('--reload', action='store_true',
                        help='Run full reload (only proceeds if cost is $0)')
    parser.add_argument('--force', action='store_true',
                        help='Force reload even if cost > $0')
    parser.add_argument('--skip-cost-check', action='store_true',
                        help='Skip cost estimation (use when API times out)')
    parser.add_argument('--skip-archive-cleanup', action='store_true',
                        help='Skip cleaning archive files')
    parser.add_argument('--keep-backup', action='store_true',
                        help='Keep backup even after successful reload')

    args = parser.parse_args()

    if not args.check and not args.reload:
        parser.print_help()
        return

    print("=" * 60)
    print("  Weekly Database Reload")
    print("  " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 60)

    # Get API key
    try:
        secrets = get_secrets()
        api_key = secrets.api_key
    except Exception as e:
        print(f"\n[ERROR] Could not load Databento API key: {e}")
        return 1

    # Calculate date ranges
    date_ranges = get_date_ranges()

    print(f"\nData Configuration:")
    print(f"  OHLCV: {OHLCV_YEARS} years ({date_ranges['ohlcv']['days']} days)")
    print(f"  MBP-1: {MBP_DAYS} days")
    print(f"  Trades: {TRADES_DAYS} days")
    print(f"  Max allowed cost: ${MAX_ALLOWED_COST:.2f}")

    # Skip cost check if requested
    if args.skip_cost_check:
        print("\n[SKIP] Cost check skipped (--skip-cost-check)")
        total_cost = 0.0
        cost_details = {'ohlcv': 0.0, 'mbp': 0.0, 'trades': 0.0}
    else:
        # Estimate cost
        total_cost, cost_details = estimate_cost(api_key, date_ranges)

        if total_cost < 0:
            print("\n[ERROR] Could not estimate costs. Aborting.")
            print("  Use --skip-cost-check to bypass (only if you're sure data is cached)")
            return 1

    # Check only mode
    if args.check:
        if total_cost <= MAX_ALLOWED_COST:
            print(f"\n[OK] Cost is ${total_cost:.2f} - reload would proceed")
        else:
            print(f"\n[BLOCKED] Cost is ${total_cost:.2f} - exceeds max ${MAX_ALLOWED_COST:.2f}")
        return 0

    # Reload mode
    if args.reload:
        # Check cost threshold
        if total_cost > MAX_ALLOWED_COST and not args.force:
            print(f"\n[BLOCKED] Cost ${total_cost:.2f} exceeds max ${MAX_ALLOWED_COST:.2f}")
            print("  Data is not cached in Databento - downloading would incur charges.")
            print("  Use --force to proceed anyway (will charge your account)")
            return 1

        if total_cost > MAX_ALLOWED_COST and args.force:
            print(f"\n[WARNING] Forcing reload with cost ${total_cost:.2f}")
            response = input("Are you sure? This will charge your Databento account. [y/N]: ").strip().lower()
            if response != 'y':
                print("Aborted.")
                return 0

        print(f"\n[OK] Cost is ${total_cost:.2f} - proceeding with reload")

        # Step 1: Create backup before any changes
        backup_path = backup_database()

        reload_success = False

        try:
            # Step 2: Clean archive files
            if not args.skip_archive_cleanup:
                clean_archive_files()

            # Step 3: Reset database
            reset_database()

            # Step 4: Download and load OHLCV
            if not download_and_load_ohlcv(
                api_key,
                date_ranges['ohlcv']['start'],
                date_ranges['ohlcv']['end']
            ):
                raise Exception("OHLCV download/load failed")

            # Step 5: Download and load MBP
            if not download_and_load_mbp(
                api_key,
                date_ranges['mbp']['start'],
                date_ranges['mbp']['end']
            ):
                raise Exception("MBP download/load failed")

            # Step 6: Download and load trades (updates OHLCV bars with trade flow metrics)
            if not download_and_load_trades(
                api_key,
                date_ranges['trades']['start'],
                date_ranges['trades']['end']
            ):
                raise Exception("Trades download/load failed")

            # Step 7: Print summary
            print_summary()

            # Step 8: Verify data integrity
            if not verify_data_loaded():
                raise Exception("Data verification failed - minimum bar counts not met")

            # All successful!
            reload_success = True

        except Exception as e:
            print(f"\n[ERROR] Reload failed: {e}")

            # Restore from backup if available
            if backup_path and backup_path.exists():
                print("\nAttempting to restore from backup...")
                if restore_database(backup_path):
                    print("[RESTORED] Database restored from backup")
                else:
                    print("[ERROR] Could not restore backup")
                    print(f"  Manual restore: cp {backup_path} /app/data/market_data.duckdb")
            else:
                print("\n[WARNING] No backup available for restore")

            return 1

        # Step 9: Clean up backup only if reload was successful (and not --keep-backup)
        if reload_success and backup_path and not args.keep_backup:
            cleanup_backup(backup_path)
            cleanup_old_backups(keep_count=2)  # Keep last 2 backups as safety
        elif args.keep_backup and backup_path:
            print(f"\n  Backup retained: {backup_path}")
            cleanup_old_backups(keep_count=3)  # Keep more backups when explicitly keeping

        print("\n" + "=" * 60)
        print("  Weekly Reload Complete!")
        print("  " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        print("=" * 60)

        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
