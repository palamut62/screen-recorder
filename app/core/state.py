from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0 and self.x >= 0 and self.y >= 0


@dataclass(slots=True)
class AppState:
    is_recording: bool = False
    selected_region: Optional[Region] = None
    output_format: str = "mp4"
    fps: int = 30
    output_dir: Path = Path.home() / "Videos"

