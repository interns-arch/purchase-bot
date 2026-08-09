"""Shared field types for API schemas.

`IstDateTime` is the ONE place API timestamp serialization is defined. Any
schema field declared with it is emitted as an IST-aware ISO-8601 string
(e.g. "2026-08-09T11:08:29.315733+05:30") instead of the previous naive
"2026-08-09T05:38:29.315733", which JavaScript silently misread as local time.

Storage is unchanged: the database still holds naive UTC. Conversion happens
only here, at the serialization edge -- see `core.time_utils` for the full
rationale and the double-conversion guarantee.
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
