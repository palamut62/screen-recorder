from __future__ import annotations

import os
from pathlib import Path


def detect_session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "unknown").lower()


def detect_display_name() -> str:
    return os.environ.get("DISPLAY", ":0.0")


def ensure_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
