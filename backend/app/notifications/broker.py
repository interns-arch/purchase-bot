"""In-memory, transient notification buffer for integration-event toasts.

Deliberately NOT a database table, history store, or event log: it's a small
bounded, thread-safe ring buffer holding only the most recent events, just
long enough for the logged-in frontend to poll them and show a toast. It is
wiped on every restart and rotates old events out (`maxlen`). Background
workers run in threads (FastAPI `BackgroundTasks` / the APScheduler poll),
so access is guarded by a lock.

Each event carries a monotonically increasing `id`; the frontend remembers
the last id it has seen and asks only for newer ones, so each toast is shown
exactly once and old buffered events are never replayed on login."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from itertools import count

# Small on purpose -- this is a live delivery buffer, not a history.
_MAX_BUFFERED = 50

# Allowed toast styles (kept in sync with the frontend ToastContext).
LEVELS = ("success", "error", "warning", "info")


@dataclass(frozen=True)
class Notification:
    id: int
    level: str
    title: str
    message: str


_lock = threading.Lock()
_buffer: "deque[Notification]" = deque(maxlen=_MAX_BUFFERED)
_ids = count(1)


def publish(level: str, title: str, message: str = "") -> int:
    """Add an event to the buffer. `level` is one of `LEVELS` (falls back to
    'info' if unrecognised). Returns the new event id."""
    if level not in LEVELS:
        level = "info"
    with _lock:
        note = Notification(id=next(_ids), level=level, title=title, message=message)
        _buffer.append(note)
        return note.id


def get_since(after_id: int) -> list[dict]:
    """Events newer than `after_id`, oldest first, as plain dicts."""
    with _lock:
        return [asdict(note) for note in _buffer if note.id > after_id]


def latest_id() -> int:
    """The id of the newest buffered event (0 if the buffer is empty). Used by
    the frontend to initialise its cursor without replaying old events."""
    with _lock:
        return _buffer[-1].id if _buffer else 0
