"""Parquet schemas shared by every data source (addon scans now, Blizzard Web API dumps later)."""

from __future__ import annotations

import polars as pl

SUPPORTED_SCHEMA_VERSION = 2

# Packed ladder row field order written by the addon (addon/WoWFAH/Core.lua ns.LADDER_FORMAT).
LADDER_FORMAT = ["unitPrice", "stackSize", "timeLeft", "listings", "quantity"]

# Addon ladder field -> Parquet column.
LADDER_FIELD_COLUMNS = {
    "unitPrice": "unit_price",
    "stackSize": "stack_size",
    "timeLeft": "time_left",
    "listings": "listings",
    "quantity": "quantity",
}

UTC_TS = pl.Datetime("ms", "UTC")

# One row per scan.
SCANS_SCHEMA: dict[str, pl.DataType] = {
    "scan_id": pl.String(),
    "source": pl.String(),
    "api": pl.String(),
    "addon_version": pl.String(),
    "schema_version": pl.Int32(),
    "realm": pl.String(),
    "faction": pl.String(),
    "category": pl.String(),  # category filter the scan was started with; null = whole watchlist
    "status": pl.String(),  # "complete" or "aborted"
    "started_at": UTC_TS,
    "finished_at": UTC_TS,
    "items_requested": pl.Int32(),
    "items_scanned": pl.Int32(),
    "ingested_at": UTC_TS,
}

# One row per watched item per scan, including items that failed.
ITEM_SCANS_SCHEMA: dict[str, pl.DataType] = {
    "scan_id": pl.String(),
    "realm": pl.String(),
    "faction": pl.String(),
    "item_id": pl.Int64(),
    "name": pl.String(),
    "status": pl.String(),  # "ok", "timeout", "not_commodity", "error"
    "capped": pl.Boolean(),  # ok, but stopped at maxPages before the whole market was read
    "started_at": UTC_TS,
    "finished_at": UTC_TS,
    "pages": pl.Int32(),
    "reported_listings": pl.Int64(),  # total the server reported (classic); null when the API doesn't say
    "reported_quantity": pl.Int64(),  # total units in the whole market (modern aggregate call)
    "listings_read": pl.Int64(),
    "quantity": pl.Int64(),  # units with a buyout
    "bid_only_listings": pl.Int64(),
    "bid_only_quantity": pl.Int64(),
    "unreadable": pl.Int64(),  # rows the client returned without an item id
}

# Buyout price ladder: one row per (item, unit price, stack size, time left) per scan.
# Money is copper per unit. stack_size 0 means the API sells partial quantities (modern commodities).
# time_left: 1 short (<30m), 2 medium (<2h), 3 long (<12h), 4 very long; 0 unknown.
LADDER_SCHEMA: dict[str, pl.DataType] = {
    "scan_id": pl.String(),
    "realm": pl.String(),
    "faction": pl.String(),
    "scanned_at": UTC_TS,  # when this item was scanned, not when the scan started
    "item_id": pl.Int64(),
    "unit_price": pl.Int64(),
    "stack_size": pl.Int32(),
    "time_left": pl.Int8(),
    "listings": pl.Int64(),
    "quantity": pl.Int64(),
}

# Item metadata from the client's DB2 tables.
ITEMS_SCHEMA: dict[str, pl.DataType] = {
    "item_id": pl.Int64(),
    "name": pl.String(),
    "class_id": pl.Int32(),
    "subclass_id": pl.Int32(),
    "quality": pl.Int8(),
    "item_level": pl.Int32(),
    "required_level": pl.Int32(),
    "max_stack": pl.Int32(),
    "bonding": pl.Int8(),  # 0 none, 1 on pickup, 2 on equip, 3 on use, 4 quest
    "sell_price": pl.Int64(),  # vendor pays, copper per unit
    "buy_price": pl.Int64(),  # vendor charges, copper per VendorStackCount
    "is_commodity": pl.Boolean(),
    "source_branch": pl.String(),
}
