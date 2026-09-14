from __future__ import annotations

import random
from pathlib import Path

import polars as pl
import pytest

from tests.wow_harness import WowClient
from wowfah import savedvars, watchlist
from wowfah.dummy import CATALOG, Item, Listing, fake_market, ladder_rows
from wowfah.ingest import ingest_savedvariables
from wowfah.query import connect
from wowfah.schema import LADDER_FORMAT

FLAVORS = ["classic", "modern"]
TIME_LEFT_SECONDS = {1: 600, 2: 3600, 3: 20_000, 4: 100_000}

LINEN, SILK, LOTUS = CATALOG[0], CATALOG[1], CATALOG[6]
# Same name as Black Lotus, different item id: an exact-name search returns both.
FAKE_LOTUS = Item(999_001, "Black Lotus", "herb", 20, 1, 30)


def to_mock(listings: list[Listing], unreadable: set[int] = frozenset()) -> list[dict]:
    return [
        {
            "itemId": li.item.item_id,
            "name": li.item.name,
            "count": li.count,
            "buyout": li.buyout,
            "minBid": li.min_bid,
            "timeLeft": li.time_left,
            "timeLeftSeconds": TIME_LEFT_SECONDS[li.time_left],
            "unreadable": i in unreadable,
        }
        for i, li in enumerate(listings)
    ]


def listings_for(item: Item, n: int, seed: int = 3) -> list[Listing]:
    """Exactly n fake listings for one item."""
    rng = random.Random(seed)
    out: list[Listing] = []
    while len(out) < n:
        out += fake_market(rng, item)
    return out[:n]


def open_client(flavor: str, listings: list[Listing], entries: list[Item], saved: dict | None = None,
                **mock) -> WowClient:
    client = WowClient(flavor)
    client.load_addon(saved)
    client.set_watchlist([(i.item_id, i.name, i.category) for i in entries])
    client.set_auctions(to_mock(listings))
    for k, v in mock.items():
        setattr(client.mock, k, v)
    client.fire("AUCTION_HOUSE_SHOW")
    return client


def scan_items(client: WowClient) -> dict[int, dict]:
    [scan] = client.db["scans"]
    return {item["itemId"]: item for item in scan["items"]}


def decoded_ladder(item: dict) -> list[list[int]]:
    rows = item["ladder"] if isinstance(item["ladder"], list) else []
    return [[int(v) for v in row.split("\t")] for row in rows]


def test_ladder_format_matches_python_schema():
    client = WowClient("classic")
    client.load_addon()
    assert savedvars.lua_to_python(client.ns.LADDER_FORMAT) == LADDER_FORMAT


def test_generated_watchlist_is_up_to_date():
    entries = watchlist.load()
    assert watchlist.DEFAULT_LUA.read_text() == watchlist.to_lua(entries), "run `wowfah watchlist export`"
    client = WowClient("classic")
    client.load_addon()
    loaded = savedvars.lua_to_python(client.ns.WATCHLIST)
    assert loaded == [[e.item_id, e.name, e.category] for e in entries]


def test_detects_api_flavor():
    for flavor in FLAVORS:
        client = WowClient(flavor)
        client.load_addon()
        assert client.ns.DetectAdapter().name == flavor


def test_classic_reads_every_page_and_ignores_same_named_items():
    linen = listings_for(LINEN, 260)  # 6 pages
    lotus = listings_for(LOTUS, 20, seed=4)
    decoys = listings_for(FAKE_LOTUS, 40, seed=5)
    silk = listings_for(SILK, 30, seed=6)  # on the AH but not watched
    client = open_client("classic", linen + lotus + decoys + silk, [LINEN, LOTUS])
    client.slash("scan")
    client.run_timers()

    items = scan_items(client)
    assert set(items) == {LINEN.item_id, LOTUS.item_id}

    li = items[LINEN.item_id]
    assert li["status"] == "ok" and li["pages"] == 6
    assert li["reportedListings"] == li["listingsRead"] == 260
    assert decoded_ladder(li) == ladder_rows(linen)
    assert li["quantity"] == sum(x.count for x in linen if x.buyout > 0)
    assert li["bidOnlyListings"] == sum(1 for x in linen if x.buyout == 0)
    assert li["bidOnlyQuantity"] == sum(x.count for x in linen if x.buyout == 0)

    lo = items[LOTUS.item_id]
    assert lo["reportedListings"] == 60 and lo["pages"] == 2  # the server counts decoys too
    assert lo["listingsRead"] == 20
    assert decoded_ladder(lo) == ladder_rows(lotus)

    log = savedvars.lua_to_python(client.mock.queryLog)
    assert [(q["text"], q["page"]) for q in log] == [("Linen Cloth", p) for p in range(6)] + [
        ("Black Lotus", 0), ("Black Lotus", 1)]
    assert all(q["exactMatch"] is True and q["getAll"] is False for q in log)
    assert client.mock.throttleViolations == 0
    assert any("scan complete: 2/2" in m for m in client.messages)


def test_modern_requests_more_until_full():
    linen = listings_for(LINEN, 350)
    client = open_client("modern", linen, [LINEN])
    client.slash("scan")
    client.run_timers()

    item = scan_items(client)[LINEN.item_id]
    buyouts = [x for x in linen if x.buyout > 0]
    assert item["status"] == "ok"
    assert item["pages"] == -(-len(buyouts) // 100)
    assert item["listingsRead"] == len(buyouts)
    assert "reportedListings" not in item
    assert decoded_ladder(item) == ladder_rows(linen, divisible=True)
    kinds = [q["kind"] for q in savedvars.lua_to_python(client.mock.queryLog)]
    assert kinds == ["search"] + ["more"] * (item["pages"] - 1)
    assert client.mock.throttleViolations == 0


def test_modern_marks_non_commodities():
    client = open_client("modern", listings_for(LINEN, 10), [LINEN, SILK])
    client.mock.nonCommodity[SILK.item_id] = True
    client.slash("scan")
    client.run_timers()
    items = scan_items(client)
    assert items[LINEN.item_id]["status"] == "ok"
    assert items[SILK.item_id]["status"] == "not_commodity"


@pytest.mark.parametrize("flavor", FLAVORS)
def test_waits_for_throttle_between_queries(flavor: str):
    client = open_client(flavor, listings_for(LINEN, 260), [LINEN, SILK], queryCooldown=4.0)
    client.slash("scan")
    client.run_timers()
    assert client.mock.throttleViolations == 0
    assert len(client.db["scans"][0]["items"]) == 2


def test_category_filter():
    client = open_client("classic", listings_for(LINEN, 5) + listings_for(LOTUS, 5), [LINEN, SILK, LOTUS])
    client.slash("scan cloth")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["category"] == "cloth" and scan["itemsRequested"] == 2
    assert set(scan_items(client)) == {LINEN.item_id, SILK.item_id}

    client.slash("scan nope")
    assert any("no watched items in category nope" in m for m in client.messages)


def test_empty_market_is_ok_with_empty_ladder():
    client = open_client("classic", [], [SILK])
    client.slash("scan")
    client.run_timers()
    item = scan_items(client)[SILK.item_id]
    assert item["status"] == "ok" and item["listingsRead"] == 0 and item["pages"] == 1
    assert decoded_ladder(item) == []


def test_unreadable_rows_are_counted():
    linen = listings_for(LINEN, 10)
    client = open_client("classic", [], [LINEN])
    client.set_auctions(to_mock(linen, unreadable={2, 7}))
    client.slash("scan")
    client.run_timers()
    item = scan_items(client)[LINEN.item_id]
    assert item["unreadable"] == 2 and item["listingsRead"] == 8


def test_unanswered_item_times_out_and_scan_continues():
    client = open_client("classic", listings_for(LINEN, 5) + listings_for(SILK, 5), [SILK, LINEN])
    client.mock.silentNames["Silk Cloth"] = True
    client.slash("scan")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["status"] == "complete"
    items = scan_items(client)
    assert items[SILK.item_id]["status"] == "timeout"
    assert items[LINEN.item_id]["status"] == "ok"


def test_repeated_timeouts_abort_but_keep_finished_items():
    entries = [LINEN, SILK, CATALOG[2], CATALOG[3], CATALOG[4]]
    client = open_client("classic", listings_for(LINEN, 5), entries)
    for item in entries[1:]:
        client.mock.silentNames[item.name] = True
    client.slash("scan")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["status"] == "aborted" and scan["itemsRequested"] == 5
    assert [i["status"] for i in scan["items"]] == ["ok", "timeout", "timeout", "timeout"]
    assert any("stopped answering" in m for m in client.messages)


def test_closing_auction_house_keeps_finished_items():
    client = open_client("classic", listings_for(LINEN, 5) + listings_for(SILK, 120), [LINEN, SILK])
    client.slash("scan")
    # Run until Linen is done and Silk is mid-scan, then close the AH.
    while len(client.ns.Scan.state["items"]) == 0 or client.ns.Scan.state["item"] is None:
        client.run_timers(1)
    client.fire("AUCTION_HOUSE_CLOSED")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["status"] == "aborted"
    assert [i["itemId"] for i in scan["items"]] == [LINEN.item_id]
    assert any("aborted: auction house closed" in m for m in client.messages)


def test_abort_before_any_item_stores_nothing():
    client = open_client("classic", listings_for(LINEN, 5), [LINEN])
    client.slash("scan")
    client.slash("abort")
    client.run_timers()
    assert client.db["scans"] in ([], {})


def test_scan_requires_open_auction_house():
    client = WowClient("classic")
    client.load_addon()
    client.slash("scan")
    assert len(client.mock.queryLog) == 0
    assert any("open the auction house" in m for m in client.messages)


def test_second_scan_while_running_is_rejected():
    client = open_client("classic", listings_for(LINEN, 5), [LINEN])
    client.slash("scan")
    client.slash("scan")
    assert any("already running" in m for m in client.messages)
    assert len(client.mock.queryLog) == 1


def test_status_list_and_clear():
    client = open_client("classic", listings_for(LINEN, 5), [LINEN, SILK, LOTUS])
    client.slash("list")
    assert any("3 watched items: cloth (2), herb (1)" in m for m in client.messages)

    client.slash("scan")
    client.slash("status")
    assert any("scan running: item 1/3 Linen Cloth, page 1" in m for m in client.messages)
    client.run_timers()
    client.slash("status")
    assert any("1 stored scan(s), 3 item scans" in m for m in client.messages)

    client.slash("clear")
    assert len(client.db["scans"]) == 1
    client.slash("clear confirm")
    assert client.db["scans"] in ([], {})


def test_old_schema_scans_are_discarded_on_load():
    client = open_client("classic", [], [SILK], saved={"schemaVersion": 1, "scans": [{"scanId": "old"}]})
    db = client.db
    assert db["schemaVersion"] == 2 and db["scans"] in ([], {})


@pytest.mark.parametrize("flavor", FLAVORS)
def test_end_to_end_addon_to_market_view(flavor: str, tmp_path: Path):
    linen = listings_for(LINEN, 180)
    silk = listings_for(SILK, 40, seed=9)
    client = open_client(flavor, linen + silk, [LINEN, SILK])
    client.slash("scan")
    client.run_timers()

    sv = tmp_path / "WoWFAH.lua"
    savedvars.dump({"WoWFAH_DB": client.db}, sv)
    [result] = ingest_savedvariables(sv, tmp_path / "data")
    assert result.status == "written" and result.items == 2

    divisible = flavor == "modern"
    expected = len(ladder_rows(linen, divisible)) + len(ladder_rows(silk, divisible))
    assert result.ladder_rows == expected

    con = connect(tmp_path / "data")
    quantity = dict(con.execute("SELECT item_id, quantity FROM market").fetchall())
    assert quantity == {
        LINEN.item_id: sum(x.count for x in linen if x.buyout > 0),
        SILK.item_id: sum(x.count for x in silk if x.buyout > 0),
    }
    ladder = pl.read_parquet(tmp_path / "data" / "ladder" / "*.parquet")
    assert ladder.filter(pl.col("item_id") == LINEN.item_id)["quantity"].sum() == quantity[LINEN.item_id]
