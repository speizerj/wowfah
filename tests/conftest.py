from __future__ import annotations

from pathlib import Path

import pytest

from wowfah import savedvars
from wowfah.dummy import generate_db
from wowfah.ingest import ingest_savedvariables


@pytest.fixture
def dummy_db() -> dict:
    return generate_db(seed=7, scans=3)


@pytest.fixture
def dummy_savedvariables(tmp_path: Path, dummy_db: dict) -> Path:
    path = tmp_path / "WoWFAH.lua"
    savedvars.dump({"WoWFAH_DB": dummy_db}, path)
    return path


@pytest.fixture
def data_dir(tmp_path: Path, dummy_savedvariables: Path) -> Path:
    out = tmp_path / "data"
    ingest_savedvariables(dummy_savedvariables, out)
    return out
