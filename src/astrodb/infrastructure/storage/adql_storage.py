from __future__ import annotations

from pathlib import Path


class AdqlStorage:
    def save(self, adql: str, path: Path) -> Path:
        target = path.with_suffix(".adql")
        target.write_text(adql, encoding="utf-8")
        return target

    def load(self, path: Path) -> str:
        target = path.with_suffix(".adql")
        return target.read_text(encoding="utf-8")
