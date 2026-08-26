from __future__ import annotations

import sys
from pathlib import Path


def data_dir() -> Path:
    """Return persistent storage beside the source tree or packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]
