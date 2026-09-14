from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from wowfah.dummy import pack
from wowfah.ingest import ingest_db, ingest_savedvariables, item_scans_frame, ladder_frame, scan_file_stem
from wowfah.schema import ITEM_SCANS_SCHEMA, LADDER_FORMAT, LADDER_SCHEMA, SCANS_SCHEMA


def _item(item_id=2589, ladder=None, **overrides):
    item = {
        "itemId": item_id,
        "name": "Linen Cloth",
        "status": "ok",
        "startedAt": 1789646410,
        "finishedAt": 1789646415,
        "pages": 1,
        "reportedListings": 3,
        "listingsRead": 3,
        "quantity": 40,
        "bidOnlyListings": 0,
        "bidOnlyQuantity": 0,
        "unreadable": 0,
        "ladder": ladder if ladder is not None else [],
    }
    item.update(overrides)
    return item


def _scan(items, ladder_format=None, **overrides):
    scan = {
        "scanId": "Pyrewood Village-Horde-1789646400",
        "addonVersion": "0.2.0",
        "api": "classic",
        "realm": "Pyrewood Village",
        "faction": "Horde",
        "status": "complete",
        "startedAt": 1789646400,
        "finishedAt": 1789646460,
        "itemsRequested": len(items),
        "itemsScanned": len(items),
        "ladderFormat": ladder_format or list(LADDER_FORMAT),
        "items": items,
    }
    scan.update(overrides)
    return scan


def test_ingest_writes_one_file_set_per_scan(dummy_savedvariables: Path, dummy_db: dict, tmp_path: Path):
    out = tmp_path / "data"
    results = ingest_savedvariables(dummy_savedvariables, out)

    assert [r.status for r in results] == ["written"] * 3
    for sub in ("scans", "item_scans", "ladder"):
        assert len(list((out / sub).glob("*.parquet"))) == 3

    ladder = pl.read_parquet(out / "ladder" / "*.parquet")
    item_scans = pl.read_parquet(out / "item_scans" / "*.parquet")
    scans = pl.read_parquet(out / "scans" / "*.parquet")
    assert ladder.schema == pl.Schema(LADDER_SCHEMA)
    assert item_scans.schema == pl.Schema(ITEM_SCANS_SCHEMA)
    assert scans.schema == pl.Schema(SCANS_SCHEMA)

    expected_rows = sum(len(i["ladder"]) for s in dummy_db["scans"] for i in s["items"])
    assert ladder.height == sum(r.ladder_rows for r in results) == expected_rows
    assert item_scans.height == 27
    assert ladder["quantity"].sum() == item_scans["quantity"].sum()


def test_reingest_is_idempotent(dummy_savedvariables: Path, tmp_path: Path):
    out = tmp_path / "data"
    first = ingest_savedvariables(dummy_savedvariables, out)
    again = ingest_savedvariables(dummy_savedvariables, out)
    assert [r.status for r in again] == ["skipped"] * 3
    forced = ingest_savedvariables(dummy_savedvariables, out, force=True)
    assert [r.status for r in forced] == ["written"] * 3
    assert pl.read_parquet(out / "ladder" / "*.parquet").height == sum(r.ladder_rows for r in first)


def test_ladder_decoding():
    scan = _scan([
        _item(ladder=[pack([10, 20, 4, 1, 20]), pack([12, 5, 1, 4, 20])]),
        _item(13468, name="Black Lotus", ladder=[pack([250000, 0, 0, 2, 3])], startedAt=1789646430),
    ])
    rows = ladder_frame(scan).to_dicts()
    assert [(r["item_id"], r["unit_price"], r["stack_size"], r["time_left"], r["listings"], r["quantity"])
            for r in rows] == [(2589, 10, 20, 4, 1, 20), (2589, 12, 5, 1, 4, 20), (13468, 250000, 0, 0, 2, 3)]
    assert rows[0]["scan_id"] == "Pyrewood Village-Horde-1789646400"
    assert rows[0]["realm"] == "Pyrewood Village" and rows[0]["faction"] == "Horde"
    assert rows[0]["scanned_at"].isoformat() == "2026-09-17T12:00:10+00:00"
    assert rows[2]["scanned_at"].isoformat() == "2026-09-17T12:00:30+00:00"


def test_ladder_format_order_is_respected_and_unknown_fields_ignored():
    fmt = ["quantity", "unitPrice", "futureField", "timeLeft"]
    df = ladder_frame(_scan([_item(ladder=["60\t15\tx\t3"])], ladder_format=fmt))
    row = df.row(0, named=True)
    assert (row["quantity"], row["unit_price"], row["time_left"]) == (60, 15, 3)
    assert row["stack_size"] is None and row["listings"] is None


def test_item_scans_keep_failed_items_and_missing_fields():
    scan = _scan([_item(), _item(4306, name="Silk Cloth", status="timeout", reportedListings=None)])
    df = item_scans_frame(scan)
    assert df.schema == pl.Schema(ITEM_SCANS_SCHEMA)
    assert df["status"].to_list() == ["ok", "timeout"]
    assert df["reported_listings"].to_list() == [3, None]


def test_empty_tables_decode_as_empty_frames():
    # Empty Lua tables round-trip as {} rather than [].
    assert ladder_frame(_scan({})).height == 0
    assert ladder_frame(_scan([_item(ladder={})])).schema == pl.Schema(LADDER_SCHEMA)
    assert item_scans_frame(_scan({})).height == 0


def test_malformed_number_fails_loudly():
    with pytest.raises(pl.exceptions.InvalidOperationError):
        ladder_frame(_scan([_item(ladder=["ten\t20\t4\t1\t20"])]))


@pytest.mark.parametrize("version", [None, 1, 99])
def test_rejects_other_schema_versions(tmp_path: Path, version):
    with pytest.raises(ValueError, match="schemaVersion"):
        ingest_db({"schemaVersion": version, "scans": []}, tmp_path)


def test_scan_file_stem_is_filesystem_safe():
    assert scan_file_stem("Zul'Jin Village-Horde-1789646400") == "Zul_Jin_Village-Horde-1789646400"
