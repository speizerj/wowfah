"""DuckDB views over the Parquet data directory."""

from __future__ import annotations

from pathlib import Path

import duckdb

from wowfah import watchlist
from wowfah.items import items_path

SCAN_TABLES = ("scans", "item_scans", "ladder")


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(
    data_dir: str | Path,
    database: str = ":memory:",
    watchlist_csv: str | Path | None = watchlist.DEFAULT_CSV,
) -> duckdb.DuckDBPyConnection:
    """Open DuckDB with views:

    scans, item_scans, ladder   raw ingested tables
    market                      per scan and item (status ok): depth, listings, quantity-weighted prices
    buy_price(n)                table macro: average unit price to buy the cheapest n units of each item per scan
    items                       item metadata, if `wowfah items import` has run
    watchlist                   watchlist.csv, if it exists
    """
    data_dir = Path(data_dir)
    for sub in SCAN_TABLES:
        if not any((data_dir / sub).glob("*.parquet")):
            raise FileNotFoundError(f"no Parquet files in {data_dir / sub}; ingest a scan first")

    con = duckdb.connect(database)
    for sub in SCAN_TABLES:
        glob = _sql_str(str(data_dir / sub / "*.parquet"))
        con.execute(f"CREATE VIEW {sub} AS SELECT * FROM read_parquet({glob}, union_by_name = true)")

    con.execute("""
        CREATE VIEW ok_ladder AS
        SELECT
            l.*,
            sum(l.quantity) OVER w AS cum_quantity,
            sum(l.quantity) OVER (PARTITION BY l.scan_id, l.item_id) AS total_quantity
        FROM ladder l
        JOIN item_scans s USING (scan_id, item_id)
        WHERE s.status = 'ok'
        WINDOW w AS (PARTITION BY l.scan_id, l.item_id ORDER BY l.unit_price, l.stack_size, l.time_left
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    """)
    con.execute("""
        CREATE VIEW market AS
        SELECT
            s.scan_id,
            s.realm,
            s.faction,
            s.item_id,
            s.name,
            s.started_at AS scanned_at,
            s.capped,  -- true: stopped at a page cap before the whole market was read (quantity/median are a floor, not exact)
            s.reported_listings,
            s.reported_quantity,
            s.listings_read,
            s.quantity,
            s.bid_only_quantity,
            min(l.unit_price) AS min_unit_price,
            min(l.unit_price) FILTER (WHERE l.cum_quantity >= 0.10 * l.total_quantity) AS p10_unit_price,
            min(l.unit_price) FILTER (WHERE l.cum_quantity >= 0.25 * l.total_quantity) AS p25_unit_price,
            min(l.unit_price) FILTER (WHERE l.cum_quantity >= 0.50 * l.total_quantity) AS median_unit_price,
            sum(l.quantity) FILTER (WHERE l.time_left IN (1, 2)) AS expiring_quantity,
            sum(l.quantity) FILTER (WHERE l.time_left = 4) AS very_long_quantity
        FROM item_scans s
        LEFT JOIN ok_ladder l USING (scan_id, item_id)
        WHERE s.status = 'ok'
        GROUP BY ALL
    """)
    # Treats every listing as divisible. On classic you buy whole stacks, so the real cost is a little higher.
    con.execute("""
        CREATE MACRO buy_price(n) AS TABLE
        SELECT
            scan_id,
            item_id,
            sum(take * unit_price) / nullif(sum(take), 0) AS avg_unit_price,
            max(unit_price) FILTER (WHERE take > 0) AS max_unit_price,
            sum(take) AS units,
            sum(take) >= n AS filled
        FROM (
            SELECT scan_id, item_id, unit_price,
                   least(quantity, greatest(n - (cum_quantity - quantity), 0)) AS take
            FROM ok_ladder
        )
        GROUP BY scan_id, item_id
    """)

    if items_path(data_dir).exists():
        con.execute(f"CREATE VIEW items AS SELECT * FROM read_parquet({_sql_str(str(items_path(data_dir)))})")
    if watchlist_csv is not None and Path(watchlist_csv).exists():
        watchlist.load(watchlist_csv)  # validate before exposing it
        con.execute(f"""
            CREATE VIEW watchlist AS
            SELECT item_id::BIGINT AS item_id, name, category, bracket,
                   list_filter(string_split(coalesce(tags, ''), ';'), t -> t <> '') AS tags
            FROM read_csv({_sql_str(str(watchlist_csv))}, header = true, comment = '#', all_varchar = true)
        """)
    return con
