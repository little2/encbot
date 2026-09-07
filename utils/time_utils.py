from datetime import datetime, timedelta, timezone


APP_TIMEZONE = timezone(timedelta(hours=8))


def app_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def app_fromtimestamp(timestamp: int | float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=APP_TIMEZONE)
