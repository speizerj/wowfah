from __future__ import annotations

import os
from pathlib import Path

from wowfah import savedvars
from wowfah.cli import main
from wowfah.dummy import generate_db
from wowfah.sync import ENV_VAR, find_savedvariables, watch


def install(root: Path, client: str, account: str, db: dict | None = None) -> Path:
    path = root / client / "WTF" / "Account" / account / "SavedVariables" / "WoWFAH.lua"
    path.parent.mkdir(parents=True)
    savedvars.dump({"WoWFAH_DB": db or generate_db(scans=1)}, path)
    return path


def test_find_picks_most_recently_saved(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    old = install(tmp_path, "_classic_era_", "1#1")
    new = install(tmp_path, "_classic_beta_", "2#1")
    os.utime(old, (1, 1))
    assert find_savedvariables([tmp_path, tmp_path / "missing"]) == new
    assert find_savedvariables([tmp_path / "missing"]) is None
    monkeypatch.setenv(ENV_VAR, str(old))
    assert find_savedvariables([tmp_path]) == old


def test_watch_ingests_now_and_on_each_save(tmp_path: Path):
    sv = install(tmp_path, "_classic_beta_", "1#1", generate_db(scans=1, seed=1))
    data = tmp_path / "data"
    seen, ticks = [], iter(range(6))

    def on_ingest(results):
        seen.append([r.status for r in results])
        if len(seen) == 1:  # the game saves again with one more scan
            savedvars.dump({"WoWFAH_DB": generate_db(scans=2, seed=1)}, sv)
            os.utime(sv, (2_000_000_000, 2_000_000_000))

    watch(sv, data, on_ingest, on_error=lambda e: None, interval=0, settle=0,
          stop=lambda: next(ticks, None) is None)
    assert seen == [["written"], ["skipped", "written"]]


def test_watch_retries_a_half_written_file(tmp_path: Path):
    sv = tmp_path / "WoWFAH.lua"
    sv.write_text("WoWFAH_DB = {")  # truncated
    errors, ticks = [], iter(range(3))
    watch(sv, tmp_path / "data", lambda r: None, errors.append, interval=0, settle=0,
          stop=lambda: next(ticks, None) is None)
    assert len(errors) == 3


def test_cli_sync(tmp_path: Path, monkeypatch, capsys):
    sv = install(tmp_path, "_classic_beta_", "1#1")
    monkeypatch.setenv(ENV_VAR, str(sv))
    data = tmp_path / "data"
    assert main(["sync", "--data-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "ingested Dreamscythe-Alliance-" in out and "latest scan: 9/9 items listed; 1 scan(s)" in out
    assert main(["sync", "--data-dir", str(data)]) == 0
    assert "nothing new (1 scan(s) already ingested)" in capsys.readouterr().out
