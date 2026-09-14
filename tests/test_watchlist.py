from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_items import write_csvs
from wowfah import savedvars, watchlist
from wowfah.cli import main
from wowfah.items import build_items

HEADER = "item_id,name,category,bracket,tags\n"


def write(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "watchlist.csv"
    path.write_text("# a comment\n\n" + header + body)
    return path


def test_load_parses_rows_and_tags(tmp_path: Path):
    entries = watchlist.load(write(tmp_path, '2447,Peacebloom,herb,1-20,leveling; cheap\n'
                                             '13444,"Major Mana Potion",potion,,\n'))
    assert entries == [
        watchlist.Entry(2447, "Peacebloom", "herb", "1-20", ("leveling", "cheap")),
        watchlist.Entry(13444, "Major Mana Potion", "potion", "", ()),
    ]


@pytest.mark.parametrize("body, header, message", [
    ("2447,Peacebloom,herb,,\n2447,Peacebloom,herb,,\n", HEADER, "duplicate"),
    ("abc,Peacebloom,herb,,\n", HEADER, "integer"),
    ("2447,,herb,,\n", HEADER, "name"),
    ("2447,Peacebloom,Herb Stuff,,\n", HEADER, "category"),
    ("2447,Peacebloom,herb\n", "item_id,name,category\n", "header"),
])
def test_load_rejects_bad_rows(tmp_path: Path, body: str, header: str, message: str):
    with pytest.raises(ValueError, match=message):
        watchlist.load(write(tmp_path, body, header))


def test_check_against_items(tmp_path: Path):
    items = build_items(*write_csvs(tmp_path), branch="x")
    entries = watchlist.load(write(tmp_path, "2447,Peacebloom,herb,,\n13444,Major Mana,potion,,\n"
                                             "12451,Juju Power,misc,,\n99,Nothing,misc,,\n"))
    errors, warnings = watchlist.check(entries, items)
    assert errors == ["13444 Major Mana: item data calls it 'Major Mana Potion'",
                      "99 Nothing: item id not found in item data"]
    assert warnings == ["12451 Juju Power: doesn't match the commodity rule (kept, since it's listed explicitly)"]


def test_to_lua_round_trips_through_lua(tmp_path: Path):
    entries = watchlist.load(write(tmp_path, '2447,Peacebloom,herb,,\n1,"Arthas\' ""Tears""",herb,,\n'))
    lua = savedvars.new_runtime()
    ns = lua.table()
    lua.execute("local src, ns = ...; assert(load(src))(nil, ns)", watchlist.to_lua(entries), ns)
    assert savedvars.lua_to_python(ns.WATCHLIST) == [[2447, "Peacebloom", "herb"], [1, 'Arthas\' "Tears"', "herb"]]


def test_cli_export_refuses_when_check_fails(tmp_path: Path, capsys):
    s, i = write_csvs(tmp_path)
    data = tmp_path / "data"
    main(["items", "import", "--itemsparse", str(s), "--item", str(i), "--data-dir", str(data)])
    out = tmp_path / "Watchlist.lua"

    bad = write(tmp_path, "99,Nothing,misc,,\n")
    assert main(["watchlist", "export", "--csv", str(bad), "--output", str(out), "--data-dir", str(data)]) == 1
    assert not out.exists()

    good = write(tmp_path, "2447,Peacebloom,herb,,\n")
    assert main(["watchlist", "export", "--csv", str(good), "--output", str(out), "--data-dir", str(data)]) == 0
    assert "exported 1 items" in capsys.readouterr().out
    assert "Peacebloom" in out.read_text()


def test_repo_watchlist_is_valid():
    entries = watchlist.load()
    assert len(entries) > 100
    assert len({e.item_id for e in entries}) == len(entries)
