from __future__ import annotations
from pathlib import Path
import pandas as pd


class CSVStorage:
    def save(self, df: pd.DataFrame, path: Path) -> None:
        path = path.with_suffix(".csv")
        df.to_csv(path, index=False)
