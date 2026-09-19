"""Find the addon's SavedVariables file and ingest it, once or whenever the game saves it."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from wowfah.ingest import ScanResult, ingest_savedvariables

ENV_VAR = "WOWFAH_SV"
WOW_ROOTS = [
    Path("/Applications/World of Warcraft"),
    Path.home() / "Applications" / "World of Warcraft",
    Path("C:/Program Files (x86)/World of Warcraft"),
    Path("C:/Program Files/World of Warcraft"),
]
PATTERN = "_*_/WTF/Account/*/SavedVariables/WoWFAH.lua"


def find_savedvariables(roots: Iterable[Path] = WOW_ROOTS) -> Path | None:
    """$WOWFAH_SV if set, else the most recently saved WoWFAH.lua under any WoW install/client."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env)
    found = [p for root in roots if root.exists() for p in root.glob(PATTERN)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def watch(
    path: Path,
    data_dir: Path,
    on_ingest: Callable[[list[ScanResult]], None],
    on_error: Callable[[Exception], None],
    interval: float = 2.0,
    settle: float = 1.0,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Ingest `path` now, then again each time the game rewrites it, until stop() is true."""
    last = None
    while not stop():
        current = mtime(path)
        if current is not None and current != last:
            time.sleep(settle)  # the game may still be writing the file
            if mtime(path) == current:
                try:
                    on_ingest(ingest_savedvariables(path, data_dir))
                    last = current
                except Exception as exc:  # a half-written file: try again next tick
                    on_error(exc)
                continue
        time.sleep(interval)
