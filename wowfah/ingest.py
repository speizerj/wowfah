"""SavedVariables -> Parquet.

Layout under the data directory:
    auctions/<scan>.parquet   one file per scan
    scans/<scan>.parquet      scan metadata; written last, so it marks the scan as ingested
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from wowfah import savedvars
from wowfah.schema import AUCTIONS_SCHEMA, ROW_FIELD_COLUMNS, SCANS_SCHEMA, SUPPORTED_SCHEMA_VERSION

DB_VARIABLE = "WoWFAH_DB"
SOURCE = "addon"
FIELD_SEP = "\t"

_BOOL_COLUMNS = {"high_bidder", "complete"}


@dataclass(frozen=True)
class ScanResult:
    scan_id: str
    status: str  # "written" or "skipped"
    rows: int


def _ts(epoch_seconds: int) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def scan_file_stem(scan_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", scan_id)


def _rows_list(scan: dict[str, Any]) -> list[str]:
    rows = scan.get("rows") or []
    # An empty Lua table decodes as {}.
    return [] if isinstance(rows, dict) else rows


def auctions_frame(scan: dict[str, Any]) -> pl.DataFrame:
    row_format: list[str] = scan["rowFormat"]
    split = pl.DataFrame({"raw": _rows_list(scan)}, schema={"raw": pl.String}).select(
        pl.col("raw").str.split(FIELD_SEP).alias("fields")
    )

    field_exprs = []
    for i, field in enumerate(row_format):
        column = ROW_FIELD_COLUMNS.get(field)
        if column is None:
            continue  # field from a newer addon we don't map yet
        raw = pl.col("fields").list.get(i, null_on_oob=True)
        value = pl.when(raw == "").then(None).otherwise(raw)
        if column in _BOOL_COLUMNS:
            value = value.cast(pl.Int8, strict=True).cast(pl.Boolean)
        else:
            value = value.cast(AUCTIONS_SCHEMA[column], strict=True)
        field_exprs.append(value.alias(column))

    meta = {
        "scan_id": pl.lit(scan["scanId"], pl.String),
        "source": pl.lit(SOURCE, pl.String),
        "realm": pl.lit(scan["realm"], pl.String),
        "faction": pl.lit(scan["faction"], pl.String),
        "scanned_at": pl.lit(_ts(scan["startedAt"]), AUCTIONS_SCHEMA["scanned_at"]),
    }
    df = split.select(*field_exprs).with_columns(**meta)
    return df.select(
        (pl.col(c) if c in df.columns else pl.lit(None)).cast(dtype).alias(c)
        for c, dtype in AUCTIONS_SCHEMA.items()
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
        "started_at": _ts(scan["startedAt"]),
        "finished_at": _ts(scan["finishedAt"]),
        "listed": scan.get("listed"),
        "row_count": scan.get("rowCount"),
        "incomplete": scan.get("incomplete"),
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
    if schema_version is None or schema_version > SUPPORTED_SCHEMA_VERSION:
        raise ValueError(f"unsupported {DB_VARIABLE} schemaVersion {schema_version!r}")

    ingested_at = datetime.now(timezone.utc)
    scans = db.get("scans") or []
    if isinstance(scans, dict):
        scans = []

    results = []
    for scan in scans:
        stem = scan_file_stem(scan["scanId"])
        scan_path = data_dir / "scans" / f"{stem}.parquet"
        if scan_path.exists() and not force:
            results.append(ScanResult(scan["scanId"], "skipped", len(_rows_list(scan))))
            continue
        auctions = auctions_frame(scan)
        _write_atomic(auctions, data_dir / "auctions" / f"{stem}.parquet")
        _write_atomic(scan_frame(scan, schema_version, ingested_at), scan_path)
        results.append(ScanResult(scan["scanId"], "written", auctions.height))
    return results


def ingest_savedvariables(path: str | Path, data_dir: str | Path, *, force: bool = False) -> list[ScanResult]:
    variables = savedvars.load(path)
    if DB_VARIABLE not in variables:
        raise ValueError(f"{path} does not define {DB_VARIABLE}")
    return ingest_db(variables[DB_VARIABLE], data_dir, force=force)
