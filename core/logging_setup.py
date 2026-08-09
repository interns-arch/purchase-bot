"""Shared console logging setup for the delivery-through-dashboard CLI scripts."""

from __future__ import annotations

import logging
import os


def _ist_timetuple(timestamp=None):
    """logging.Formatter.converter -> struct_time in IST."""
    from datetime import datetime, timezone as _tz
    from core.time_utils import IST
    return datetime.fromtimestamp(timestamp, _tz.utc).astimezone(IST).timetuple()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    handler = logging.StreamHandler()
    # Log timestamps in IST (the business timezone) so Render logs line up
    # with what users see in the UI. Uses the same central conversion as
    # every other timestamp -- see core.time_utils.
    formatter = logging.Formatter(
        "%(asctime)s IST [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    formatter.converter = _ist_timetuple
    handler.setFormatter(
        formatter or logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger
