#!/usr/bin/env python
"""Download 100 days of trades data (Aug 26 - Dec 3, 2025)"""
import sys
import os

# Add backend to path - use absolute Windows path
backend_path = r"C:\Users\jthlbg2\market-regime-classifier\backend"
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

# Also set PYTHONPATH
os.environ["PYTHONPATH"] = backend_path

from scripts.data.preload_historical import download_and_load_trades_chunked
from config import get_secrets

if __name__ == "__main__":
    secrets = get_secrets()
    api_key = secrets.api_key

    print("Starting trades download for Aug 26 - Dec 3, 2025...")
    download_and_load_trades_chunked(
        api_key=api_key,
        start_date='2025-08-26',
        end_date='2025-12-03',
        hours_per_chunk=2
    )
    print("Done!")
