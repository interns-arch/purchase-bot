"""THE single source of truth for time handling in this application.

Business timezone: **Asia/Kolkata (IST, UTC+05:30)**. Everything a user sees --
Import History, notifications, dashboards, logs -- is IST.

Storage policy (Founder, 9 Sep 2026 -- this REVERSED the previous policy):
    The database keeps NAIVE **IST** timestamps.

    It always did in practice, and that is the bug this replaces. Every one
    of the 32 models defaults its timestamps with `server_default=func.now()`,
    which is evaluated by POSTGRES -- and `db/initdb/01-timezone.sql` sets the
    database timezone to Asia/Kolkata. So `func.now()` has been writing IST
    all along. The old docstring here claimed storage was UTC and that the
    "Postgres server TimeZone is GMT"; both were untrue on every deployment,
    and `to_ist()` therefore added 5h30 to values that were already IST. An
    order imported at 15:29 IST displayed as 20:59.

    Rather than rewrite ~132,000 inventory rows and every other historical
    timestamp to UTC, the convention now matches what the data actually is.
    The handful of columns that Python used to fill with UTC were converted
    once (see backend/scripts/backfill_ist_timestamps.py) and their writers
    now call `now_ist_naive()`.

    The one deliberate exception is `RevokedToken.expires_at`, which holds a
    JWT `exp` claim. That is UTC by definition, is compared only against
    other UTC values, and is never displayed -- so it stays UTC.

Ordering, duplicate detection, import activation/supersession and date
filtering are unaffected either way: they compare stored values against each
other, which is timezone-agnostic as long as ONE convention is used. What
matters is that reads and writes now agree.

Double-conversion safety: `to_ist()` never adds a fixed offset to an already
converted value. It attaches IST only to NAIVE inputs. Applying it twice is a
no-op, because the second call sees an aware IST value and `astimezone(IST)`
returns it unchanged.
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


def now_ist_naive() -> datetime:
    """Current IST time as a NAIVE datetime -- THE value to write to the
    database. Matches what Postgres `func.now()` writes for every column that
    uses `server_default=func.now()`, which is all of them."""
    return datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)


def utcnow_naive() -> datetime:
    """Current UTC time as a NAIVE datetime.

    NOT for database columns any more -- see the storage policy above. It
    remains for the one value that is genuinely UTC: a JWT `exp` claim, which
    is compared only against other UTC values and never displayed."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(value: datetime | None) -> datetime | None:
    """Convert a stored timestamp to an IST-aware datetime.

    A naive input is assumed to be IST (that is what this application stores
    -- see the storage policy above). An aware input is converted properly.
    Idempotent: calling this on a value that is already IST returns the same
    instant, so a timestamp can never be shifted by +05:30 twice.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
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
