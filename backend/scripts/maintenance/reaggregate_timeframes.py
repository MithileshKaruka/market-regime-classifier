#!/usr/bin/env python
"""
Re-aggregate higher timeframes (15M, 1H, 4H, 1D) from 5M data.

This fixes gaps caused by independent aggregation during data downloads.
Uses existing 5M data to rebuild higher timeframe bars with proper continuity.

Usage:
    python scripts/maintenance/reaggregate_timeframes.py
    python scripts/maintenance/reaggregate_timeframes.py --timeframe 1H
    python scripts/maintenance/reaggregate_timeframes.py --dry-run
"""
import sys
from pathlib import Path
import argparse
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage


def reaggregate_timeframe(source_tf: str, target_tf: str, dry_run: bool = False):
    """Re-aggregate a higher timeframe from lower timeframe data.

    Args:
        source_tf: Source timeframe (e.g., '5M')
        target_tf: Target timeframe (e.g., '1H')
        dry_run: If True, just show what would be done
    """
    # Mapping of timeframe to minutes
    tf_minutes = {
        '5M': 5,
        '15M': 15,
        '1H': 60,
        '4H': 240,
        '1D': 1440,
    }

    source_mins = tf_minutes[source_tf]
    target_mins = tf_minutes[target_tf]

    if target_mins <= source_mins:
        print(f"Cannot aggregate {source_tf} to {target_tf} - target must be larger")
        return

    print(f"\nRe-aggregating {target_tf} from {source_tf}...")

    with DuckDBStorage() as storage:
        # Get source data
        source_df = storage.conn.execute(f"""
            SELECT *
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{source_tf}'
            ORDER BY timestamp
        """).pl()

        print(f"  Source bars ({source_tf}): {len(source_df):,}")

        if len(source_df) == 0:
            print("  No source data found!")
            return

        # Get existing target data count
        existing = storage.conn.execute(f"""
            SELECT COUNT(*) FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{target_tf}'
        """).fetchone()[0]
        print(f"  Existing {target_tf} bars: {existing:,}")

        # Aggregate to target timeframe
        # Truncate timestamp to target interval
        target_df = source_df.with_columns([
            pl.col('timestamp').dt.truncate(f'{target_mins}m').alias('target_ts')
        ]).group_by('target_ts').agg([
            pl.first('symbol'),
            pl.lit(target_tf).alias('timeframe'),
            pl.first('open'),
            pl.max('high'),
            pl.min('low'),
            pl.last('close'),
            pl.sum('volume'),
            # Orderflow metrics - sum deltas, average ratios
            pl.sum('instant_delta').alias('instant_delta'),
            pl.mean('dom_imbalance').alias('dom_imbalance'),
            pl.last('total_bid_depth').alias('total_bid_depth'),
            pl.last('total_ask_depth').alias('total_ask_depth'),
            pl.sum('cvd').alias('cvd'),
            # Trade flow metrics
            pl.mean('trade_flow_ratio').alias('trade_flow_ratio'),
            pl.sum('buy_trades').alias('buy_trades'),
            pl.sum('sell_trades').alias('sell_trades'),
            pl.sum('large_trade_count').alias('large_trade_count'),
        ]).rename({'target_ts': 'timestamp'}).sort('timestamp')

        print(f"  Aggregated {target_tf} bars: {len(target_df):,}")

        if dry_run:
            print("  [DRY RUN] Would replace existing data")
            # Show sample
            print(f"  Sample (first 3 bars):")
            for row in target_df.head(3).to_dicts():
                print(f"    {row['timestamp']}: O={row['open']:.2f} H={row['high']:.2f} L={row['low']:.2f} C={row['close']:.2f}")
            return

        # Delete existing data for this timeframe
        storage.conn.execute(f"""
            DELETE FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{target_tf}'
        """)

        # Reorder columns to match table schema
        target_df = target_df.select([
            'timestamp', 'symbol', 'timeframe', 'open', 'high', 'low', 'close', 'volume',
            'instant_delta', 'dom_imbalance', 'total_bid_depth', 'total_ask_depth', 'cvd',
            'trade_flow_ratio', 'buy_trades', 'sell_trades', 'large_trade_count'
        ])

        # Insert new aggregated data
        storage.conn.execute("""
            INSERT INTO ohlcv_ticks
            SELECT * FROM target_df
        """)

        # Verify
        new_count = storage.conn.execute(f"""
            SELECT COUNT(*) FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{target_tf}'
        """).fetchone()[0]

        print(f"  New {target_tf} bar count: {new_count:,}")
        print(f"  Change: {existing:,} -> {new_count:,} ({new_count - existing:+,})")


def main():
    parser = argparse.ArgumentParser(description='Re-aggregate higher timeframes from 5M data')
    parser.add_argument('--timeframe', '-t', choices=['15M', '1H', '4H', '1D', 'all'],
                        default='all', help='Timeframe to re-aggregate')
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show what would be done without making changes')

    args = parser.parse_args()

    print("=" * 60)
    print("Re-aggregating Higher Timeframes from 5M Data")
    print("=" * 60)

    if args.timeframe == 'all':
        timeframes = ['15M', '1H', '4H', '1D']
    else:
        timeframes = [args.timeframe]

    for tf in timeframes:
        reaggregate_timeframe('5M', tf, dry_run=args.dry_run)

    print("\nDone!")


if __name__ == '__main__':
    main()
