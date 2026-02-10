#!/usr/bin/env python
"""
Fix DOM Imbalance values in database.

The old formula was: (bid - ask) / (bid + ask + 1) -> range -1 to +1 (0 = balanced)
The new formula is:  bid / (bid + ask + 1) -> range 0 to 1 (0.5 = balanced)

This script transforms existing values: new_dom = (old_dom + 1) / 2

Usage:
    python scripts/maintenance/fix_dom_imbalance.py
    python scripts/maintenance/fix_dom_imbalance.py --dry-run
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import argparse
from app.data.storage import DuckDBStorage


def fix_dom_imbalance(dry_run: bool = False):
    """Transform DOM imbalance from -1..+1 range to 0..1 range."""

    print("=" * 60)
    print("FIX DOM IMBALANCE VALUES")
    print("=" * 60)
    print("Transforming: new_dom = (old_dom + 1) / 2")
    print("  -1 to +1 range -> 0 to 1 range")
    print("  0 (balanced) -> 0.5 (balanced)")
    print()

    with DuckDBStorage() as storage:
        # Check current values
        result = storage.conn.execute("""
            SELECT
                timeframe,
                COUNT(*) as bar_count,
                MIN(dom_imbalance) as min_dom,
                MAX(dom_imbalance) as max_dom,
                AVG(dom_imbalance) as avg_dom
            FROM ohlcv_ticks
            WHERE dom_imbalance IS NOT NULL
            GROUP BY timeframe
            ORDER BY timeframe
        """).fetchall()

        print("Current DOM imbalance values:")
        print("-" * 60)
        print(f"{'Timeframe':<10} {'Bars':<10} {'Min':<12} {'Max':<12} {'Avg':<12}")
        print("-" * 60)
        for row in result:
            tf, count, min_dom, max_dom, avg_dom = row
            print(f"{tf:<10} {count:<10} {min_dom:<12.4f} {max_dom:<12.4f} {avg_dom:<12.4f}")
        print()

        # Check if already fixed (values should be in 0-1 range if fixed)
        check = storage.conn.execute("""
            SELECT MIN(dom_imbalance), MAX(dom_imbalance)
            FROM ohlcv_ticks
            WHERE dom_imbalance IS NOT NULL
        """).fetchone()

        min_val, max_val = check
        if min_val >= 0 and max_val <= 1:
            print("Data appears to already be in 0-1 range. Skipping fix.")
            return

        if dry_run:
            print("[DRY RUN] Would update all dom_imbalance values")
            print("Formula: dom_imbalance = (dom_imbalance + 1) / 2")

            # Show what values would become
            print("\nSample transformation:")
            sample = storage.conn.execute("""
                SELECT timestamp, timeframe, dom_imbalance,
                       (dom_imbalance + 1) / 2 as new_dom
                FROM ohlcv_ticks
                WHERE dom_imbalance IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 10
            """).fetchall()
            print(f"{'Timestamp':<22} {'TF':<6} {'Old DOM':<12} {'New DOM':<12}")
            print("-" * 52)
            for row in sample:
                ts, tf, old, new = row
                print(f"{str(ts)[:19]:<22} {tf:<6} {old:<12.4f} {new:<12.4f}")
            return

        # Apply the fix
        print("Applying fix...")
        storage.conn.execute("""
            UPDATE ohlcv_ticks
            SET dom_imbalance = (dom_imbalance + 1) / 2
            WHERE dom_imbalance IS NOT NULL
        """)
        storage.conn.commit()

        # Verify the fix
        result = storage.conn.execute("""
            SELECT
                timeframe,
                COUNT(*) as bar_count,
                MIN(dom_imbalance) as min_dom,
                MAX(dom_imbalance) as max_dom,
                AVG(dom_imbalance) as avg_dom
            FROM ohlcv_ticks
            WHERE dom_imbalance IS NOT NULL
            GROUP BY timeframe
            ORDER BY timeframe
        """).fetchall()

        print("\nFixed DOM imbalance values:")
        print("-" * 60)
        print(f"{'Timeframe':<10} {'Bars':<10} {'Min':<12} {'Max':<12} {'Avg':<12}")
        print("-" * 60)
        for row in result:
            tf, count, min_dom, max_dom, avg_dom = row
            print(f"{tf:<10} {count:<10} {min_dom:<12.4f} {max_dom:<12.4f} {avg_dom:<12.4f}")

        print("\nDone! DOM imbalance values have been fixed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix DOM imbalance values in database")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    fix_dom_imbalance(dry_run=args.dry_run)
