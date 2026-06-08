from __future__ import annotations
from pathlib import Path
import sqlite3
import pandas as pd


class SQLiteStorage:
    def save(self, df: pd.DataFrame, path: Path, table_name: str = "results") -> None:
        path = path.with_suffix(".db")
        with sqlite3.connect(path) as conn:
            df.to_sql(table_name, conn, if_exists="replace", index=False)
