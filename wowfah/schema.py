"""Parquet schemas shared by every data source (addon scans now, Blizzard Web API dumps later)."""

from __future__ import annotations

import polars as pl

SUPPORTED_SCHEMA_VERSION = 1

# Packed row field order written by the addon (addon/WoWFAH/Core.lua ns.ROW_FORMAT).
ROW_FORMAT = [
    "itemId", "itemString", "name", "count", "quality", "level",
    "minBid", "minIncrement", "buyout", "bidAmount", "highBidder",
    "owner", "timeLeft", "saleStatus", "complete",
]

# Addon row field -> Parquet column.
ROW_FIELD_COLUMNS = {
    "itemId": "item_id",
    "itemString": "item_string",
    "name": "name",
    "count": "count",
    "quality": "quality",
    "level": "item_level",
    "minBid": "min_bid",
    "minIncrement": "min_increment",
    "buyout": "buyout",
    "bidAmount": "bid_amount",
    "highBidder": "high_bidder",
    "owner": "owner",
    "timeLeft": "time_left",
    "saleStatus": "sale_status",
    "complete": "complete",
}

UTC_TS = pl.Datetime("ms", "UTC")

# One row per auction per scan. Money is copper; buyout/min_bid/bid_amount are
# for the whole auction (stack), 0 buyout means bid-only.
AUCTIONS_SCHEMA: dict[str, pl.DataType] = {
    "scan_id": pl.String(),
    "source": pl.String(),
    "realm": pl.String(),
    "faction": pl.String(),
    "scanned_at": UTC_TS,
    "item_id": pl.Int64(),
    "item_string": pl.String(),
    "name": pl.String(),
    "count": pl.Int32(),
    "quality": pl.Int8(),
    "item_level": pl.Int32(),
    "min_bid": pl.Int64(),
    "min_increment": pl.Int64(),
    "buyout": pl.Int64(),
    "bid_amount": pl.Int64(),
    "high_bidder": pl.Boolean(),
    "owner": pl.String(),
    "time_left": pl.Int8(),
    "sale_status": pl.Int8(),
    "complete": pl.Boolean(),
}

# One row per scan.
SCANS_SCHEMA: dict[str, pl.DataType] = {
    "scan_id": pl.String(),
    "source": pl.String(),
    "api": pl.String(),
    "addon_version": pl.String(),
    "schema_version": pl.Int32(),
    "realm": pl.String(),
    "faction": pl.String(),
    "started_at": UTC_TS,
    "finished_at": UTC_TS,
    "listed": pl.Int64(),
    "row_count": pl.Int64(),
    "incomplete": pl.Int64(),
    "ingested_at": UTC_TS,
}
