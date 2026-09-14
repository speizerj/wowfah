"""Item metadata from wago.tools DB2 CSV exports (ItemSparse + Item)."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import polars as pl

from wowfah.schema import ITEMS_SCHEMA

WAGO_CSV_URL = "https://wago.tools/db2/{table}/csv?branch={branch}"
DEFAULT_BRANCH = "wow_classic_era"

COMMODITY_CLASSES = {0, 5, 7}  # Consumable, Reagent, Trade Goods
BIND_ON_PICKUP = 1

_SPARSE_COLUMNS = {
    "ID": "item_id",
    "Display_lang": "name",
    "OverallQualityID": "quality",
    "ItemLevel": "item_level",
    "RequiredLevel": "required_level",
    "Stackable": "max_stack",
    "Bonding": "bonding",
    "SellPrice": "sell_price",
    "BuyPrice": "buy_price",
}
_ITEM_COLUMNS = {"ID": "item_id", "ClassID": "class_id", "SubclassID": "subclass_id"}


def download(table: str, branch: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(WAGO_CSV_URL.format(table=table, branch=branch), headers={"User-Agent": "wowfah"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        dest.write_bytes(resp.read())
    return dest


def _read(path: Path, columns: dict[str, str]) -> pl.DataFrame:
    header = pl.read_csv(path, n_rows=0).columns
    missing = sorted(set(columns) - set(header))
    if missing:
        raise ValueError(f"{path} is missing columns {missing}; the DB2 layout may have changed")
    return pl.read_csv(path, columns=list(columns), infer_schema_length=0).rename(columns)


def commodity_rule() -> pl.Expr:
    return (
        (pl.col("max_stack") > 1)
        & (pl.col("bonding") != BIND_ON_PICKUP)
        & pl.col("class_id").is_in(COMMODITY_CLASSES)
        & (pl.col("quality") >= 1)
    )


def build_items(itemsparse_csv: Path, item_csv: Path, branch: str) -> pl.DataFrame:
    sparse = _read(itemsparse_csv, _SPARSE_COLUMNS)
    item = _read(item_csv, _ITEM_COLUMNS)
    df = sparse.join(item, on="item_id", how="left").with_columns(source_branch=pl.lit(branch))
    df = df.select(pl.col(c).cast(t, strict=True) for c, t in ITEMS_SCHEMA.items() if c != "is_commodity")
    return df.with_columns(is_commodity=commodity_rule().fill_null(False)).select(list(ITEMS_SCHEMA)).sort("item_id")


def items_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "items" / "items.parquet"


def load_items(data_dir: str | Path) -> pl.DataFrame | None:
    path = items_path(data_dir)
    return pl.read_parquet(path) if path.exists() else None
