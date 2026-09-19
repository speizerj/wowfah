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


def test_modern_one_query_per_item_with_total_depth():
    # One search returns the market grouped into price tiers; the addon never pages. If the
    # server holds rows back, the ladder is the cheap end and the total comes from the aggregate.
    linen = listings_for(LINEN, 350)  # more buyout tiers than the mock returns per search
    silk = listings_for(SILK, 20, seed=5)  # fits in one reply
    client = open_client("modern", linen + silk, [LINEN, SILK])
    client.slash("scan")
    client.run_timers()

    items = scan_items(client)
    li, si = items[LINEN.item_id], items[SILK.item_id]
    assert li["pages"] == 1 and li["capped"] is True and li["listingsRead"] == 100
    assert li["reportedQuantity"] == sum(x.count for x in linen if x.buyout > 0) > li["quantity"]
    assert si["capped"] is False and si["reportedQuantity"] == si["quantity"]
    assert decoded_ladder(si) == ladder_rows(silk, divisible=True)
    kinds = [q["kind"] for q in savedvars.lua_to_python(client.mock.queryLog)]
    assert kinds == ["search", "search"]


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
    assert any("too many items in a row failed" in m for m in client.messages)


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
    # modernPageSize: the live server returned whole beta markets in one reply
    client = open_client(flavor, linen + silk, [LINEN, SILK], modernPageSize=10_000)
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


def probe_log(client: WowClient, index: int = -1) -> list[str]:
    return client.db["probes"][index]["log"]


def test_probe_logs_environment_pages_and_raw_rows(tmp_path: Path, capsys):
    client = open_client("classic", listings_for(LINEN, 260), [LINEN])
    client.slash("probe linen cloth 2")
    client.run_timers()

    db = client.db
    assert db["scans"] in ([], {}) and db["itemStats"] in ([], {})
    [probe] = db["probes"]
    assert probe["target"] == "Linen Cloth" and probe["api"] == "classic"
    assert probe["itemStatus"] == "ok" and probe["pages"] == 2 and probe["reportedListings"] == 260
    assert [q["page"] for q in savedvars.lua_to_python(client.mock.queryLog)] == [0, 1]

    log = "\n".join(probe["log"])
    assert 'build: "1.15.9", "69722", "Sep 1 2026", 11509' in log
    assert "globals: QueryAuctionItems=function" in log and "C_AuctionHouse: nil" in log
    assert "GetNumAuctionItems: 50, 260" in log
    assert '[1] "Linen Cloth", 136235, ' in log
    assert "answered after 0.200s" in log and "throttle cleared after" in log
    assert "page read: 50 listing(s), " in log and "unit(s) read so far, 0 unreadable, reported total 260, more: true" in log
    assert "[stray" not in log
    assert any("probe complete: ok, 2 page(s), 100 listing(s) read" in m for m in client.messages)

    sv = tmp_path / "WoWFAH.lua"
    savedvars.dump({"WoWFAH_DB": db}, sv)
    from wowfah.cli import main
    assert main(["probes", str(sv)]) == 0
    out = capsys.readouterr().out
    assert "== probe Linen Cloth (classic API, complete): item ok, 2 page(s), 100 read (" in out and "units), reported 260" in out
    assert "GetNumAuctionItems: 50, 260" in out


def test_probe_flags_duplicate_events():
    client = open_client("classic", listings_for(LINEN, 60), [LINEN], duplicateAnswers=True)
    client.slash("probe 2589")
    client.run_timers()
    log = probe_log(client)
    strays = [line for line in log if "[stray" in line]
    assert len(strays) == 2  # one duplicate per page
    assert client.db["probes"][0]["listingsRead"] == 60


def test_probe_modern_dumps_result_indexes():
    client = open_client("modern", listings_for(LINEN, 30), [LINEN])
    client.slash("probe Linen Cloth")
    client.run_timers()
    log = "\n".join(probe_log(client))
    assert "C_AuctionHouse: GetCommoditySearchResultInfo" in log
    assert "GetNumCommoditySearchResults: " in log and "HasFull: true" in log
    assert "[0] nil" in log and "[1] {itemID=2589, quantity=" in log


def test_probe_unknown_item_and_usage():
    client = open_client("classic", listings_for(SILK, 5), [LINEN])
    client.slash("probe")
    client.slash("probe Nothing Here")
    assert any("usage: /wowfah probe" in m for m in client.messages)
    assert any("unknown item Nothing Here" in m for m in client.messages)
    # Not watched, but in the client's item cache.
    client.slash("probe Silk Cloth")
    client.run_timers()
    assert client.db["probes"][0]["target"] == "Silk Cloth"


def test_only_recent_probes_are_kept():
    client = open_client("classic", listings_for(LINEN, 5), [LINEN])
    for _ in range(7):
        client.slash("probe 2589 1")
        client.run_timers()
    assert len(client.db["probes"]) == 5


def test_estimates_use_history_from_previous_scan():
    linen = listings_for(LINEN, 260)  # 6 pages
    client = open_client("classic", linen + listings_for(SILK, 80), [LINEN, SILK], queryCooldown=1.0)
    client.slash("scan")
    assert not any("roughly" in m for m in client.messages)  # no history yet
    client.run_timers()
    assert any("scan complete: 2/2 item(s) stored (0 not ok), 8 page(s) in" in m and "s/page" in m
               for m in client.messages)
    stats = client.db["itemStats"]
    assert stats[LINEN.item_id]["pages"] == 6 and stats[SILK.item_id]["pages"] == 2
    assert client.db["secPerPage"] > 0

    client.slash("clear confirm")
    client.slash("scan")
    assert any("scanning 2 item(s) at full depth (classic API), roughly " in m for m in client.messages)
    # Finish Linen's first page so the server's total is known.
    while client.ns.Scan.state["item"]["pagesRead"] < 1:
        client.run_timers(1)
    client.slash("status")
    progress = client.messages[-1]
    assert "scan running: item 1/2 Linen Cloth, page 2/6, " in progress
    assert " elapsed, ~" in progress and "left (" in progress and "s/page)" in progress


def test_format_duration():
    client = WowClient("classic")
    client.load_addon()
    fmt = client.ns.FormatDuration
    assert [fmt(4.4), fmt(59.6), fmt(125), fmt(3600 * 2 + 61)] == ["4s", "1m00s", "2m05s", "2h01m"]


@pytest.mark.parametrize("flavor", FLAVORS)
def test_scan_recovers_from_an_api_error_on_one_item(flavor: str):
    # A live client can throw on a call signature the addon guessed wrong. One item's error
    # shouldn't lose the rest of the scan -- see the pcall around ReadPage in Scan.lua.
    client = open_client(flavor, listings_for(LINEN, 5) + listings_for(SILK, 5), [SILK, LINEN])
    client.mock.brokenItems[SILK.item_id] = True
    client.slash("scan")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["status"] == "complete"
    items = scan_items(client)
    assert items[SILK.item_id]["status"] == "error"
    assert items[LINEN.item_id]["status"] == "ok"
    assert any("ReadPage error:" in m for m in client.messages) is False  # only in probe logs, not chat


def test_probe_survives_a_broken_diagnostic_call():
    # GetItemCommodityStatus threw on a real beta client for a valid item id (bad argument #1);
    # the probe must log that and keep going, not crash the whole event handler.
    client = open_client("modern", listings_for(LINEN, 5), [LINEN])

    def broken(item_id):
        raise ValueError("bad argument #1 to 'GetItemCommodityStatus'")

    client.mock.brokenItems[LINEN.item_id] = False  # ReadPage itself must still work
    client.lua.globals().C_AuctionHouse.GetItemCommodityStatus = client.lua.eval(
        "function(f) return function(...) return f(...) end end")(broken)
    client.slash("probe Linen Cloth")
    client.run_timers()

    log = "\n".join(probe_log(client))
    assert "GetItemCommodityStatus: ERROR:" in log and "bad argument" in log
    item = client.db["probes"][0]
    assert item["itemStatus"] == "ok" and item["listingsRead"] == 5  # ReadPage still ran fine


def test_repeated_errors_abort_like_repeated_timeouts():
    entries = [LINEN, SILK, CATALOG[2], CATALOG[3]]
    # Each broken item needs its own listings, or Classic's read loop (where checkBroken
    # fires) never runs for it -- an empty market for that item just reads as zero rows.
    auctions = [li for e in entries for li in listings_for(e, 5, seed=e.item_id)]
    client = open_client("classic", auctions, entries)
    for item in entries[1:]:
        client.mock.brokenItems[item.item_id] = True
    client.slash("scan")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["status"] == "aborted"
    assert [i["status"] for i in scan["items"]] == ["ok", "error", "error", "error"]


def test_scan_maxpages_arg_caps_and_flags_capped_items():
    flavor = "classic"  # modern never pages, so maxPages only matters here
    # A market with many price tiers (many pages) gets cut off; a small one finishes naturally
    # within the cap regardless. Both are status "ok", only the big one is capped.
    big = listings_for(LINEN, 400)  # several pages
    small = listings_for(SILK, 10, seed=11)  # fits in one page
    client = open_client(flavor, big + small, [LINEN, SILK])
    client.slash("scan 2")
    client.run_timers()

    items = scan_items(client)
    assert items[LINEN.item_id]["status"] == "ok" and items[LINEN.item_id]["capped"] is True
    assert items[LINEN.item_id]["pages"] == 2
    assert items[SILK.item_id]["status"] == "ok" and items[SILK.item_id]["capped"] is False

    # Capped items' page counts shouldn't poison future ETA estimates.
    stats = client.db["itemStats"]
    assert LINEN.item_id not in stats
    assert stats[SILK.item_id]["pages"] == items[SILK.item_id]["pages"]


def test_scan_arg_parsing_category_and_pages():
    client = open_client("classic", listings_for(LINEN, 3) + listings_for(LOTUS, 3), [LINEN, LOTUS])
    client.slash("scan herb 1")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["category"] == "herb"
    items = scan_items(client)
    assert set(items) == {LOTUS.item_id}
    assert items[LOTUS.item_id]["pages"] == 1


def test_end_to_end_capped_item_reaches_item_scans(tmp_path: Path):
    client = open_client("classic", listings_for(LINEN, 300), [LINEN])
    client.slash("scan 1")
    client.run_timers()

    sv = tmp_path / "WoWFAH.lua"
    savedvars.dump({"WoWFAH_DB": client.db}, sv)
    ingest_savedvariables(sv, tmp_path / "data")

    df = pl.read_parquet(tmp_path / "data" / "item_scans" / "*.parquet")
    assert df.row(0, named=True)["capped"] is True


def test_probe_logs_diagnostic_ah_events():
    client = open_client("modern", listings_for(LINEN, 5), [LINEN])
    client.mock.silentNames["Linen Cloth"] = True  # the result event never comes...
    client.slash("probe Linen Cloth 1")
    client.fire("AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED")  # ...because the server dropped it
    client.run_timers()
    log = "\n".join(probe_log(client))
    assert "event AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED() [diagnostic]" in log
    assert "no answer after 5s, resending (1/2)" in log and "timed out after 3 tries" in log
    # Diagnostic handlers are gone once the probe finishes.
    n = len(probe_log(client))
    client.fire("AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED")
    assert len(probe_log(client)) == n


@pytest.mark.parametrize("flavor", FLAVORS)
def test_lost_query_is_resent_and_scan_logs_it(flavor: str):
    client = open_client(flavor, listings_for(LINEN, 5), [LINEN])
    client.mock.dropNext["Linen Cloth"] = 1  # first query lost, the resend is answered
    client.slash("scan")
    client.run_timers()
    [scan] = client.db["scans"]
    assert scan["status"] == "complete"
    assert scan_items(client)[LINEN.item_id]["status"] == "ok"
    log = "\n".join(scan["log"])
    assert "no answer after 5s, resending (1/2)" in log
    assert "answered after" in log
    assert "GetNumCommoditySearchResults" not in log and "GetNumAuctionItems" not in log  # no row dumps in scans


def test_cli_probes_prints_scan_logs(tmp_path: Path, capsys):
    client = open_client("modern", listings_for(LINEN, 5), [LINEN])
    client.slash("scan")
    client.run_timers()
    sv = tmp_path / "WoWFAH.lua"
    savedvars.dump({"WoWFAH_DB": client.db}, sv)
    from wowfah.cli import main
    assert main(["probes", str(sv)]) == 0
    out = capsys.readouterr().out
    assert "== scan Dreamscythe-Alliance-" in out and "(complete): 1/1 item(s), 0 not ok" in out
    assert "query page 0" in out


def test_uncached_item_is_resent_as_soon_as_its_info_arrives():
    # Live beta: a search for an item the client hasn't cached only fetches its info and the
    # search is dropped. The scan must resend on ITEM_KEY_ITEM_INFO_RECEIVED, not wait 5s.
    client = open_client("modern", listings_for(LINEN, 5) + listings_for(SILK, 5, seed=2), [LINEN, SILK])
    client.mock.uncachedKeys[LINEN.item_id] = True
    client.mock.uncachedKeys[SILK.item_id] = True
    client.slash("scan")
    client.run_timers()

    [scan] = client.db["scans"]
    assert scan["status"] == "complete"
    assert all(i["status"] == "ok" for i in scan["items"])
    log = "\n".join(scan["log"])
    assert log.count("ITEM_KEY_ITEM_INFO_RECEIVED") == 2 and "resending now" in log
    assert "no answer after" not in log
    assert scan["finishedAt"] - scan["startedAt"] < 5


def panel(client):
    return client.ns.UI


def click(button):
    button.Click(button)


def test_panel_appears_on_the_ah_and_scans_with_a_click():
    client = open_client("modern", listings_for(LINEN, 5), [LINEN])
    ui = panel(client)
    assert client.lua.eval("function(ui) return ui.frame.parent == AuctionHouseFrame end")(ui)
    assert ui.scan.enabled and not ui.abort.enabled
    assert "ready" in ui.status.text

    click(ui.scan)
    assert not ui.scan.enabled and ui.abort.enabled
    assert "scan running: item 1/1 Linen Cloth" in ui.status.text
    client.run_timers()

    assert ui.scan.enabled and not ui.abort.enabled
    assert "last scan complete: 1/1 items" in ui.status.text
    assert "1 scan(s) not saved yet" in ui.status.text
    click(ui.save)
    assert client.mock.reloads == 1


def test_panel_abort_button_and_disabled_scan_when_ah_closed():
    client = open_client("modern", listings_for(LINEN, 5) + listings_for(SILK, 5, seed=2), [LINEN, SILK],
                         listDelay=1.0)
    ui = panel(client)
    click(ui.scan)
    click(ui.abort)
    client.run_timers()
    assert any("aborted by user" in m for m in client.messages)
    client.fire("AUCTION_HOUSE_CLOSED")
    assert not ui.scan.enabled


def test_old_scans_are_pruned_and_only_the_newest_keeps_its_log():
    client = open_client("modern", listings_for(LINEN, 3), [LINEN])
    for _ in range(12):
        client.slash("scan")
        client.run_timers()
        client.mock.now += 60  # distinct scan ids
    scans = client.db["scans"]
    assert len(scans) == 10
    assert all("log" not in s for s in scans[:-1]) and scans[-1]["log"]
