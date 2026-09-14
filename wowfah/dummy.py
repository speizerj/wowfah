"""Deterministic fake WoWFAH_DB data for tests and pipeline development."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from wowfah.schema import ROW_FORMAT, SUPPORTED_SCHEMA_VERSION

FIELD_SEP = "\t"


@dataclass(frozen=True)
class Item:
    item_id: int
    name: str
    quality: int
    level: int
    max_stack: int
    unit_price: int  # typical copper per unit


CATALOG = [
    Item(2589, "Linen Cloth", 1, 5, 20, 12),
    Item(2770, "Copper Ore", 1, 10, 20, 45),
    Item(2447, "Peacebloom", 1, 5, 20, 20),
    Item(4306, "Silk Cloth", 1, 26, 20, 80),
    Item(14047, "Runecloth", 1, 46, 20, 350),
    Item(12359, "Thorium Bar", 1, 50, 20, 900),
    Item(12360, "Arcanite Bar", 2, 55, 20, 60000),
    Item(13468, "Black Lotus", 1, 60, 20, 250000),
    Item(7909, "Aquamarine", 2, 45, 20, 2500),
    Item(2459, "Swiftness Potion", 1, 5, 5, 300),
    Item(13444, "Major Mana Potion", 1, 49, 5, 1500),
    Item(12804, "Powerful Mojo", 1, 55, 10, 1200),
    Item(14551, "Edgemaster's Handguards", 4, 44, 1, 5_000_000),
    Item(2575, "Red Linen Shirt", 1, 12, 1, 400),
]

SELLERS = [
    "Thrall", "Jaina", "Rexxar", "Mograine", "Pyrewood", "Bigmoney",
    "Stackz", "Herbalina", "Oreganon", "Clothpants", "Flipper", "Tinkerbolt",
]

TIME_LEFT_WEIGHTS = {1: 1, 2: 2, 3: 4, 4: 6}  # short .. very long


def pack_row(values: list[Any]) -> str:
    out = []
    for v in values:
        if v is None:
            out.append("")
        elif isinstance(v, bool):
            out.append("1" if v else "0")
        else:
            out.append(str(v))
    return FIELD_SEP.join(out)


def fake_auction(rng: random.Random, item: Item, price_drift: float) -> dict[str, Any]:
    count = 1 if item.max_stack == 1 else rng.choice([1, 5, 10, item.max_stack])
    unit = max(1, round(item.unit_price * price_drift * rng.lognormvariate(0, 0.25)))
    buyout = unit * count if rng.random() > 0.08 else 0
    min_bid = max(1, round((buyout or unit * count) * rng.uniform(0.6, 0.95)))
    has_bid = rng.random() < 0.1
    return {
        "item": item,
        "count": count,
        "min_bid": min_bid,
        "min_increment": max(1, min_bid // 20) if has_bid else 0,
        "buyout": buyout,
        "bid_amount": min_bid if has_bid else 0,
        "high_bidder": False,
        "owner": rng.choice(SELLERS),
        "time_left": rng.choices(list(TIME_LEFT_WEIGHTS), weights=list(TIME_LEFT_WEIGHTS.values()))[0],
        "sale_status": 0,
    }


def auction_row(a: dict[str, Any], complete: bool = True) -> str:
    item: Item = a["item"]
    return pack_row([
        item.item_id,
        f"item:{item.item_id}::::::::60:::::" if complete else f"item:{item.item_id}",
        item.name if complete else None,
        a["count"],
        item.quality,
        item.level,
        a["min_bid"],
        a["min_increment"],
        a["buyout"],
        a["bid_amount"],
        a["high_bidder"],
        a["owner"] if complete else None,
        a["time_left"],
        a["sale_status"],
        complete,
    ])


def generate_db(
    *,
    seed: int = 1,
    realm: str = "Dreamscythe",
    faction: str = "Alliance",
    scans: int = 3,
    auctions_per_scan: int = 500,
    start_ts: int = 1_789_646_400,  # 2026-09-17 12:00 UTC
    interval_s: int = 3 * 3600,
    incomplete_rate: float = 0.01,
    api: str = "classic",
) -> dict[str, Any]:
    rng = random.Random(seed)
    out_scans = []
    for s in range(scans):
        started = start_ts + s * interval_s
        drift = 1 + 0.05 * s
        rows = []
        incomplete = 0
        for _ in range(auctions_per_scan):
            complete = rng.random() >= incomplete_rate
            incomplete += not complete
            rows.append(auction_row(fake_auction(rng, rng.choice(CATALOG), drift), complete))
        out_scans.append({
            "scanId": f"{realm}-{faction}-{started}",
            "addonVersion": "0.1.0",
            "api": api,
            "realm": realm,
            "faction": faction,
            "startedAt": started,
            "finishedAt": started + 40,
            "listed": len(rows),
            "rowCount": len(rows),
            "incomplete": incomplete,
            "rowFormat": list(ROW_FORMAT),
            "rows": rows,
        })
    return {"schemaVersion": SUPPORTED_SCHEMA_VERSION, "scans": out_scans}
