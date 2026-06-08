from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd


@dataclass
class DatabaseConfig:
    name: str
    display_name: str
    query_url: str          # base REST URL; query is appended URL-encoded
    columns_mode: str = "tap_schema"  # "tap_schema" | "select_top1"


@dataclass
class QueryResult:
    df: pd.DataFrame
    columns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.columns and not self.df.empty:
            self.columns = list(self.df.columns)
