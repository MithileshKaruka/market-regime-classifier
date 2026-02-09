#!/usr/bin/env python3
"""
Zone Detection Diagnostic Tool

Analyzes why zone detection may be biased toward supply zones on higher timeframes.
Reports ERC counts, filtering statistics, and zone distribution.

Usage:
    python scripts/backtesting/diagnose_zone_bias.py
    python scripts/backtesting/diagnose_zone_bias.py --timeframe 4H
"""
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any
from collections import defaultdict

# Add backend directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage
from app.features.zone_bias import ZONE_PARAMS, ZoneType

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class FilterStats:
    """Statistics for zone filtering pipeline"""
    bullish_ercs: int = 0
    bearish_ercs: int = 0
    demand_no_base: int = 0  # Filtered: no boring base
    supply_no_base: int = 0
    demand_leg_in_too_short: int = 0  # Filtered: leg_in_bars < 2
    supply_leg_in_too_short: int = 0
    demand_weak_leg_in: int = 0  # Filtered: leg_in < min_departure
    supply_weak_leg_in: int = 0
    demand_zone_too_wide: int = 0  # Filtered: zone > 2x ATR
    supply_zone_too_wide: int = 0
    demand_weak_departure: int = 0  # Filtered: departure < min
    supply_weak_departure: int = 0
    demand_created: int = 0  # Final zones created
    supply_created: int = 0
    # Track actual values for debugging
    demand_widths: List[float] = None  # Zone widths in ATR units
    supply_widths: List[float] = None
    demand_departures: List[float] = None  # Departure values in ATR units
    supply_departures: List[float] = None

    def __post_init__(self):
        self.demand_widths = []
        self.supply_widths = []
        self.demand_departures = []
        self.supply_departures = []


def load_data(timeframe: str, symbol: str = "MNQ", limit: int = 500) -> pl.DataFrame:
    """Load historical data for zone detection"""
    db = DuckDBStorage()
    query = f"""
        SELECT
            timestamp,
            open, high, low, close, volume,
            dom_imbalance, cvd, instant_delta
        FROM ohlcv_ticks
        WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    with db as storage:
        df = storage.conn.execute(query).pl()
    return df.reverse() if len(df) > 0 else df


def diagnose_zone_detection(df: pl.DataFrame, timeframe: str) -> FilterStats:
    """Run zone detection with detailed diagnostics"""
    stats = FilterStats()
    atr_period = 14

    if len(df) < atr_period + 20:
        logger.warning(f"Insufficient data for {timeframe}: {len(df)} bars")
        return stats

    # Calculate candle metrics (same as zone_bias.py)
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).alias("candle_range"),
        (pl.col("close") - pl.col("open")).abs().alias("body_size"),
    ])
    df = df.with_columns([
        pl.col("candle_range").rolling_mean(window_size=atr_period).alias("atr"),
    ])

    rows = df.to_dicts()

    # Scan window by timeframe
    scan_bars_by_tf = {
        "5M": 3000,
        "15M": 4000,
        "1H": 4800,
        "4H": 1200,
        "1D": 200,
    }
    scan_bars = scan_bars_by_tf.get(timeframe, 500)
    scan_start = max(atr_period + 10, len(rows) - scan_bars)

    # Parameters
    erc_body_mult = ZONE_PARAMS["erc_body_multiplier"]
    boring_ratio = ZONE_PARAMS["boring_body_ratio"]
    min_base = ZONE_PARAMS["min_base_candles"]
    max_base = ZONE_PARAMS["max_base_candles"]
    min_departure = ZONE_PARAMS["min_departure_atr"]
    zone_extend = ZONE_PARAMS.get("zone_extend_candles", 1)
    max_zone_width = ZONE_PARAMS.get("max_zone_width_atr", 2.5)

    for i in range(scan_start, len(rows)):
        curr = rows[i]
        atr = curr.get("atr")

        if atr is None or atr <= 0:
            continue

        # Check if this is an ERC (Extended Range Candle)
        body = curr["body_size"]

        if body < atr * erc_body_mult:
            continue  # Not an ERC

        # Determine ERC direction
        is_bullish_erc = curr["close"] > curr["open"]
        is_bearish_erc = curr["close"] < curr["open"]

        if not is_bullish_erc and not is_bearish_erc:
            continue

        # Count ERCs
        if is_bullish_erc:
            stats.bullish_ercs += 1
        else:
            stats.bearish_ercs += 1

        # Look backwards for a "base" (1-8 boring candles)
        base_candles = []
        for j in range(i - 1, max(scan_start - 1, i - max_base - 1), -1):
            bar = rows[j]
            bar_body = bar["body_size"]
            bar_range = bar["candle_range"]

            # Check if boring candle (body < 60% of range)
            if bar_range > 0 and bar_body / bar_range < boring_ratio:
                base_candles.append(j)
            else:
                break  # No longer boring, stop looking

        # Track if this is a V-reversal (no boring base found)
        is_v_reversal = len(base_candles) < min_base

        # If no boring candles found, use 1-2 candles before ERC as base
        if is_v_reversal:
            if is_bullish_erc:
                stats.demand_no_base += 1
            else:
                stats.supply_no_base += 1

            # Fallback: use candles immediately before ERC
            for j in range(i - 1, max(scan_start - 1, i - 3), -1):
                base_candles.append(j)
            if len(base_candles) == 0:
                continue

        # Determine base start for leg-in calculation
        base_start_idx = base_candles[-1]  # Earliest base candle
        if base_start_idx < scan_start + 3:
            continue

        # Find swing point for leg-in validation (search 20 bars before base)
        swing_search_start = max(scan_start, base_start_idx - 20)
        swing_search_indices = list(base_candles) + list(range(swing_search_start, base_start_idx))

        # For V-reversals, include the ERC in swing search
        if is_v_reversal:
            swing_search_indices.append(i)

        if is_bullish_erc:
            # DEMAND zone: find the actual swing LOW for leg-in validation
            swing_low_idx = min(swing_search_indices, key=lambda k: rows[k]["low"])
            swing_low = rows[swing_low_idx]["low"]

            # Check for leg-in: high before swing low
            leg_in_bars = min(5, swing_low_idx - scan_start)
            if leg_in_bars < 2:
                stats.demand_leg_in_too_short += 1
                continue
            leg_check_start = max(scan_start, swing_low_idx - leg_in_bars)
            if leg_check_start >= swing_low_idx:
                continue
            high_before = max(rows[k]["high"] for k in range(leg_check_start, swing_low_idx))
            leg_in_move = (high_before - swing_low) / atr

            if leg_in_move < min_departure:
                stats.demand_weak_leg_in += 1
                continue  # Weak leg-in

            zone_type = ZoneType.DEMAND

            # Zone boundaries
            extended_base = [swing_low_idx]
            for offset in range(1, zone_extend + 1):
                if swing_low_idx - offset >= scan_start:
                    extended_base.append(swing_low_idx - offset)
                if swing_low_idx + offset < i:
                    extended_base.append(swing_low_idx + offset)
        else:
            # SUPPLY zone: find the actual swing HIGH for leg-in validation
            swing_high_idx = max(swing_search_indices, key=lambda k: rows[k]["high"])
            swing_high = rows[swing_high_idx]["high"]

            # Check for leg-in: low before swing high
            leg_in_bars = min(5, swing_high_idx - scan_start)
            if leg_in_bars < 2:
                stats.supply_leg_in_too_short += 1
                continue
            leg_check_start = max(scan_start, swing_high_idx - leg_in_bars)
            if leg_check_start >= swing_high_idx:
                continue
            low_before = min(rows[k]["low"] for k in range(leg_check_start, swing_high_idx))
            leg_in_move = (swing_high - low_before) / atr

            if leg_in_move < min_departure:
                stats.supply_weak_leg_in += 1
                continue  # Weak leg-in

            zone_type = ZoneType.SUPPLY

            # Zone boundaries
            extended_base = [swing_high_idx]
            for offset in range(1, zone_extend + 1):
                if swing_high_idx - offset >= scan_start:
                    extended_base.append(swing_high_idx - offset)
                if swing_high_idx + offset < i:
                    extended_base.append(swing_high_idx + offset)

        # Zone boundaries = high/low of the consolidation range
        zone_high = max(rows[k]["high"] for k in extended_base)
        zone_low = min(rows[k]["low"] for k in extended_base)
        zone_height = zone_high - zone_low

        # Cap zone height at max_zone_width * ATR to prevent overly wide zones
        if zone_height > atr * max_zone_width:
            if zone_type == ZoneType.DEMAND:
                # For demand, cap by raising zone_low (keep the swing low as lower bound)
                swing_low = rows[swing_low_idx]["low"]
                capped_height = atr * max_zone_width
                zone_low = swing_low
                zone_high = min(zone_high, swing_low + capped_height)
            else:
                # For supply, cap by lowering zone_high (keep the swing high as upper bound)
                swing_high = rows[swing_high_idx]["high"]
                capped_height = atr * max_zone_width
                zone_high = swing_high
                zone_low = max(zone_low, swing_high - capped_height)

            zone_height = zone_high - zone_low

        # Track zone width in ATR units (after capping)
        width_atr = zone_height / atr
        if zone_type == ZoneType.DEMAND:
            stats.demand_widths.append(width_atr)
        else:
            stats.supply_widths.append(width_atr)

        # Skip zones that are still too wide (shouldn't happen after capping)
        if zone_height > atr * max_zone_width:
            if zone_type == ZoneType.DEMAND:
                stats.demand_zone_too_wide += 1
            else:
                stats.supply_zone_too_wide += 1
            continue

        # Departure strength
        if zone_type == ZoneType.DEMAND:
            departure = (curr["close"] - zone_high) / atr
            stats.demand_departures.append(departure)
        else:
            departure = (zone_low - curr["close"]) / atr
            stats.supply_departures.append(departure)

        if departure < min_departure:
            if zone_type == ZoneType.DEMAND:
                stats.demand_weak_departure += 1
            else:
                stats.supply_weak_departure += 1
            continue

        # Zone created successfully
        if zone_type == ZoneType.DEMAND:
            stats.demand_created += 1
        else:
            stats.supply_created += 1

    return stats


def print_diagnostics(stats: FilterStats, timeframe: str):
    """Print detailed diagnostics"""
    print(f"\n{'='*70}")
    print(f"Zone Detection Diagnostics for {timeframe}")
    print(f"{'='*70}")

    print(f"\n1. ERC Detection (Extended Range Candles)")
    print(f"   Bullish ERCs: {stats.bullish_ercs}")
    print(f"   Bearish ERCs: {stats.bearish_ercs}")
    erc_ratio = stats.bullish_ercs / max(1, stats.bearish_ercs)
    print(f"   Ratio (Bull/Bear): {erc_ratio:.2f}")

    print(f"\n2. Base Detection Fallbacks (V-reversals)")
    print(f"   Demand (no boring base): {stats.demand_no_base}")
    print(f"   Supply (no boring base): {stats.supply_no_base}")

    print(f"\n3. Filtering Pipeline - DEMAND zones")
    print(f"   Filtered by leg-in too short: {stats.demand_leg_in_too_short}")
    print(f"   Filtered by weak leg-in:      {stats.demand_weak_leg_in}")
    print(f"   Filtered by zone too wide:    {stats.demand_zone_too_wide}")
    print(f"   Filtered by weak departure:   {stats.demand_weak_departure}")
    demand_filtered = (stats.demand_leg_in_too_short + stats.demand_weak_leg_in +
                       stats.demand_zone_too_wide + stats.demand_weak_departure)
    print(f"   Total filtered:               {demand_filtered}")
    print(f"   Final zones created:          {stats.demand_created}")

    print(f"\n4. Filtering Pipeline - SUPPLY zones")
    print(f"   Filtered by leg-in too short: {stats.supply_leg_in_too_short}")
    print(f"   Filtered by weak leg-in:      {stats.supply_weak_leg_in}")
    print(f"   Filtered by zone too wide:    {stats.supply_zone_too_wide}")
    print(f"   Filtered by weak departure:   {stats.supply_weak_departure}")
    supply_filtered = (stats.supply_leg_in_too_short + stats.supply_weak_leg_in +
                       stats.supply_zone_too_wide + stats.supply_weak_departure)
    print(f"   Total filtered:               {supply_filtered}")
    print(f"   Final zones created:          {stats.supply_created}")

    print(f"\n5. Summary")
    print(f"   Demand zones: {stats.demand_created}")
    print(f"   Supply zones: {stats.supply_created}")
    zone_ratio = stats.demand_created / max(1, stats.supply_created)
    print(f"   Ratio (Demand/Supply): {zone_ratio:.2f}")

    # Identify the biggest filter difference
    print(f"\n6. Asymmetry Analysis")
    diff_leg_short = stats.demand_leg_in_too_short - stats.supply_leg_in_too_short
    diff_weak_leg = stats.demand_weak_leg_in - stats.supply_weak_leg_in
    diff_wide = stats.demand_zone_too_wide - stats.supply_zone_too_wide
    diff_departure = stats.demand_weak_departure - stats.supply_weak_departure

    asymmetries = [
        ("Leg-in too short", diff_leg_short),
        ("Weak leg-in", diff_weak_leg),
        ("Zone too wide", diff_wide),
        ("Weak departure", diff_departure),
    ]

    for name, diff in sorted(asymmetries, key=lambda x: abs(x[1]), reverse=True):
        direction = "more demand filtered" if diff > 0 else "more supply filtered"
        if abs(diff) > 0:
            print(f"   {name}: {abs(diff)} {direction}")

    # Print actual value distributions
    max_zone_width = ZONE_PARAMS.get("max_zone_width_atr", 2.5)
    min_departure = ZONE_PARAMS["min_departure_atr"]

    print(f"\n7. Zone Width Distribution (in ATR units, max={max_zone_width})")
    if stats.demand_widths:
        avg_demand_width = sum(stats.demand_widths) / len(stats.demand_widths)
        max_demand_width_val = max(stats.demand_widths)
        print(f"   Demand zones: avg={avg_demand_width:.2f} ATR, max={max_demand_width_val:.2f} ATR")
        over_limit = sum(1 for w in stats.demand_widths if w > max_zone_width)
        print(f"   Demand > {max_zone_width} ATR (filtered): {over_limit}/{len(stats.demand_widths)}")
    else:
        print(f"   Demand zones: no data")

    if stats.supply_widths:
        avg_supply_width = sum(stats.supply_widths) / len(stats.supply_widths)
        max_supply_width_val = max(stats.supply_widths)
        print(f"   Supply zones: avg={avg_supply_width:.2f} ATR, max={max_supply_width_val:.2f} ATR")
        over_limit = sum(1 for w in stats.supply_widths if w > max_zone_width)
        print(f"   Supply > {max_zone_width} ATR (filtered): {over_limit}/{len(stats.supply_widths)}")
    else:
        print(f"   Supply zones: no data")

    print(f"\n8. Departure Strength Distribution (in ATR units, need >= {min_departure})")
    if stats.demand_departures:
        avg_demand_dep = sum(stats.demand_departures) / len(stats.demand_departures)
        min_demand_dep = min(stats.demand_departures)
        print(f"   Demand zones: avg={avg_demand_dep:.2f} ATR, min={min_demand_dep:.2f} ATR")
        under_limit = sum(1 for d in stats.demand_departures if d < min_departure)
        print(f"   Demand < {min_departure} ATR (filtered): {under_limit}/{len(stats.demand_departures)}")
    else:
        print(f"   Demand zones: no data")

    if stats.supply_departures:
        avg_supply_dep = sum(stats.supply_departures) / len(stats.supply_departures)
        min_supply_dep = min(stats.supply_departures)
        print(f"   Supply zones: avg={avg_supply_dep:.2f} ATR, min={min_supply_dep:.2f} ATR")
        under_limit = sum(1 for d in stats.supply_departures if d < min_departure)
        print(f"   Supply < {min_departure} ATR (filtered): {under_limit}/{len(stats.supply_departures)}")
    else:
        print(f"   Supply zones: no data")


def main():
    parser = argparse.ArgumentParser(description='Diagnose zone detection bias')
    parser.add_argument('--timeframe', '-t', default=None,
                        choices=['5M', '15M', '1H', '4H', '1D'],
                        help='Specific timeframe to test (default: all)')

    args = parser.parse_args()

    if args.timeframe:
        timeframes = [args.timeframe]
    else:
        timeframes = ['5M', '15M', '1H', '4H', '1D']

    for tf in timeframes:
        df = load_data(tf)
        print(f"\nLoaded {len(df)} bars for {tf}")

        if len(df) < 50:
            print(f"  Insufficient data for {tf}")
            continue

        stats = diagnose_zone_detection(df, tf)
        print_diagnostics(stats, tf)


if __name__ == '__main__':
    main()
