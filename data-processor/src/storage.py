"""Shared Parquet I/O through ordinary paths, including the GCSFuse mount."""

from pathlib import Path

import pandas as pd


def read_existing_parquet(path, columns):
    """Read existing rows, or return an empty frame with the expected columns."""
    path = Path(path)
    return pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=columns)


def save_parquet(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
