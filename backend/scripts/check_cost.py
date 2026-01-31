"""Quick cost check for Databento data download"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

import databento as db

# Configuration - adjust these as needed
OHLCV_YEARS = 1       # 1 year of OHLCV data
MBP_DAYS = 3          # 3 days of MBP-1 data
DATASET = "GLBX.MDP3"
SYMBOL = "MNQ.c.0"    # Continuous front-month contract
STYPE_IN = "continuous"


def main():
    api_key = os.getenv("DATABENTO_API_KEY")
    if not api_key:
        print("ERROR: DATABENTO_API_KEY not found in .env file")
        sys.exit(1)

    print("=" * 60)
    print("  Databento Cost Check")
    print("=" * 60)

    # Calculate date ranges
    today = datetime.now(timezone.utc).date()
    ohlcv_start = today - timedelta(days=OHLCV_YEARS * 365)
    mbp_start = today - timedelta(days=MBP_DAYS)

    print(f"\nDate ranges:")
    print(f"  OHLCV: {ohlcv_start} to {today} ({OHLCV_YEARS} year)")
    print(f"  MBP-1: {mbp_start} to {today} ({MBP_DAYS} days)")

    client = db.Historical(api_key)

    # Check OHLCV cost
    print("\n" + "-" * 40)
    print("Checking OHLCV-1M cost...")
    try:
        ohlcv_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="ohlcv-1m",
            start=ohlcv_start.strftime('%Y-%m-%d'),
            end=today.strftime('%Y-%m-%d'),
        )
        print(f"  OHLCV-1M: ${ohlcv_cost:.2f}")
    except Exception as e:
        print(f"  ERROR: {e}")
        ohlcv_cost = None

    # Check MBP-1 cost
    print("\nChecking MBP-1 cost...")
    try:
        mbp_cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="mbp-1",
            start=mbp_start.strftime('%Y-%m-%d'),
            end=today.strftime('%Y-%m-%d'),
        )
        print(f"  MBP-1: ${mbp_cost:.2f}")
    except Exception as e:
        print(f"  ERROR: {e}")
        mbp_cost = None

    # Summary
    print("\n" + "=" * 60)
    if ohlcv_cost is not None and mbp_cost is not None:
        total = ohlcv_cost + mbp_cost
        print(f"  TOTAL COST: ${total:.2f}")

        if total == 0:
            print("\n  [OK] Data is cached - FREE to download!")
            print("  You can proceed with the weekly reload on EC2.")
        else:
            print(f"\n  [WARN] Download will cost ${total:.2f}")
            print("  Data is not fully cached in Databento.")
    else:
        print("  Could not determine total cost due to errors.")

    print("=" * 60)


if __name__ == "__main__":
    main()
