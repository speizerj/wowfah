"""Deterministic fake WoWFAH_DB data for tests and pipeline development."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from wowfah.schema import LADDER_FORMAT, SUPPORTED_SCHEMA_VERSION

FIELD_SEP = "\t"


@dataclass(frozen=True)
class Item:
    item_id: int
    name: str
    category: str
    max_stack: int
    unit_price: int  # typical copper per unit
    depth: int  # typical number of listings


CATALOG = [
    Item(2589, "Linen Cloth", "cloth", 20, 12, 180),
    Item(4306, "Silk Cloth", "cloth", 20, 80, 120),
    Item(14047, "Runecloth", "cloth", 20, 350, 220),
    Item(2770, "Copper Ore", "ore", 10, 45, 90),
    Item(12359, "Thorium Bar", "bar", 20, 900, 70),
    Item(2447, "Peacebloom", "herb", 20, 20, 140),
    Item(13468, "Black Lotus", "herb", 20, 250000, 12),
    Item(13444, "Major Mana Potion", "potion", 5, 1500, 60),
    Item(13510, "Flask of the Titans", "flask", 5, 400000, 8),
]

TIME_LEFT_WEIGHTS = {1: 1, 2: 2, 3: 4, 4: 6}  # short .. very long


@dataclass(frozen=True)
class Listing:
    item: Item
    count: int
    buyout: int  # whole stack; 0 = bid-only
    min_bid: int
    time_left: int


def fake_listing(rng: random.Random, item: Item, price_drift: float = 1.0) -> Listing:
    count = rng.choice(sorted({1, min(5, item.max_stack), min(10, item.max_stack), item.max_stack}))
    unit = max(1, round(item.unit_price * price_drift * rng.lognormvariate(0, 0.2)))
    buyout = unit * count if rng.random() > 0.05 else 0
    return Listing(
        item=item,
        count=count,
        buyout=buyout,
        min_bid=max(1, round(unit * count * rng.uniform(0.6, 0.95))),
        time_left=rng.choices(list(TIME_LEFT_WEIGHTS), weights=list(TIME_LEFT_WEIGHTS.values()))[0],
    )


def fake_market(rng: random.Random, item: Item, price_drift: float = 1.0, depth_scale: float = 1.0) -> list[Listing]:
    n = max(1, round(item.depth * depth_scale * rng.uniform(0.7, 1.3)))
    return [fake_listing(rng, item, price_drift) for _ in range(n)]


def unit_price(buyout: int, count: int) -> int:
    # Matches the addon's math.floor(buyout / count + 0.5).
    return int(buyout / count + 0.5)


def ladder_rows(listings: list[Listing], divisible: bool = False) -> list[list[int]]:
    """Aggregate listings the way the addon does. divisible=True mimics modern commodities (stack size 0)."""
    agg: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])
    for li in listings:
        if li.buyout <= 0:
            continue
        key = (unit_price(li.buyout, li.count), 0 if divisible else li.count, li.time_left)
        agg[key][0] += 1
        agg[key][1] += li.count
    return [[*key, n, qty] for key, (n, qty) in sorted(agg.items())]


def pack(values: list[Any]) -> str:
    return FIELD_SEP.join("" if v is None else str(v) for v in values)


def item_record(item: Item, listings: list[Listing], started: int, status: str = "ok") -> dict[str, Any]:
    bid_only = [li for li in listings if li.buyout <= 0]
    return {
        "itemId": item.item_id,
        "name": item.name,
        "status": status,
        "startedAt": started,
        "finishedAt": started + 2,
        "pages": max(1, -(-len(listings) // 50)),
        "reportedListings": len(listings),
        "listingsRead": len(listings),
        "quantity": sum(li.count for li in listings if li.buyout > 0),
        "bidOnlyListings": len(bid_only),
        "bidOnlyQuantity": sum(li.count for li in bid_only),
        "unreadable": 0,
        "ladder": [pack(r) for r in ladder_rows(listings)],
    }


def generate_db(
    *,
    seed: int = 1,
    realm: str = "Dreamscythe",
    faction: str = "Alliance",
    scans: int = 3,
    depth_scale: float = 1.0,
    start_ts: int = 1_789_646_400,  # 2026-09-17 12:00 UTC
    interval_s: int = 3 * 3600,
    api: str = "classic",
    catalog: list[Item] = CATALOG,
) -> dict[str, Any]:
    rng = random.Random(seed)
    out_scans = []
    for s in range(scans):
        started = start_ts + s * interval_s
        drift = 1 + 0.05 * s
        items = []
        for i, item in enumerate(catalog):
            listings = fake_market(rng, item, drift, depth_scale)
            items.append(item_record(item, listings, started + 5 * i))
        out_scans.append({
            "scanId": f"{realm}-{faction}-{started}",
            "addonVersion": "0.2.0",
            "api": api,
            "realm": realm,
            "faction": faction,
            "status": "complete",
            "startedAt": started,
            "finishedAt": started + 5 * len(catalog),
            "itemsRequested": len(catalog),
            "itemsScanned": len(items),
            "ladderFormat": list(LADDER_FORMAT),
            "items": items,
        })
    return {"schemaVersion": SUPPORTED_SCHEMA_VERSION, "scans": out_scans}
