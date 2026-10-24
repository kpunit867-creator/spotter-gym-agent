"""Dates in the user's timezone."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# A "day" ends at 3 AM, so a 12:30 AM post-workout meal counts for the day before.
DAY_START_HOUR = 3


def user_now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def user_today(tz: str) -> date:
    return (user_now(tz) - timedelta(hours=DAY_START_HOUR)).date()
