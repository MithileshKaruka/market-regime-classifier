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
import urllib.request
import urllib.error
import json
import os
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
from app.data.storage import DuckDBStorage
from config import get_secrets


# Database paths
def get_db_paths():
    """Get database file paths"""
    backend_dir = Path(__file__).parent.parent.parent
    data_dir = backend_dir / "data"
    return {
        'main': data_dir / "market_data.duckdb",
        'new': data_dir / "market_data_new.duckdb",
        'backup': data_dir / "market_data_backup.duckdb",
    }


# Live ingestion control API
BACKEND_URL = "http://localhost:8000"


def pause_live_ingestion() -> bool:
    """Pause live ingestion before data load

    Returns True if paused successfully, False if failed or already paused.
    """
    try:
        data = json.dumps({"reason": "historical_preload"}).encode('utf-8')
        req = urllib.request.Request(
            f"{BACKEND_URL}/api/admin/ingestion/pause",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode('utf-8'))
            if result.get("paused"):
                print("  Live ingestion PAUSED")
                return True
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # Already paused
            print("  Live ingestion already paused")
            return True
        print(f"  Warning: Could not pause ingestion (HTTP {e.code})")
    except urllib.error.URLError:
        print("  Note: Backend not running - no live ingestion to pause")
    except Exception as e:
        print(f"  Warning: Could not pause ingestion: {e}")
    return False


def resume_live_ingestion() -> bool:
    """Resume live ingestion after data load

    Returns True if resumed successfully, False if failed.
    """
    try:
        req = urllib.request.Request(
            f"{BACKEND_URL}/api/admin/ingestion/resume",
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode('utf-8'))
            if not result.get("paused"):
                print("  Live ingestion RESUMED")
                return True
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # Already running
            print("  Live ingestion already running")
            return True
        print(f"  Warning: Could not resume ingestion (HTTP {e.code})")
    except urllib.error.URLError:
        print("  Note: Backend not running - no ingestion to resume")
    except Exception as e:
        print(f"  Warning: Could not resume ingestion: {e}")
    return False


def verify_database(db_path: Path, date_ranges: dict) -> dict:
    """Verify loaded data in database

    Args:
        db_path: Path to database file
        date_ranges: Expected date ranges from get_date_ranges()

    Returns:
        Dict with verification results
    """
    import duckdb

    print(f"\nVerifying database: {db_path}")
    results = {
        'valid': True,
        'errors': [],
        'ohlcv': {},
        'summary': {},
    }

    if not db_path.exists():
        results['valid'] = False
        results['errors'].append(f"Database file not found: {db_path}")
        return results

    try:
        conn = duckdb.connect(str(db_path), read_only=True)

        # Check OHLCV data per timeframe
        timeframes = ['5M', '15M', '1H', '4H', '1D']
        for tf in timeframes:
            query = f"""
                SELECT
                    COUNT(*) as count,
                    MIN(timestamp) as min_ts,
                    MAX(timestamp) as max_ts
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            """
            result = conn.execute(query).fetchone()
            results['ohlcv'][tf] = {
                'count': result[0],
                'min_ts': str(result[1]) if result[1] else None,
                'max_ts': str(result[2]) if result[2] else None,
            }
            print(f"  {tf}: {result[0]:,} bars ({result[1]} to {result[2]})")

            # Validate minimum row counts
            min_expected = {'5M': 100, '15M': 50, '1H': 20, '4H': 5, '1D': 1}
            if result[0] < min_expected.get(tf, 1):
                results['errors'].append(f"{tf} has only {result[0]} bars (expected >= {min_expected[tf]})")
                results['valid'] = False

        # Total summary
        total_query = "SELECT COUNT(*) FROM ohlcv_ticks WHERE symbol = 'MNQ'"
        total_count = conn.execute(total_query).fetchone()[0]
        results['summary']['total_ohlcv'] = total_count
        print(f"  Total OHLCV bars: {total_count:,}")

        conn.close()

    except Exception as e:
        results['valid'] = False
        results['errors'].append(f"Database error: {e}")
        print(f"  ERROR: {e}")

    if results['valid']:
        print("  ✓ Verification PASSED")
    else:
        print(f"  ✗ Verification FAILED: {results['errors']}")

    return results


def copy_live_ingested_bars(source_db: Path, target_db: Path) -> dict:
    """Copy live-ingested bars from source DB to target DB

    Copies any bars from source that are newer than the latest bar in target.
    This preserves live-ingested data that fills the gap between historical
    data and the current time.

    Args:
        source_db: Current database with live-ingested bars
        target_db: New database with historical data

    Returns:
        Dict with copy results (bars_copied per timeframe)
    """
    import duckdb

    print(f"\nCopying live-ingested bars from current DB...")
    print(f"  Source: {source_db}")
    print(f"  Target: {target_db}")

    results = {
        'total_copied': 0,
        'by_timeframe': {},
    }

    if not source_db.exists():
        print("  Source DB not found - nothing to copy")
        return results

    if not target_db.exists():
        print("  Target DB not found - cannot copy")
        return results

    try:
        # Connect to both databases
        source_conn = duckdb.connect(str(source_db), read_only=True)
        target_conn = duckdb.connect(str(target_db))

        timeframes = ['5M', '15M', '1H', '4H', '1D']

        for tf in timeframes:
            # Get latest timestamp in target (historical data endpoint)
            target_max = target_conn.execute(f"""
                SELECT MAX(timestamp) FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            """).fetchone()[0]

            if target_max is None:
                print(f"  {tf}: No data in target - skipping")
                continue

            # Get bars from source that are newer than target's max
            newer_bars = source_conn.execute(f"""
                SELECT * FROM ohlcv_ticks
                WHERE symbol = 'MNQ'
                  AND timeframe = '{tf}'
                  AND timestamp > '{target_max}'
                ORDER BY timestamp
            """).fetchdf()

            if len(newer_bars) == 0:
                print(f"  {tf}: No newer bars to copy (latest: {target_max})")
                results['by_timeframe'][tf] = 0
                continue

            # Insert newer bars into target
            # Convert to polars for DuckDB insert
            import polars as pl
            df_newer = pl.from_pandas(newer_bars)

            target_conn.execute("INSERT OR REPLACE INTO ohlcv_ticks SELECT * FROM df_newer")
            target_conn.commit()

            copied_count = len(df_newer)
            results['by_timeframe'][tf] = copied_count
            results['total_copied'] += copied_count

            min_ts = newer_bars['timestamp'].min()
            max_ts = newer_bars['timestamp'].max()
            print(f"  {tf}: Copied {copied_count} bars ({min_ts} to {max_ts})")

        source_conn.close()
        target_conn.close()

        print(f"  Total bars copied: {results['total_copied']}")

    except Exception as e:
        print(f"  ERROR copying bars: {e}")
        import traceback
        traceback.print_exc()

    return results


def swap_databases() -> bool:
    """Swap new database with existing database

    This performs:
    1. Rename market_data.duckdb -> market_data_backup.duckdb
    2. Rename market_data_new.duckdb -> market_data.duckdb

    Returns True if swap succeeded, False if failed.
    """
    paths = get_db_paths()

    print("\nSwapping databases...")

    # Check new DB exists
    if not paths['new'].exists():
        print(f"  ERROR: New database not found: {paths['new']}")
        return False

    try:
        # Remove old backup if exists
        if paths['backup'].exists():
            print(f"  Removing old backup: {paths['backup']}")
            paths['backup'].unlink()

        # Backup current DB (if exists)
        if paths['main'].exists():
            print(f"  Backing up: {paths['main']} -> {paths['backup']}")
            shutil.move(str(paths['main']), str(paths['backup']))

        # Promote new DB
        print(f"  Promoting: {paths['new']} -> {paths['main']}")
        shutil.move(str(paths['new']), str(paths['main']))

        print("  ✓ Database swap completed!")
        return True

    except Exception as e:
        print(f"  ERROR during swap: {e}")

        # Try to restore backup if main DB was moved
        if not paths['main'].exists() and paths['backup'].exists():
            print("  Attempting to restore backup...")
            try:
                shutil.move(str(paths['backup']), str(paths['main']))
                print("  Backup restored")
            except Exception as restore_e:
                print(f"  CRITICAL: Could not restore backup: {restore_e}")

        return False


# Default configuration
DEFAULT_OHLCV_YEARS = 5
DEFAULT_MBP_DAYS = 60
DEFAULT_TRADES_DAYS = 14  # Trades data for institutional activity signals
DATASET = "GLBX.MDP3"
SYMBOL = "MNQ.c.0"  # Continuous front-month contract
STYPE_IN = "continuous"


def get_date_ranges(
    ohlcv_years: int = DEFAULT_OHLCV_YEARS,
    ohlcv_start: Optional[str] = None,
    mbp_days: int = DEFAULT_MBP_DAYS,
    trades_days: int = DEFAULT_TRADES_DAYS
) -> dict:
    """Calculate date ranges for data download

    Args:
        ohlcv_years: Years of OHLCV data to download
        ohlcv_start: Optional fixed start date for OHLCV
        mbp_days: Days of MBP-1 data to download
        trades_days: Days of trades data to download

    Returns:
        Dict with start/end dates for each schema
    """
    # Use yesterday as end date - Databento data has ~1 day delay
    today = datetime.now(timezone.utc).date()
    end_date = today - timedelta(days=1)

    # OHLCV: 5 years back (or custom start)
    if ohlcv_start:
        ohlcv_start_date = datetime.strptime(ohlcv_start, '%Y-%m-%d').date()
    else:
        ohlcv_start_date = end_date - timedelta(days=ohlcv_years * 365)

    # MBP-1: 60 days back
    mbp_start_date = end_date - timedelta(days=mbp_days)

    # Trades: 14 days back (for institutional activity signals)
    trades_start_date = end_date - timedelta(days=trades_days)

    return {
        'ohlcv': {
            'start': ohlcv_start_date.strftime('%Y-%m-%d'),
            'end': end_date.strftime('%Y-%m-%d'),
            'days': (end_date - ohlcv_start_date).days,
        },
        'mbp': {
            'start': mbp_start_date.strftime('%Y-%m-%d'),
            'end': end_date.strftime('%Y-%m-%d'),
            'days': (end_date - mbp_start_date).days,
        },
        'trades': {
            'start': trades_start_date.strftime('%Y-%m-%d'),
            'end': end_date.strftime('%Y-%m-%d'),
            'days': (end_date - trades_start_date).days,
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
            stype_in=STYPE_IN,
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
            stype_in=STYPE_IN,
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
            stype_in=STYPE_IN,
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
            stype_in=STYPE_IN,
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


def load_ohlcv_file(file_path: Path, db_path: Optional[Path] = None):
    """Load OHLCV file into database

    Args:
        file_path: Path to DBN file
        db_path: Optional custom database path (default: main database)
    """
    from scripts.data.load_historical_data import (
        load_ohlcv_from_dbn,
        filter_ohlcv_data,
        resample_to_timeframe,
        ensure_ohlcv_table,
        insert_ohlcv_data,
        TIMEFRAMES,
    )

    print(f"\nLoading OHLCV into database...")
    if db_path:
        print(f"  Target: {db_path}")

    with DuckDBStorage(db_path=str(db_path) if db_path else None) as storage:
        ensure_ohlcv_table(storage)

        # Clear existing OHLCV data for fresh import
        print("Clearing existing OHLCV data...")
        storage.conn.execute("DELETE FROM ohlcv_ticks WHERE symbol = 'MNQ'")
        storage.conn.commit()

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


def load_mbp_file(file_path: Path, chunk_size: int = 1_000_000):
    """Load MBP file into database - skipped, use download_and_load_mbp_chunked instead"""
    print(f"\nMBP file too large for single load: {file_path.stat().st_size / 1024**3:.2f} GB")
    print("  Use --mbp-days 7 to download smaller chunks")
    print("  Or run download_and_load_mbp_chunked() for streaming load")


def download_and_load_trades_chunked(api_key: str, start_date: str, end_date: str, hours_per_chunk: int = 4, db_path: Optional[Path] = None):
    """Download trades data and aggregate to update OHLCV bars with trade flow metrics

    This function downloads actual trade data (not quote-inferred) and updates
    existing OHLCV bars with accurate trade flow metrics:
    - trade_flow_ratio: proportion of buy vs sell volume
    - buy_trades: count of buy aggressor trades
    - sell_trades: count of sell aggressor trades
    - large_trade_count: count of institutional-sized trades (>=50 contracts)

    Args:
        api_key: Databento API key
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        hours_per_chunk: Hours per download chunk (default 4)
        db_path: Optional custom database path (default: main database)
    """
    import polars as pl
    import gc

    print(f"\nDownloading trades and updating OHLCV bars with trade flow metrics...")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Chunk size: {hours_per_chunk} hours")

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

    timeframes = {
        "5M": "5m",
        "15M": "15m",
        "1H": "1h",
        "4H": "4h",
        "1D": "1d",
    }

    LARGE_TRADE_THRESHOLD = 50  # contracts

    if db_path:
        print(f"  Target: {db_path}")

    with DuckDBStorage(db_path=str(db_path) if db_path else None) as storage:
        client = db.Historical(api_key)
        total_trades = 0
        total_bars_updated = 0
        chunk_num = 0
        current = start_dt

        while current < end_dt:
            chunk_num += 1
            chunk_end = min(current + timedelta(hours=hours_per_chunk), end_dt)

            print(f"\n  Chunk {chunk_num}: {current.strftime('%Y-%m-%d %H:%M')} to {chunk_end.strftime('%Y-%m-%d %H:%M')}...")

            try:
                # Download chunk directly to dataframe
                data = client.timeseries.get_range(
                    dataset=DATASET,
                    symbols=[SYMBOL],
                    stype_in=STYPE_IN,
                    schema="trades",
                    start=current.strftime('%Y-%m-%dT%H:%M:%S'),
                    end=chunk_end.strftime('%Y-%m-%dT%H:%M:%S'),
                )

                df = data.to_df()
                if len(df) == 0:
                    print(f"    No trades for this period")
                    current = chunk_end
                    continue

                # Reset index
                if hasattr(df, 'index'):
                    df = df.reset_index()
                    if 'index' in df.columns:
                        df = df.rename(columns={'index': 'ts_event'})

                total_trades += len(df)
                print(f"    Downloaded: {len(df):,} trades")

                # Convert to polars
                df_pl = pl.from_pandas(df)

                # Process trades: classify as buy or sell based on side field
                # 'A' = ask (buy aggressor), 'B' = bid (sell aggressor)
                df_trades = df_pl.with_columns([
                    pl.col("ts_event").alias("timestamp"),
                    pl.when(pl.col("side") == "A").then(1).otherwise(0).alias("is_buy"),
                    pl.when(pl.col("side") == "B").then(1).otherwise(0).alias("is_sell"),
                    pl.when(pl.col("size") >= LARGE_TRADE_THRESHOLD).then(1).otherwise(0).alias("is_large"),
                ])

                del df, data
                gc.collect()

                # Aggregate to each timeframe and update bars
                chunk_bars = 0
                for tf, duration in timeframes.items():
                    # Aggregate trade metrics per bar
                    df_agg = df_trades.group_by_dynamic(
                        "timestamp", every=duration, closed="left", label="left"
                    ).agg([
                        pl.col("is_buy").sum().alias("buy_trades"),
                        pl.col("is_sell").sum().alias("sell_trades"),
                        pl.col("is_large").sum().alias("large_trade_count"),
                    ]).with_columns([
                        # trade_flow_ratio: 0.0 = all sells, 1.0 = all buys
                        (pl.col("buy_trades") / (pl.col("buy_trades") + pl.col("sell_trades"))).fill_nan(0.5).alias("trade_flow_ratio"),
                    ])

                    # Update existing OHLCV bars with trade metrics
                    for row in df_agg.iter_rows(named=True):
                        storage.conn.execute("""
                            UPDATE ohlcv_ticks
                            SET trade_flow_ratio = ?,
                                buy_trades = ?,
                                sell_trades = ?,
                                large_trade_count = ?
                            WHERE timestamp = ?
                              AND symbol = 'MNQ'
                              AND timeframe = ?
                        """, [
                            row['trade_flow_ratio'],
                            row['buy_trades'],
                            row['sell_trades'],
                            row['large_trade_count'],
                            row['timestamp'],
                            tf
                        ])
                        chunk_bars += 1

                storage.conn.commit()
                total_bars_updated += chunk_bars
                print(f"    Updated: {chunk_bars} bars (total: {total_bars_updated})")

                del df_trades, df_agg
                gc.collect()

            except Exception as e:
                print(f"    Error: {e}")
                import traceback
                traceback.print_exc()

            current = chunk_end
            gc.collect()

        print(f"\n  Total trades processed: {total_trades:,}")
        print(f"  Total bars updated: {total_bars_updated}")
        print("  Done!")


def download_and_load_mbp_chunked(api_key: str, start_date: str, end_date: str, hours_per_chunk: int = 4, db_path: Optional[Path] = None):
    """Download MBP data and aggregate directly to OHLCV bars (memory efficient)

    Args:
        api_key: Databento API key
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        hours_per_chunk: Hours per download chunk (default 4)
        db_path: Optional custom database path (default: main database)
    """
    import polars as pl
    from scripts.data.load_historical_data import ensure_ohlcv_table
    import gc

    print(f"\nDownloading MBP-1 and aggregating to OHLCV bars...")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Chunk size: {hours_per_chunk} hours")
    print(f"  (Aggregating directly - not storing raw ticks)")

    if db_path:
        print(f"  Target: {db_path}")

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

    timeframes = {
        "5M": "5m",
        "15M": "15m",
        "1H": "1h",
        "4H": "4h",
        "1D": "1d",
    }

    with DuckDBStorage(db_path=str(db_path) if db_path else None) as storage:
        ensure_ohlcv_table(storage)

        client = db.Historical(api_key)
        total_bars = 0
        chunk_num = 0
        current = start_dt

        while current < end_dt:
            chunk_num += 1
            chunk_end = min(current + timedelta(hours=hours_per_chunk), end_dt)

            print(f"\n  Chunk {chunk_num}: {current.strftime('%Y-%m-%d %H:%M')} to {chunk_end.strftime('%Y-%m-%d %H:%M')}...")

            try:
                # Download chunk directly to dataframe (no file)
                data = client.timeseries.get_range(
                    dataset=DATASET,
                    symbols=[SYMBOL],
                    stype_in=STYPE_IN,
                    schema="mbp-1",
                    start=current.strftime('%Y-%m-%dT%H:%M:%S'),
                    end=chunk_end.strftime('%Y-%m-%dT%H:%M:%S'),
                )

                df = data.to_df()
                if len(df) == 0:
                    print(f"    No data for this period")
                    current = chunk_end
                    continue

                # Reset index
                if hasattr(df, 'index'):
                    df = df.reset_index()
                    if 'index' in df.columns:
                        df = df.rename(columns={'index': 'ts_event'})

                print(f"    Downloaded: {len(df):,} records")

                # Convert to polars and process
                df_pl = pl.from_pandas(df)
                df_processed = _process_mbp_chunk(df_pl)

                # Free original dataframes
                del df, df_pl, data

                # Aggregate directly to each timeframe using median-based filtering
                # This removes back-month contract quotes that cause long wicks
                chunk_bars = 0
                for tf, duration in timeframes.items():
                    # Two-pass approach: compute median per bar, filter outliers, then aggregate
                    df_with_bucket = df_processed.with_columns([
                        pl.col("timestamp").dt.truncate(duration).alias("bucket")
                    ])

                    medians = df_with_bucket.group_by("bucket").agg([
                        pl.col("mid_price").median().alias("median_price")
                    ])

                    # Join and filter quotes within 0.5% of median (removes back-month quotes)
                    df_filtered = df_with_bucket.join(medians, on="bucket", how="left").filter(
                        (pl.col("mid_price") - pl.col("median_price")).abs() / pl.col("median_price") < 0.005
                    )

                    # Now aggregate the filtered data
                    df_agg = df_filtered.group_by_dynamic(
                        "timestamp", every=duration, closed="left", label="left"
                    ).agg([
                        pl.col("mid_price").first().alias("open"),
                        pl.col("mid_price").max().alias("high"),
                        pl.col("mid_price").min().alias("low"),
                        pl.col("mid_price").last().alias("close"),
                        pl.len().alias("volume"),
                        pl.col("delta").sum().alias("instant_delta"),
                        pl.col("dom_imbalance").mean().alias("dom_imbalance"),
                        pl.col("bid_size").mean().cast(pl.Float64).alias("total_bid_depth"),
                        pl.col("ask_size").mean().cast(pl.Float64).alias("total_ask_depth"),
                    ]).with_columns([
                        pl.lit("MNQ").alias("symbol"),
                        pl.lit(tf).alias("timeframe"),
                        pl.lit(0).cast(pl.Int64).alias("cvd"),  # Will compute rolling later
                    ])

                    # Post-aggregation filter: remove bars with >3% range (relaxed to allow high volatility)
                    df_agg = df_agg.filter(
                        ((pl.col("high") - pl.col("low")) / pl.col("close") < 0.03)
                    )

                    # Reorder columns to match table schema (including trade flow columns as NULL)
                    df_insert = df_agg.select([
                        "timestamp", "symbol", "timeframe", "open", "high", "low", "close",
                        "volume", "instant_delta", "dom_imbalance", "total_bid_depth",
                        "total_ask_depth", "cvd",
                        pl.lit(None).cast(pl.Float64).alias("trade_flow_ratio"),
                        pl.lit(None).cast(pl.Int32).alias("buy_trades"),
                        pl.lit(None).cast(pl.Int32).alias("sell_trades"),
                        pl.lit(None).cast(pl.Int32).alias("large_trade_count"),
                    ])

                    if len(df_insert) > 0:
                        storage.conn.execute("INSERT OR REPLACE INTO ohlcv_ticks SELECT * FROM df_insert")
                        chunk_bars += len(df_insert)

                storage.conn.commit()
                total_bars += chunk_bars
                print(f"    Aggregated: {chunk_bars} bars (total: {total_bars})")

                # Free memory aggressively
                del df_processed
                gc.collect()

            except Exception as e:
                print(f"    Error: {e}")
                import traceback
                traceback.print_exc()

            current = chunk_end
            gc.collect()

        print(f"\n  Total bars created: {total_bars}")
        print("  Note: CVD is calculated per-chunk; cross-day CVD continuity requires post-processing")
        print("  Done!")


def _process_mbp_chunk(df: "pl.DataFrame") -> "pl.DataFrame":
    """Process a chunk of MBP data to ticks format"""
    import polars as pl

    symbol = "MNQ"

    # Cast size columns to signed int to allow subtraction/negation
    df = df.with_columns([
        pl.col("bid_sz_00").cast(pl.Int64).alias("bid_sz_00"),
        pl.col("ask_sz_00").cast(pl.Int64).alias("ask_sz_00"),
    ])

    # Calculate mid price and spread
    df = df.with_columns([
        ((pl.col("bid_px_00") + pl.col("ask_px_00")) / 2).alias("mid_price"),
        (pl.col("ask_px_00") - pl.col("bid_px_00")).alias("spread"),
    ])

    # Filter out bad quotes: wide spreads (>0.5% of price) or prices outside reasonable range
    df = df.filter(
        (pl.col("spread") / pl.col("mid_price") < 0.005) &  # Spread < 0.5%
        (pl.col("mid_price") > 10000) &  # Min MNQ price
        (pl.col("mid_price") < 50000) &  # Max MNQ price
        (pl.col("bid_px_00") > 0) &
        (pl.col("ask_px_00") > 0)
    )

    # Calculate delta from size changes
    df = df.with_columns([
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

    # Calculate DOM imbalance
    df = df.with_columns([
        (pl.col("bid_sz_00") / (pl.col("bid_sz_00") + pl.col("ask_sz_00"))).alias("dom_imbalance")
    ])

    # CVD will be recalculated during aggregation
    df = df.with_columns([
        pl.lit(0).cast(pl.Int64).alias("cvd")
    ])

    # Select final columns
    df_ticks = df.select([
        pl.col("ts_event").alias("timestamp"),
        pl.lit(symbol).alias("symbol"),
        pl.col("mid_price"),
        pl.col("bid_px_00").alias("bid_price"),
        pl.col("ask_px_00").alias("ask_price"),
        pl.col("spread"),
        pl.col("bid_sz_00").alias("bid_size"),
        pl.col("ask_sz_00").alias("ask_size"),
        pl.col("bid_sz_00").alias("total_bid_depth"),
        pl.col("ask_sz_00").alias("total_ask_depth"),
        pl.col("dom_imbalance"),
        pl.col("delta"),
        pl.col("cvd"),
    ])

    return df_ticks


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
    parser.add_argument('--trades-only', action='store_true',
                        help='Only download trades data')
    parser.add_argument('--ohlcv-years', type=int, default=DEFAULT_OHLCV_YEARS,
                        help=f'Years of OHLCV data (default: {DEFAULT_OHLCV_YEARS})')
    parser.add_argument('--ohlcv-start', type=str,
                        help='Fixed start date for OHLCV (YYYY-MM-DD)')
    parser.add_argument('--mbp-days', type=int, default=DEFAULT_MBP_DAYS,
                        help=f'Days of MBP-1 data (default: {DEFAULT_MBP_DAYS})')
    parser.add_argument('--trades-days', type=int, default=DEFAULT_TRADES_DAYS,
                        help=f'Days of trades data (default: {DEFAULT_TRADES_DAYS})')
    parser.add_argument('--keep-files', action='store_true',
                        help='Keep downloaded DBN files after loading')
    parser.add_argument('--swap-db', action='store_true',
                        help='Load into new DB file, verify, then swap (minimizes downtime)')
    parser.add_argument('--download-only', action='store_true',
                        help='Only download historical data into new DB (no copy, no swap)')
    parser.add_argument('--copy-and-swap', action='store_true',
                        help='Only copy ingestion data from current DB to new DB, then swap')

    args = parser.parse_args()

    if not args.estimate and not args.load and not args.copy_and_swap:
        parser.print_help()
        return

    print("=" * 60)
    print("  Historical Data Preload Utility")
    print("=" * 60)

    db_paths = get_db_paths()

    # Handle --copy-and-swap mode (no download needed)
    if args.copy_and_swap:
        print("\n*** COPY-AND-SWAP MODE ***")
        print("Copying ingestion data from current DB to new DB, then swapping")

        # Check new DB exists
        if not db_paths['new'].exists():
            print(f"\n[ERROR] New database not found: {db_paths['new']}")
            print("Run --download-only first to create the new database with historical data")
            return

        # Verify new DB before proceeding
        print("\n" + "=" * 60)
        print("  Verifying New Database")
        print("=" * 60)
        verification = verify_database(db_paths['new'], {})

        if not verification['valid']:
            print("\n*** VERIFICATION FAILED ***")
            print("New database did not pass verification.")
            return

        # Copy live-ingested bars
        print("\n" + "=" * 60)
        print("  Copying Live-Ingested Bars")
        print("=" * 60)
        copy_result = copy_live_ingested_bars(db_paths['main'], db_paths['new'])

        if copy_result['total_copied'] > 0:
            print(f"\n  Copied {copy_result['total_copied']} live-ingested bars to new DB")

        # Swap databases
        print("\n" + "=" * 60)
        print("  Swapping Databases (pausing ingestion briefly)")
        print("=" * 60)

        print("\nPausing live ingestion for swap...")
        pause_live_ingestion()

        try:
            if swap_databases():
                print("\n*** Database swap successful! ***")
            else:
                print("\n*** Database swap FAILED ***")
                print("Check logs above for details.")
                return
        finally:
            print("\nResuming live ingestion...")
            resume_live_ingestion()

        print("\n" + "=" * 60)
        print("  Copy and Swap Complete!")
        print("=" * 60)
        return

    # Get API key (needed for estimate and load)
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
        mbp_days=args.mbp_days,
        trades_days=args.trades_days
    )

    print(f"\nData ranges:")
    print(f"  OHLCV-1M: {date_ranges['ohlcv']['start']} to {date_ranges['ohlcv']['end']} ({date_ranges['ohlcv']['days']} days)")
    print(f"  MBP-1:    {date_ranges['mbp']['start']} to {date_ranges['mbp']['end']} ({date_ranges['mbp']['days']} days)")
    print(f"  Trades:   {date_ranges['trades']['start']} to {date_ranges['trades']['end']} ({date_ranges['trades']['days']} days)")

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

        if not args.ohlcv_only and not args.mbp_only and not args.trades_only:
            print("\nThis will download:")
            print(f"  - {date_ranges['ohlcv']['days']} days of OHLCV-1M data")
            print(f"  - {date_ranges['mbp']['days']} days of MBP-1 data")
            print(f"  - {date_ranges['trades']['days']} days of trades data")
        elif args.ohlcv_only:
            print(f"\nThis will download {date_ranges['ohlcv']['days']} days of OHLCV-1M data")
        elif args.mbp_only:
            print(f"\nThis will download {date_ranges['mbp']['days']} days of MBP-1 data")
        elif args.trades_only:
            print(f"\nThis will download {date_ranges['trades']['days']} days of trades data")

        print("\nNote: Run with --estimate first to check costs")

        if args.download_only:
            print("\n*** DOWNLOAD-ONLY MODE ***")
            print("Data will be loaded into a NEW database file and verified.")
            print("No copy or swap will be performed.")
            print("Run --copy-and-swap later to copy ingestion data and swap.")
        elif args.swap_db:
            print("\n*** SWAP-DB MODE ***")
            print("Data will be loaded into a NEW database file, verified,")
            print("then swapped with the existing database (minimal downtime)")

        response = input("\nProceed? [y/N]: ").strip().lower()
        if response != 'y':
            print("Aborted.")
            return

        # Setup output directory
        output_dir = Path(__file__).parent.parent.parent / "data"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Determine target database
        use_new_db = args.swap_db or args.download_only
        target_db = db_paths['new'] if use_new_db else None

        if use_new_db:
            # Remove any leftover new DB from previous failed run
            if db_paths['new'].exists():
                print(f"\nRemoving leftover new database: {db_paths['new']}")
                db_paths['new'].unlink()

            print(f"\n*** Loading data into NEW database: {target_db}")
        else:
            # Traditional mode: pause ingestion for entire load
            print("\nPausing live ingestion...")
            pause_live_ingestion()

        try:
            # Download and load OHLCV
            if not args.mbp_only and not args.trades_only:
                ohlcv_path = download_ohlcv(
                    api_key,
                    date_ranges['ohlcv']['start'],
                    date_ranges['ohlcv']['end'],
                    output_dir
                )
                if ohlcv_path:
                    load_ohlcv_file(ohlcv_path, db_path=target_db)
                    if not args.keep_files:
                        ohlcv_path.unlink()
                        print(f"  Cleaned up: {ohlcv_path.name}")

            # Download and load MBP-1 (chunked to manage memory)
            if not args.ohlcv_only and not args.trades_only:
                download_and_load_mbp_chunked(
                    api_key,
                    date_ranges['mbp']['start'],
                    date_ranges['mbp']['end'],
                    hours_per_chunk=4,  # 4-hour chunks for 8GB RAM
                    db_path=target_db
                )

            # Download and load trades (for institutional activity signals)
            if not args.ohlcv_only and not args.mbp_only:
                download_and_load_trades_chunked(
                    api_key,
                    date_ranges['trades']['start'],
                    date_ranges['trades']['end'],
                    hours_per_chunk=4,  # 4-hour chunks for memory management
                    db_path=target_db
                )

            # Post-download actions depend on mode
            if args.download_only:
                # Download-only: verify and stop
                print("\n" + "=" * 60)
                print("  Verifying New Database")
                print("=" * 60)
                verification = verify_database(target_db, date_ranges)

                if not verification['valid']:
                    print("\n*** VERIFICATION FAILED ***")
                    print("New database did not pass verification.")
                    print(f"New database kept at: {target_db}")
                else:
                    print("\n*** DOWNLOAD COMPLETE ***")
                    print(f"Historical data ready at: {target_db}")
                    print("\nNext steps:")
                    print("  1. Wait for CME maintenance window")
                    print("  2. Run: --copy-and-swap to copy ingestion data and swap")

            elif args.swap_db:
                # Full swap-db workflow: verify, copy, swap
                print("\n" + "=" * 60)
                print("  Verifying New Database")
                print("=" * 60)
                verification = verify_database(target_db, date_ranges)

                if not verification['valid']:
                    print("\n*** VERIFICATION FAILED ***")
                    print("New database did not pass verification.")
                    print("Keeping existing database unchanged.")
                    print(f"New database kept at: {target_db}")
                    return

                # Copy live-ingested bars from current DB to new DB
                print("\n" + "=" * 60)
                print("  Copying Live-Ingested Bars")
                print("=" * 60)
                copy_result = copy_live_ingested_bars(db_paths['main'], target_db)

                if copy_result['total_copied'] > 0:
                    print(f"\n  Copied {copy_result['total_copied']} live-ingested bars to new DB")

                # Swap databases (brief pause for file operations only)
                print("\n" + "=" * 60)
                print("  Swapping Databases (pausing ingestion briefly)")
                print("=" * 60)

                print("\nPausing live ingestion for swap...")
                pause_live_ingestion()

                try:
                    if swap_databases():
                        print("\n*** Database swap successful! ***")
                    else:
                        print("\n*** Database swap FAILED ***")
                        print("Check logs above for details.")
                        return
                finally:
                    print("\nResuming live ingestion...")
                    resume_live_ingestion()
            else:
                # Traditional mode: print summary from main DB
                print_summary()

        finally:
            if not args.swap_db and not args.download_only:
                # Traditional mode: resume ingestion
                print("\nResuming live ingestion...")
                resume_live_ingestion()

        print("\n" + "=" * 60)
        print("  Preload Complete!")
        print("=" * 60)


if __name__ == "__main__":
    main()
