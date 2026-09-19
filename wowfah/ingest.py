"""SavedVariables -> Parquet.

Layout under the data directory:
    ladder/<scan>.parquet       buyout price ladder rows, one file per scan
    item_scans/<scan>.parquet   one row per watched item per scan
    scans/<scan>.parquet        scan metadata; written last, so it marks the scan as ingested
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from wowfah import savedvars
from wowfah.schema import (
    ITEM_SCANS_SCHEMA,
    LADDER_FIELD_COLUMNS,
    LADDER_SCHEMA,
    SCANS_SCHEMA,
    SUPPORTED_SCHEMA_VERSION,
)

DB_VARIABLE = "WoWFAH_DB"
SOURCE = "addon"
FIELD_SEP = "\t"


@dataclass(frozen=True)
class ScanResult:
    scan_id: str
    status: str  # "written" or "skipped"
    items: int
    ladder_rows: int


def _ts(epoch_seconds: int | None) -> datetime | None:
    return None if epoch_seconds is None else datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def scan_file_stem(scan_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", scan_id)


def _as_list(value: Any) -> list:
    # An empty Lua table decodes as {}.
    return [] if not value or isinstance(value, dict) else value


def item_scans_frame(scan: dict[str, Any]) -> pl.DataFrame:
    rows = [
        {
            "scan_id": scan["scanId"],
            "realm": scan["realm"],
            "faction": scan["faction"],
            "item_id": item["itemId"],
            "name": item.get("name"),
            "status": item["status"],
            "capped": bool(item.get("capped", False)),
            "started_at": _ts(item.get("startedAt")),
            "finished_at": _ts(item.get("finishedAt")),
            "pages": item.get("pages"),
            "reported_listings": item.get("reportedListings"),
            "reported_quantity": item.get("reportedQuantity"),
            "listings_read": item.get("listingsRead"),
            "quantity": item.get("quantity"),
            "bid_only_listings": item.get("bidOnlyListings"),
            "bid_only_quantity": item.get("bidOnlyQuantity"),
            "unreadable": item.get("unreadable"),
        }
        for item in _as_list(scan.get("items"))
    ]
    return pl.DataFrame(rows, schema=ITEM_SCANS_SCHEMA)


def ladder_frame(scan: dict[str, Any]) -> pl.DataFrame:
    ladder_format: list[str] = scan["ladderFormat"]
    raw, item_ids, scanned_at = [], [], []
    for item in _as_list(scan.get("items")):
        rows = _as_list(item.get("ladder"))
        raw += rows
        item_ids += [item["itemId"]] * len(rows)
        scanned_at += [_ts(item.get("startedAt") or scan["startedAt"])] * len(rows)

    base = pl.DataFrame(
        {"raw": raw, "item_id": item_ids, "scanned_at": scanned_at},
        schema={"raw": pl.String, "item_id": pl.Int64, "scanned_at": LADDER_SCHEMA["scanned_at"]},
    )
    fields = pl.col("raw").str.split(FIELD_SEP)
    field_exprs = []
    for i, field in enumerate(ladder_format):
        column = LADDER_FIELD_COLUMNS.get(field)
        if column is None:
            continue  # field from a newer addon we don't map yet
        value = fields.list.get(i, null_on_oob=True)
        field_exprs.append(pl.when(value == "").then(None).otherwise(value).cast(LADDER_SCHEMA[column], strict=True)
                           .alias(column))

    df = base.with_columns(
        *field_exprs,
        scan_id=pl.lit(scan["scanId"], pl.String),
        realm=pl.lit(scan["realm"], pl.String),
        faction=pl.lit(scan["faction"], pl.String),
    )
    return df.select(
        (pl.col(c) if c in df.columns else pl.lit(None)).cast(dtype).alias(c)
        for c, dtype in LADDER_SCHEMA.items()
    )


def scan_frame(scan: dict[str, Any], schema_version: int, ingested_at: datetime) -> pl.DataFrame:
    row = {
        "scan_id": scan["scanId"],
        "source": SOURCE,
        "api": scan.get("api"),
        "addon_version": scan.get("addonVersion"),
        "schema_version": schema_version,
        "realm": scan["realm"],
        "faction": scan["faction"],
        "category": scan.get("category"),
        "status": scan.get("status"),
        "started_at": _ts(scan["startedAt"]),
        "finished_at": _ts(scan.get("finishedAt")),
        "items_requested": scan.get("itemsRequested"),
        "items_scanned": scan.get("itemsScanned"),
        "ingested_at": ingested_at,
    }
    return pl.DataFrame([row], schema=SCANS_SCHEMA)


def _write_atomic(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(path)


def ingest_db(db: dict[str, Any], data_dir: str | Path, *, force: bool = False) -> list[ScanResult]:
    data_dir = Path(data_dir)
    schema_version = db.get("schemaVersion")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported {DB_VARIABLE} schemaVersion {schema_version!r} (expected {SUPPORTED_SCHEMA_VERSION})"
        )

    ingested_at = datetime.now(timezone.utc)
    results = []
    for scan in _as_list(db.get("scans")):
        stem = scan_file_stem(scan["scanId"])
        scan_path = data_dir / "scans" / f"{stem}.parquet"
        items = len(_as_list(scan.get("items")))
        if scan_path.exists() and not force:
            results.append(ScanResult(scan["scanId"], "skipped", items, 0))
            continue
        ladder = ladder_frame(scan)
        _write_atomic(ladder, data_dir / "ladder" / f"{stem}.parquet")
        _write_atomic(item_scans_frame(scan), data_dir / "item_scans" / f"{stem}.parquet")
        _write_atomic(scan_frame(scan, schema_version, ingested_at), scan_path)
        results.append(ScanResult(scan["scanId"], "written", items, ladder.height))
    return results


def ingest_savedvariables(path: str | Path, data_dir: str | Path, *, force: bool = False) -> list[ScanResult]:
    variables = savedvars.load(path)
    if DB_VARIABLE not in variables:
        raise ValueError(f"{path} does not define {DB_VARIABLE}")
    return ingest_db(variables[DB_VARIABLE], data_dir, force=force)
