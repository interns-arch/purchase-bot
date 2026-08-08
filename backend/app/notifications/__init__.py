"""Lightweight, in-memory toast notifications for integration events.

No database, no persisted history, no activity log -- just a small transient
buffer the logged-in frontend polls to display toasts. See `broker.py`."""
