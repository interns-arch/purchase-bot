"""Shared console logging setup for the delivery-through-dashboard CLI scripts."""

from __future__ import annotations

import logging
import os


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger
