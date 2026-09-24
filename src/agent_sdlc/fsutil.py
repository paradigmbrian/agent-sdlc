from __future__ import annotations

from pathlib import Path


def ensure_private_dir(path: Path) -> None:
    """Create `path` and every missing ancestor directory, each at mode 0700.

    Walks from the top-most missing ancestor down so every newly created level
    (not just the final one) is private. Directories that already exist are left
    untouched: their mode is never changed. May raise OSError; callers handle it.
    """
    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        parent = p.parent
        if parent == p:
            break
        p = parent
    for d in reversed(missing):
        d.mkdir(mode=0o700)
