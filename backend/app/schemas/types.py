"""Shared field types for API schemas.

`IstDateTime` is the ONE place API timestamp serialization is defined. Any
schema field declared with it is emitted as an IST-aware ISO-8601 string
(e.g. "2026-08-09T11:08:29.315733+05:30") instead of the previous naive
"2026-08-09T05:38:29.315733", which JavaScript silently misread as local time.

The database holds naive **IST** (Founder, 9 Sep 2026 -- see the storage
policy in `core.time_utils`). This type only stamps the offset onto it; it no
longer shifts the value. Until that date `to_ist()` treated the stored value
as UTC and added 5h30, so every timestamp in the UI read five and a half
hours late -- an order imported at 15:29 showed as 20:59.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import PlainSerializer

from core.time_utils import ist_isoformat

IstDateTime = Annotated[
    datetime,
    PlainSerializer(ist_isoformat, return_type=str, when_used="json"),
]
"""A datetime that is stored as naive UTC but always serialized as IST."""
