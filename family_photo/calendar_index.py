"""Calendar metadata only: no originals, thumbnails or per-photo JSON reads."""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=128)
def calendar_zone(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        raise ValueError('Invalid time zone')
    return ZoneInfo(name)


def month_key(stamp, zone):
    try:
        return datetime.fromtimestamp(stamp, calendar_zone(zone)).strftime('%Y-%m')
    except (ValueError, OverflowError, OSError):
        return None


def month_bounds(key, zone):
    year, month = map(int, key.split('-'))
    tz = calendar_zone(zone)
    start = datetime(year, month, 1, tzinfo=tz)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())
