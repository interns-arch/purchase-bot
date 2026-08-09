"""THE single source of truth for time handling in this application.

Business timezone: **Asia/Kolkata (IST, UTC+05:30)**. Everything a user sees --
Import History, notifications, dashboards, logs -- is IST.

Storage policy (deliberate, do not change casually):
    The database keeps NAIVE UTC timestamps, exactly as it always has.
    Nothing about ordering, duplicate detection, import activation/
    supersession or date filtering changes, because those all compare stored
    values against each other and are timezone-agnostic as long as one
    convention is used consistently. Migrating stored values to IST would
    silently shift every historical record by 5h30 and break comparisons
    against `func.now()` (Postgres server TimeZone is GMT), so we convert at
    the EDGE instead: on API serialization and on display.

Why the bug happened: a naive UTC value serialized to JSON as
"2026-08-09T05:38:29" carries no timezone marker, so JavaScript's
`new Date(...)` interprets it as *browser-local* time and prints 05:38 in an
IST browser -- 5h30 behind the true 11:08 IST.

Double-conversion safety: `to_ist()` never adds a fixed offset to an already
converted value. It attaches UTC only to NAIVE inputs, then converts. Applying
it twice is a no-op, because the second call sees an aware IST value and
`astimezone(IST)` returns it unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# IST does not observe daylight saving, so a fixed +05:30 offset is exact all
# year. ZoneInfo is preferred when the platform ships the IANA database
# (Linux/Render); the fixed offset keeps Windows dev machines working without
# the extra `tzdata` package.
try:  # pragma: no cover - platform dependent
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback, numerically identical
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

IST_LABEL = "IST"


def utcnow_naive() -> datetime:
    """Current UTC time as a NAIVE datetime -- the value written to the
    database. Identical to the `_utcnow()` helpers already used by the import
    services; kept here so there is one definition to point at."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(value: datetime | None) -> datetime | None:
    """Convert a stored timestamp to an IST-aware datetime.

    A naive input is assumed to be UTC (that is what this application stores).
    An aware input is converted properly. Idempotent: calling this on a value
    that is already IST returns the same instant, so a timestamp can never be
    shifted by +05:30 twice.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def ist_isoformat(value: datetime | None) -> str | None:
    """IST-aware ISO-8601 string, e.g. '2026-08-09T11:08:29.315733+05:30'.
    Unambiguous, so no consumer can misread it as local time."""
    converted = to_ist(value)
    return converted.isoformat() if converted is not None else None


def format_ist(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Human-readable IST, for logs and plain-text messages (emails, WhatsApp
    replies). Returns '-' for None."""
    converted = to_ist(value)
    return f"{converted.strftime(fmt)} {IST_LABEL}" if converted is not None else "-"


def now_ist() -> datetime:
    """Current time as an IST-aware datetime (display/logging use only --
    never write this to the database)."""
    return datetime.now(timezone.utc).astimezone(IST)
