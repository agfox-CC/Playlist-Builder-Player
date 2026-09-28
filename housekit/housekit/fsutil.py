"""Filesystem helpers shared by tags, features and output."""
from __future__ import annotations

import os
from pathlib import Path


def longpath(p: Path | str) -> str:
    """Return a string path safe for Windows' 260-char limit (\\\\?\\ prefix when needed)."""
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        s = os.path.abspath(s)
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s
