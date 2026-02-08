#!/usr/bin/env python
"""
Backfill missing Sunday evening sessions.

The CME opens Sunday 5pm CT (22:00-23:00 UTC depending on DST).
Many weeks are missing this Sunday overnight -> Monday morning data.

Usage:
    python scripts/maintenance/backfill_sunday_sessions.py
    python scripts/maintenance/backfill_sunday_sessions.py --dry-run
"""
import sys
from pathlib import Path
import argparse
from datetime import datetime, timedelta
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from app.data.storage import DuckDBStorage
from config import get_secrets


def find_missing_sunday_sessions() -> List[Tuple[datetime, datetime]]:
    """Find Sundays where the evening session is missing."""

    with DuckDBStorage() as storage:
        # Find gaps that start on Friday and show Sunday morning gap
        result = storage.conn.execute('''
            WITH ordered AS (
                SELECT timestamp,
                       LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '5M'
            )
            SELECT prev_ts as gap_start, timestamp as gap_end
            FROM ordered
            WHERE EXTRACT(EPOCH FROM (timestamp - prev_ts))/3600 BETWEEN 35 AND 45
              AND EXTRACT(DOW FROM prev_ts) = 5  -- Friday
              AND EXTRACT(DOW FROM timestamp) = 0  -- Sunday
            ORDER BY prev_ts
        ''').fetchall()

    missing = []
    for row in result:
        # The Sunday session we need is from Sunday ~21:00 UTC to the gap_end
        gap_end = row[1]
        sunday_date = gap_end.date()
        # CME opens Sunday 5pm CT = ~22:00 UTC (winter) or ~21:00 UTC (summer)
        session_start = datetime(sunday_date.year, sunday_date.month, sunday_date.day, 21, 0)
        session_end = gap_end
        if session_end > session_start:
            missing.append((session_start, session_end))

    return missing


def download_mbp_for_range(api_key: str, start: datetime, end: datetime):
    """Download MBP-1 data for a specific range and aggregate to 5M bars."""

    print(f"  Downloading {start} to {end}...")

    client = db.Historical(api_key)

    try:
        data = client.timeseries.get_range(
            dataset="GLBX.MDP3",
            symbols=["MNQ.c.0"],
            stype_in="continuous",
            schema="mbp-1",
            start=start.strftime('%Y-%m-%dT%H:%M:%S'),
            end=end.strftime('%Y-%m-%dT%H:%M:%S'),
        )

        if data is None or len(data) == 0:
            print(f"    No data found")
            return None

        df = data.to_df()
        print(f"    Downloaded {len(df):,} MBP records")

        # Convert to polars and aggregate to 5M bars
        df_pl = pl.from_pandas(df.reset_index())

        # Aggregate to 5M OHLCV with orderflow metrics
        bars = df_pl.with_columns([
            pl.col('ts_event').dt.truncate('5m').alias('timestamp'),
        ]).group_by('timestamp').agg([
            pl.lit('MNQ').alias('symbol'),
            pl.lit('5M').alias('timeframe'),
            pl.first('price').alias('open') / 1e9,
            (pl.max('price') / 1e9).alias('high'),
            (pl.min('price') / 1e9).alias('low'),
            pl.last('price').alias('close') / 1e9,
            pl.sum('size').alias('volume'),
            # Orderflow: bid_px vs ask_px imbalance
            (pl.sum(pl.when(pl.col('side') == 'B').then(pl.col('size')).otherwise(0)) -
             pl.sum(pl.when(pl.col('side') == 'A').then(pl.col('size')).otherwise(0))).alias('instant_delta'),
            ((pl.col('bid_sz_00').mean() - pl.col('ask_sz_00').mean()) /
             (pl.col('bid_sz_00').mean() + pl.col('ask_sz_00').mean() + 1)).alias('dom_imbalance'),
            pl.last('bid_sz_00').alias('total_bid_depth'),
            pl.last('ask_sz_00').alias('total_ask_depth'),
        ]).sort('timestamp')

        # Add missing columns with nulls
        bars = bars.with_columns([
            pl.lit(0).cast(pl.Int64).alias('cvd'),
            pl.lit(None).cast(pl.Float64).alias('trade_flow_ratio'),
            pl.lit(None).cast(pl.Int32).alias('buy_trades'),
            pl.lit(None).cast(pl.Int32).alias('sell_trades'),
            pl.lit(None).cast(pl.Int32).alias('large_trade_count'),
        ])

        print(f"    Aggregated to {len(bars)} 5M bars")
        return bars

    except Exception as e:
        print(f"    Error: {e}")
        return None


def backfill_missing_sessions(dry_run: bool = False):
    """Find and backfill missing Sunday sessions."""

    print("=" * 60)
    print("Backfilling Missing Sunday Sessions")
    print("=" * 60)

    missing = find_missing_sunday_sessions()
    print(f"\nFound {len(missing)} missing Sunday sessions\n")

    if not missing:
        print("No missing sessions to backfill!")
        return

    for start, end in missing[:5]:  # Show first 5
        print(f"  {start} -> {end}")
    if len(missing) > 5:
        print(f"  ... and {len(missing) - 5} more")

    if dry_run:
        print("\n[DRY RUN] Would download data for these periods")
        return

    secrets = get_secrets()
    api_key = secrets.api_key

    total_bars = 0
    with DuckDBStorage() as storage:
        for i, (start, end) in enumerate(missing):
            print(f"\n[{i+1}/{len(missing)}] {start.date()}")

            bars = download_mbp_for_range(api_key, start, end)

            if bars is not None and len(bars) > 0:
                # Insert with upsert logic
                bars = bars.select([
                    'timestamp', 'symbol', 'timeframe', 'open', 'high', 'low', 'close', 'volume',
                    'instant_delta', 'dom_imbalance', 'total_bid_depth', 'total_ask_depth', 'cvd',
                    'trade_flow_ratio', 'buy_trades', 'sell_trades', 'large_trade_count'
                ])

                # Delete any existing bars in this range first
                storage.conn.execute(f"""
                    DELETE FROM ohlcv_ticks
                    WHERE symbol = 'MNQ' AND timeframe = '5M'
                    AND timestamp >= '{start}' AND timestamp < '{end}'
                """)

                storage.conn.execute("INSERT INTO ohlcv_ticks SELECT * FROM bars")
                total_bars += len(bars)
                print(f"    Inserted {len(bars)} bars")

    print(f"\n{'='*60}")
    print(f"Total bars inserted: {total_bars}")
    print("Done! Run reaggregate_timeframes.py to update higher timeframes.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill missing Sunday sessions')
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show what would be done without downloading')

    args = parser.parse_args()
    backfill_missing_sessions(dry_run=args.dry_run)
