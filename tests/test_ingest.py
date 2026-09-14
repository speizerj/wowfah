from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from wowfah.dummy import pack_row
from wowfah.ingest import auctions_frame, ingest_db, ingest_savedvariables, scan_file_stem
from wowfah.schema import AUCTIONS_SCHEMA, ROW_FORMAT, SCANS_SCHEMA


def _scan(rows, row_format=None, **overrides):
    scan = {
        "scanId": "Pyrewood Village-Horde-1789646400",
        "addonVersion": "0.1.0",
        "api": "classic",
        "realm": "Pyrewood Village",
        "faction": "Horde",
        "startedAt": 1789646400,
        "finishedAt": 1789646460,
        "listed": len(rows),
        "rowCount": len(rows),
        "incomplete": 0,
        "rowFormat": row_format or list(ROW_FORMAT),
        "rows": rows,
    }
    scan.update(overrides)
    return scan


def test_ingest_writes_one_file_pair_per_scan(dummy_savedvariables: Path, dummy_db: dict, tmp_path: Path):
    out = tmp_path / "data"
    results = ingest_savedvariables(dummy_savedvariables, out)

    assert [r.status for r in results] == ["written"] * 3
    assert len(list((out / "auctions").glob("*.parquet"))) == 3
    assert len(list((out / "scans").glob("*.parquet"))) == 3

    auctions = pl.read_parquet(out / "auctions" / "*.parquet")
    scans = pl.read_parquet(out / "scans" / "*.parquet")
    assert auctions.schema == pl.Schema(AUCTIONS_SCHEMA)
    assert scans.schema == pl.Schema(SCANS_SCHEMA)
    assert auctions.height == 1200
    assert scans["row_count"].sum() == 1200
    assert auctions["complete"].not_().sum() == sum(s["incomplete"] for s in dummy_db["scans"])


def test_reingest_is_idempotent(dummy_savedvariables: Path, tmp_path: Path):
    out = tmp_path / "data"
    ingest_savedvariables(dummy_savedvariables, out)
    again = ingest_savedvariables(dummy_savedvariables, out)
    assert [r.status for r in again] == ["skipped"] * 3
    forced = ingest_savedvariables(dummy_savedvariables, out, force=True)
    assert [r.status for r in forced] == ["written"] * 3
    assert pl.read_parquet(out / "auctions" / "*.parquet").height == 1200


def test_row_decoding_types_and_nulls():
    rows = [
        pack_row([2589, "item:2589::::::::60:::::", "Linen Cloth", 20, 1, 5, 200, 0, 240, 0, False, "Stackz", 4, 0, True]),
        pack_row([13468, "item:13468", None, 1, 1, 60, 90000, 5000, 0, 95000, True, None, 2, 0, False]),
    ]
    df = auctions_frame(_scan(rows))
    assert df.row(0, named=True) | {"scanned_at": None} == {
        "scan_id": "Pyrewood Village-Horde-1789646400",
        "source": "addon",
        "realm": "Pyrewood Village",
        "faction": "Horde",
        "scanned_at": None,
        "item_id": 2589,
        "item_string": "item:2589::::::::60:::::",
        "name": "Linen Cloth",
        "count": 20,
        "quality": 1,
        "item_level": 5,
        "min_bid": 200,
        "min_increment": 0,
        "buyout": 240,
        "bid_amount": 0,
        "high_bidder": False,
        "owner": "Stackz",
        "time_left": 4,
        "sale_status": 0,
        "complete": True,
    }
    second = df.row(1, named=True)
    assert second["name"] is None and second["owner"] is None
    assert second["high_bidder"] is True and second["complete"] is False
    assert df["scanned_at"][0].isoformat() == "2026-09-17T12:00:00+00:00"


def test_row_format_order_is_respected_and_unknown_fields_ignored():
    fmt = ["name", "itemId", "futureField", "count", "buyout"]
    df = auctions_frame(_scan(["Silk Cloth\t4306\tsomething\t10\t800"], row_format=fmt))
    row = df.row(0, named=True)
    assert (row["name"], row["item_id"], row["count"], row["buyout"]) == ("Silk Cloth", 4306, 10, 800)
    assert row["owner"] is None and row["min_bid"] is None


def test_empty_scan_rows_decode_as_empty_frame():
    # An empty Lua table round-trips as {} rather than [].
    df = auctions_frame(_scan({}))
    assert df.height == 0
    assert df.schema == pl.Schema(AUCTIONS_SCHEMA)


def test_malformed_number_fails_loudly():
    with pytest.raises(pl.exceptions.InvalidOperationError):
        auctions_frame(_scan([pack_row([2589, "item:2589", "Linen", "twenty"] + [None] * 11)]))


def test_rejects_newer_schema_version(tmp_path: Path):
    with pytest.raises(ValueError, match="schemaVersion"):
        ingest_db({"schemaVersion": 99, "scans": []}, tmp_path)


def test_scan_file_stem_is_filesystem_safe():
    assert scan_file_stem("Zul'Jin Village-Horde-1789646400") == "Zul_Jin_Village-Horde-1789646400"
