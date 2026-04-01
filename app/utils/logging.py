from __future__ import annotations

import logging
from pathlib import Path


LOG_DIR = Path.home() / ".local" / "share" / "screen-recorder" / "logs"
LOG_FILE = LOG_DIR / "screen_recorder.log"


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return LOG_FILE
