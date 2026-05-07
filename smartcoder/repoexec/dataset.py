from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd


def load_repoexec_rows(parquet_path: Path, start: int, limit: int) -> List[Dict[str, Any]]:
    df = pd.read_parquet(parquet_path)
    end = min(start + limit, len(df))
    return [df.iloc[i].to_dict() for i in range(start, end)]
