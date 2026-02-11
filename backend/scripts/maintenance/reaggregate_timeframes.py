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


from datetime import timedelta as td


def _truncate_4h(ts) -> datetime:
    """Truncate timestamp to 4H CME session boundary."""
    shifted_hour = (ts.hour + 1) % 24
    bucket_hour = (shifted_hour // 4) * 4
    actual_hour = (bucket_hour - 1) % 24
    result = ts.replace(hour=actual_hour, minute=0, second=0, microsecond=0)
    if actual_hour == 23 and ts.hour < 23:
        result = result - td(days=1)
    return result


def _truncate_1d(ts) -> datetime:
    """Truncate timestamp to 1D CME session boundary (23:00 UTC)."""
    if ts.hour >= 23:
        return ts.replace(hour=23, minute=0, second=0, microsecond=0)
    else:
        return (ts - td(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)


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

        # Aggregate to target timeframe using CME-aligned timestamps
        if target_tf == '4H':
            target_df = source_df.with_columns([
                pl.col('timestamp').map_elements(_truncate_4h, return_dtype=pl.Datetime).alias('target_ts')
            ])
        elif target_tf == '1D':
            target_df = source_df.with_columns([
                pl.col('timestamp').map_elements(_truncate_1d, return_dtype=pl.Datetime).alias('target_ts')
            ])
        else:
            # Standard truncation for 15M, 1H
            target_df = source_df.with_columns([
                pl.col('timestamp').dt.truncate(f'{target_mins}m').alias('target_ts')
            ])

        target_df = target_df.group_by('target_ts').agg([
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


def check_bar_alignment():
    """Check current bar alignment for all timeframes"""
    print("\n" + "=" * 60)
    print("  Checking Bar Alignment")
    print("=" * 60)

    with DuckDBStorage() as storage:
        for tf in ['5M', '15M', '1H', '4H', '1D']:
            result = storage.conn.execute(f"""
                SELECT EXTRACT(HOUR FROM timestamp) as hour, COUNT(*) as cnt
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                GROUP BY hour
                ORDER BY hour
            """).fetchdf()

            print(f"\n{tf} bars by hour:")

            # Define expected hours for each timeframe (CME-aligned)
            if tf in ('5M', '15M', '1H'):
                expected = set(range(24))  # All hours valid
            elif tf == '4H':
                # CME 4H bars: 23:00, 03:00, 07:00, 11:00, 15:00, 19:00
                expected = {23, 3, 7, 11, 15, 19}
            elif tf == '1D':
                # CME daily session starts at 23:00 UTC
                expected = {23}

            actual = set(int(h) for h in result['hour'].tolist()) if len(result) > 0 else set()
            misaligned = actual - expected

            for _, row in result.iterrows():
                hour = int(row['hour'])
                cnt = int(row['cnt'])
                status = "OK" if hour in expected else "MISALIGNED"
                print(f"  Hour {hour:2d}: {cnt:,} bars  [{status}]")

            if misaligned:
                print(f"  -> ISSUE: Found bars at unexpected hours: {sorted(misaligned)}")
            else:
                print(f"  -> OK: All bars at expected hours")

    return len(misaligned) == 0


def main():
    parser = argparse.ArgumentParser(description='Re-aggregate higher timeframes from 5M data')
    parser.add_argument('--timeframe', '-t', choices=['15M', '1H', '4H', '1D', 'all'],
                        default='all', help='Timeframe to re-aggregate')
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--check', '-c', action='store_true',
                        help='Check bar alignment without making changes')

    args = parser.parse_args()

    if args.check:
        check_bar_alignment()
        return

    print("=" * 60)
    print("Re-aggregating Higher Timeframes from 5M Data")
    print("=" * 60)

    if args.timeframe == 'all':
        timeframes = ['15M', '1H', '4H', '1D']
    else:
        timeframes = [args.timeframe]

    for tf in timeframes:
        reaggregate_timeframe('5M', tf, dry_run=args.dry_run)

    print("\n  Verifying alignment...")
    check_bar_alignment()

    print("\nDone!")


if __name__ == '__main__':
    main()
