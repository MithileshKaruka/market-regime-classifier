"""
Quick test to verify lookback values in the current codebase
"""
import sys
sys.path.insert(0, '.')

# Import the actual function
from app.api.regime import get_support_resistance
import inspect

source = inspect.getsource(get_support_resistance)

# Extract the 4H lookback value
import re
match = re.search(r"'4H':\s*(\d+)", source)
if match:
    lookback_4h = match.group(1)
    print(f"4H lookback in code: {lookback_4h}")
else:
    print("Could not find 4H lookback")

# Extract the 4H swing window
match = re.search(r"'4H':\s*(\d+),\s*#[^\n]*More sensitive", source)
if match:
    swing_4h = match.group(1)
    print(f"4H swing window in code: {swing_4h}")
else:
    print("Could not find 4H swing window")

# Test the logic directly
timeframe = '4H'
lookback = 0

timeframe_lookbacks = {
    '5M': 2000,
    '15M': 1500,
    '1H': 1000,
    '4H': 1000,
    '1D': 365,
}
lookback = timeframe_lookbacks.get(timeframe, 500)
print(f"\nDirect test: For timeframe={timeframe}, lookback={lookback}")

timeframe_swing_windows = {
    '5M': 3,
    '15M': 3,
    '1H': 4,
    '4H': 3,
    '1D': 5,
}
swing_window = timeframe_swing_windows.get(timeframe, 5)
print(f"Direct test: For timeframe={timeframe}, swing_window={swing_window}")
