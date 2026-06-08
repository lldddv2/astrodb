from __future__ import annotations
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class QueryHistoryEntry:
    id: str
    adql: str
    started_at: float
    started_wall: str = ""
    finished_at: float | None = None
    status: Literal["running", "ok", "error", "cancelled"] = "running"
    row_count: int | None = None
    error: str | None = None
    save_target: Path | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at
