"""DuckDB views over the Parquet data directory."""

from __future__ import annotations

from pathlib import Path

import duckdb


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(data_dir: str | Path, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    data_dir = Path(data_dir)
    for sub in ("auctions", "scans"):
        if not any((data_dir / sub).glob("*.parquet")):
            raise FileNotFoundError(f"no Parquet files in {data_dir / sub}; ingest a scan first")

    con = duckdb.connect(database)
    auctions_glob = _sql_str(str(data_dir / "auctions" / "*.parquet"))
    scans_glob = _sql_str(str(data_dir / "scans" / "*.parquet"))

    con.execute(f"CREATE VIEW scans AS SELECT * FROM read_parquet({scans_glob}, union_by_name = true)")
    con.execute(f"""
        CREATE VIEW auctions AS
        SELECT
            *,
            CASE WHEN buyout > 0 THEN buyout::DOUBLE / count END AS unit_buyout,
            min_bid::DOUBLE / count AS unit_min_bid
        FROM read_parquet({auctions_glob}, union_by_name = true)
    """)
    con.execute("""
        CREATE VIEW item_prices AS
        SELECT
            scan_id,
            scanned_at,
            realm,
            faction,
            item_id,
            any_value(name) FILTER (WHERE name IS NOT NULL) AS name,
            count(*) AS listings,
            sum(count) AS quantity,
            count(DISTINCT owner) AS sellers,
            min(unit_buyout) AS min_unit_buyout,
            quantile_cont(unit_buyout, 0.5) AS median_unit_buyout,
            sum(count) FILTER (WHERE buyout > 0) AS buyout_quantity
        FROM auctions
        GROUP BY ALL
    """)
    return con
