"""Shared constants and time helpers.

All timestamps in the store are NEM time: Australian Eastern Standard Time (UTC+10)
all year round, with no daylight saving. They are stored as naive datetimes.
"""

from datetime import datetime, timedelta, timezone

NEM_TZ = timezone(timedelta(hours=10))

# Region ids as AEMO publishes them. ACT is part of the NSW1 region.
REGIONS = {
    "NSW1": "New South Wales & ACT",
    "QLD1": "Queensland",
    "VIC1": "Victoria",
    "SA1": "South Australia",
    "TAS1": "Tasmania",
}

# AEMO's market day runs 04:00 to 04:00 NEM time.
MARKET_DAY_START_HOUR = 4

# Windows used for the "midday vs evening" comparison, as [start, end) hours of the
# interval start time in NEM time.
MIDDAY = (10, 15)
EVENING = (17, 21)

INTERVAL = timedelta(minutes=5)
INTERVALS_PER_DAY = 288


def market_day_of(settlement_ts):
    """Market day (a date) that a dispatch interval belongs to.

    AEMO stamps a dispatch interval with the time it ENDS, so the interval ending at
    04:05 is the first of its market day and the one ending at 04:00 the last.
    """
    return (settlement_ts - INTERVAL - timedelta(hours=MARKET_DAY_START_HOUR)).date()


def market_day_start(day):
    """Naive NEM-time datetime at which a market day begins (04:00)."""
    return datetime(day.year, day.month, day.day, MARKET_DAY_START_HOUR)


def now_nem():
    """Current NEM time as a naive datetime."""
    return datetime.now(timezone.utc).astimezone(NEM_TZ).replace(tzinfo=None)


def iso_nem(ts):
    """Format a naive NEM-time datetime as ISO 8601 with its +10:00 offset."""
    return ts.strftime("%Y-%m-%dT%H:%M:%S") + "+10:00"
