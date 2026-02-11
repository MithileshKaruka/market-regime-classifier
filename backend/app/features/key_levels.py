"""
Key Levels Calculator

Calculates key reference price levels:
- Monthly Open (MO)
- Weekly Open (WO)
- Yearly Open (YO)
- Monday High (MDAY-H)
- Monday Low (MDAY-L)
- Previous Week High (PWH)
- Previous Week Low (PWL)

All levels use CME session boundaries (23:00 UTC = 18:00 ET).
"""
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass
import polars as pl


@dataclass
class KeyLevel:
    """A key reference price level"""
    name: str
    short_name: str
    price: float
    timestamp: datetime  # When this level was set
    color: str  # Suggested color for charting


@dataclass
class KeyLevels:
    """Collection of key price levels"""
    monthly_open: Optional[KeyLevel] = None
    weekly_open: Optional[KeyLevel] = None
    yearly_open: Optional[KeyLevel] = None
    monday_high: Optional[KeyLevel] = None
    monday_low: Optional[KeyLevel] = None
    prev_week_high: Optional[KeyLevel] = None
    prev_week_low: Optional[KeyLevel] = None


class KeyLevelsCalculator:
    """Calculate key reference price levels from OHLCV data"""

    # CME session starts at 23:00 UTC (18:00 ET) on Sunday
    CME_SESSION_HOUR = 23

    def __init__(self):
        pass

    def _get_cme_week_start(self, dt: datetime) -> datetime:
        """Get CME week start (Sunday 23:00 UTC) for a given datetime.

        CME futures week runs Sunday 18:00 ET to Friday 17:00 ET.
        In UTC: Sunday 23:00 to Friday 22:00.
        """
        # Find most recent Sunday
        days_since_sunday = dt.weekday() + 1  # Monday=0, Sunday=6 -> +1
        if days_since_sunday == 7:
            days_since_sunday = 0

        # If we're before Sunday 23:00 UTC, go back to previous week
        if days_since_sunday == 0 and dt.hour < self.CME_SESSION_HOUR:
            days_since_sunday = 7

        sunday = dt - timedelta(days=days_since_sunday)
        return sunday.replace(hour=self.CME_SESSION_HOUR, minute=0, second=0, microsecond=0)

    def _get_cme_month_start(self, dt: datetime) -> datetime:
        """Get CME month start (first Sunday 23:00 UTC of the month, or last Sunday of prev month)."""
        # Start of calendar month
        first_of_month = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Find the first Sunday at or after the 1st
        days_until_sunday = (6 - first_of_month.weekday()) % 7
        if days_until_sunday == 0 and first_of_month.weekday() != 6:
            days_until_sunday = 7

        first_sunday = first_of_month + timedelta(days=days_until_sunday)

        # If we're before that Sunday's session, use previous month's last Sunday
        first_sunday_session = first_sunday.replace(hour=self.CME_SESSION_HOUR)
        if dt < first_sunday_session:
            # Go back to find last Sunday of previous month
            last_of_prev_month = first_of_month - timedelta(days=1)
            days_since_sunday = (last_of_prev_month.weekday() + 1) % 7
            prev_sunday = last_of_prev_month - timedelta(days=days_since_sunday)
            return prev_sunday.replace(hour=self.CME_SESSION_HOUR, minute=0, second=0, microsecond=0)

        return first_sunday_session

    def _get_cme_year_start(self, dt: datetime) -> datetime:
        """Get CME year start (January 1st 00:00 UTC).

        Returns Jan 1 00:00 - the first bar on or after this is the yearly open.
        """
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def _get_monday_bounds(self, dt: datetime) -> tuple[datetime, datetime]:
        """Get Monday session bounds (Sunday 23:00 UTC to Monday 22:00 UTC).

        CME Monday session:
        - Starts: Sunday 18:00 ET (23:00 UTC)
        - Ends: Monday 17:00 ET (22:00 UTC)
        """
        week_start = self._get_cme_week_start(dt)
        # Monday session is from Sunday 23:00 to Monday ~22:00
        monday_end = week_start + timedelta(hours=23)  # Sunday 23:00 + 23h = Monday 22:00
        return week_start, monday_end

    def _get_prev_week_bounds(self, dt: datetime) -> tuple[datetime, datetime]:
        """Get previous week session bounds."""
        current_week_start = self._get_cme_week_start(dt)
        prev_week_start = current_week_start - timedelta(days=7)
        prev_week_end = current_week_start - timedelta(minutes=1)  # End just before current week
        return prev_week_start, prev_week_end

    def calculate(self, df: pl.DataFrame, current_time: Optional[datetime] = None) -> KeyLevels:
        """Calculate all key levels from OHLCV data.

        Args:
            df: DataFrame with timestamp, open, high, low, close columns
            current_time: Reference time (defaults to latest bar timestamp)

        Returns:
            KeyLevels with all calculated levels
        """
        if len(df) == 0:
            return KeyLevels()

        # Use latest timestamp if not provided
        if current_time is None:
            current_time = df["timestamp"].max()
            if hasattr(current_time, "to_pydatetime"):
                current_time = current_time.to_pydatetime()

        levels = KeyLevels()

        # Weekly Open
        week_start = self._get_cme_week_start(current_time)
        wo_bar = df.filter(pl.col("timestamp") >= week_start).sort("timestamp").head(1)
        if len(wo_bar) > 0:
            levels.weekly_open = KeyLevel(
                name="Weekly Open",
                short_name="WO",
                price=float(wo_bar["open"][0]),
                timestamp=week_start,
                color="#3b82f6"  # Blue
            )

        # Monthly Open
        month_start = self._get_cme_month_start(current_time)
        mo_bar = df.filter(pl.col("timestamp") >= month_start).sort("timestamp").head(1)
        if len(mo_bar) > 0:
            levels.monthly_open = KeyLevel(
                name="Monthly Open",
                short_name="MO",
                price=float(mo_bar["open"][0]),
                timestamp=month_start,
                color="#8b5cf6"  # Purple
            )

        # Yearly Open
        year_start = self._get_cme_year_start(current_time)
        yo_bar = df.filter(pl.col("timestamp") >= year_start).sort("timestamp").head(1)
        if len(yo_bar) > 0:
            levels.yearly_open = KeyLevel(
                name="Yearly Open",
                short_name="YO",
                price=float(yo_bar["open"][0]),
                timestamp=year_start,
                color="#ec4899"  # Pink
            )

        # Monday High/Low
        monday_start, monday_end = self._get_monday_bounds(current_time)
        monday_bars = df.filter(
            (pl.col("timestamp") >= monday_start) & (pl.col("timestamp") <= monday_end)
        )
        if len(monday_bars) > 0:
            monday_high = float(monday_bars["high"].max())
            monday_low = float(monday_bars["low"].min())

            levels.monday_high = KeyLevel(
                name="Monday High",
                short_name="MDAY-H",
                price=monday_high,
                timestamp=monday_start,
                color="#22c55e"  # Green
            )
            levels.monday_low = KeyLevel(
                name="Monday Low",
                short_name="MDAY-L",
                price=monday_low,
                timestamp=monday_start,
                color="#ef4444"  # Red
            )

        # Previous Week High/Low
        prev_week_start, prev_week_end = self._get_prev_week_bounds(current_time)
        prev_week_bars = df.filter(
            (pl.col("timestamp") >= prev_week_start) & (pl.col("timestamp") <= prev_week_end)
        )
        if len(prev_week_bars) > 0:
            prev_week_high = float(prev_week_bars["high"].max())
            prev_week_low = float(prev_week_bars["low"].min())

            levels.prev_week_high = KeyLevel(
                name="Previous Week High",
                short_name="PWH",
                price=prev_week_high,
                timestamp=prev_week_start,
                color="#f97316"  # Orange
            )
            levels.prev_week_low = KeyLevel(
                name="Previous Week Low",
                short_name="PWL",
                price=prev_week_low,
                timestamp=prev_week_start,
                color="#f97316"  # Orange
            )

        return levels

    def to_dict(self, levels: KeyLevels) -> dict:
        """Convert KeyLevels to dictionary format for API response."""
        result = {}

        for attr in ['monthly_open', 'weekly_open', 'yearly_open',
                     'monday_high', 'monday_low', 'prev_week_high', 'prev_week_low']:
            level = getattr(levels, attr)
            if level:
                result[attr] = {
                    "name": level.name,
                    "short_name": level.short_name,
                    "price": level.price,
                    "timestamp": int(level.timestamp.timestamp()),
                    "color": level.color
                }

        return result
