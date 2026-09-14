from __future__ import annotations

from pathlib import Path

import pytest

from wowfah.cli import main
from wowfah.items import build_items, load_items

# Trimmed copies of the wago.tools CSV layout (real exports have many more columns).
SPARSE = """ID,Display_lang,Stackable,Bonding,SellPrice,BuyPrice,OverallQualityID,ItemLevel,RequiredLevel,Flags_0
2447,Peacebloom,20,0,10,40,1,5,0,0
13444,Major Mana Potion,5,0,1500,6000,1,59,49,0
12451,Juju Power,20,1,0,0,1,55,0,0
2589,"Linen Cloth",20,0,13,52,1,5,0,0
17,Martin Fury,1,1,0,0,4,63,0,0
3300,Rabbit's Foot,5,0,20,80,0,1,0,0
"""
ITEM = """ID,ClassID,SubclassID,Material
2447,7,0,0
13444,0,0,0
12451,12,0,0
2589,7,0,0
17,4,4,6
3300,15,0,0
"""


def write_csvs(tmp_path: Path, sparse: str = SPARSE) -> tuple[Path, Path]:
    s, i = tmp_path / "ItemSparse.csv", tmp_path / "Item.csv"
    s.write_text(sparse)
    i.write_text(ITEM)
    return s, i


def test_build_items_applies_commodity_rule(tmp_path: Path):
    df = build_items(*write_csvs(tmp_path), branch="wow_classic_era")
    rows = {r["item_id"]: r for r in df.iter_rows(named=True)}
    assert {k for k, r in rows.items() if r["is_commodity"]} == {2447, 13444, 2589}
    assert rows[12451]["is_commodity"] is False  # bind on pickup
    assert rows[3300]["is_commodity"] is False  # poor quality, misc class
    assert rows[13444]["sell_price"] == 1500 and rows[13444]["max_stack"] == 5
    assert rows[2589]["name"] == "Linen Cloth" and rows[2589]["source_branch"] == "wow_classic_era"
    assert df["item_id"].is_sorted()


def test_missing_column_fails_loudly(tmp_path: Path):
    sparse = SPARSE.replace("Bonding", "Binding")
    with pytest.raises(ValueError, match="Bonding"):
        build_items(*write_csvs(tmp_path, sparse), branch="x")


def test_cli_items_import_from_local_files(tmp_path: Path, capsys):
    s, i = write_csvs(tmp_path)
    data = tmp_path / "data"
    assert main(["items", "import", "--itemsparse", str(s), "--item", str(i), "--data-dir", str(data)]) == 0
    assert "6 items (3 match the commodity rule)" in capsys.readouterr().out
    assert load_items(data).height == 6
    assert load_items(tmp_path / "nowhere") is None
