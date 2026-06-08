from __future__ import annotations
from typing import Protocol
from pathlib import Path
import pandas as pd

from astrodb.domain.models import QueryResult


class IQueryClient(Protocol):
    def execute(self, adql: str) -> QueryResult: ...
    def get_columns(self, table_name: str) -> list[str]: ...


class IStorage(Protocol):
    def save(self, df: pd.DataFrame, path: Path) -> None: ...
