"""Calendar helpers for the local job-search workflow."""

from datetime import date, datetime, timedelta, timezone


def today_local() -> date:
    from settings import profile
    from zoneinfo import ZoneInfo

    zone = profile().get("timezone")
    return datetime.now(ZoneInfo(zone) if zone else None).date()


def parse_day(value) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        result = date.fromisoformat(value)
        return result if result.isoformat() == value else None
    except ValueError:
        return None


def confirmed_day(value: str | None) -> str:
    day = parse_day(value or today_local().isoformat())
    if day is None or day > today_local():
        raise ValueError("Use a real, non-future YYYY-MM-DD event date")
    return day.isoformat()
