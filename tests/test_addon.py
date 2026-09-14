from __future__ import annotations

import random
from pathlib import Path

import polars as pl
import pytest

from tests.wow_harness import WowClient
from wowfah import savedvars
from wowfah.dummy import CATALOG, fake_auction
from wowfah.ingest import ingest_savedvariables
from wowfah.schema import ROW_FORMAT

FLAVORS = ["classic", "modern"]


def mock_auctions(n: int, seed: int = 3, uncached: dict[int, int] | None = None) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        a = fake_auction(rng, rng.choice(CATALOG), 1.0)
        item = a["item"]
        out.append({
            "itemId": item.item_id,
            "name": item.name,
            "quality": item.quality,
            "level": item.level,
            "count": a["count"],
            "minBid": a["min_bid"],
            "minIncrement": a["min_increment"],
            "buyout": a["buyout"],
            "bidAmount": a["bid_amount"],
            "highBidder": a["high_bidder"],
            "owner": a["owner"],
            "timeLeft": a["time_left"],
            "saleStatus": a["sale_status"],
            "uncachedReads": (uncached or {}).get(i, 0),
        })
    return out


def open_client(flavor: str, auctions: list[dict], saved: dict | None = None) -> WowClient:
    client = WowClient(flavor)
    client.load_addon(saved)
    client.set_auctions(auctions)
    client.fire("AUCTION_HOUSE_SHOW")
    return client


def test_row_format_matches_python_schema():
    client = WowClient("classic")
    client.load_addon()
    assert savedvars.lua_to_python(client.ns.ROW_FORMAT) == ROW_FORMAT


def test_detects_api_flavor():
    for flavor in FLAVORS:
        client = WowClient(flavor)
        client.load_addon()
        assert client.ns.DetectAdapter().name == flavor


@pytest.mark.parametrize("flavor", FLAVORS)
def test_full_scan_stores_every_auction(flavor: str):
    # 2500 rows exercises chunked reading across several timer ticks.
    auctions = mock_auctions(2500)
    client = open_client(flavor, auctions)
    client.slash("scan")
    client.run_timers()

    db = client.db
    assert db["schemaVersion"] == 1
    [scan] = db["scans"]
    assert scan["api"] == flavor
    assert scan["rowCount"] == scan["listed"] == 2500
    assert scan["incomplete"] == 0
    assert scan["realm"] == "Dreamscythe" and scan["faction"] == "Alliance"
    assert scan["scanId"] == f"Dreamscythe-Alliance-{scan['startedAt']}"

    first = dict(zip(ROW_FORMAT, scan["rows"][0].split("\t")))
    a = auctions[0]
    assert first["itemId"] == str(a["itemId"])
    assert first["itemString"] == f"item:{a['itemId']}::::::::60:::::"
    assert first["name"] == a["name"]
    assert first["owner"] == a["owner"]
    assert first["buyout"] == str(a["buyout"])
    assert first["complete"] == "1"
    assert any("scan complete: 2500" in m for m in client.messages)


def test_classic_uses_get_all_query():
    client = open_client("classic", mock_auctions(5))
    client.slash("scan")
    get_all = savedvars.lua_to_python(client.mock.lastQuery)
    # QueryAuctionItems(text, minLevel, maxLevel, page, usable, rarity, getAll, ...)
    assert get_all[1] == "" and get_all[4] == 0 and get_all[7] is True


def test_uncached_rows_are_retried_until_complete():
    # Row 1 resolves after the initial read + 2 retries; row 3 never resolves.
    auctions = mock_auctions(5, uncached={1: 3, 3: 999})
    client = open_client("classic", auctions)
    client.slash("scan")
    client.run_timers()

    [scan] = client.db["scans"]
    rows = [dict(zip(ROW_FORMAT, r.split("\t"))) for r in scan["rows"]]
    assert scan["rowCount"] == 5
    assert scan["incomplete"] == 1
    assert rows[1]["complete"] == "1" and rows[1]["name"] == auctions[1]["name"]
    assert rows[3]["complete"] == "0"
    assert rows[3]["name"] == "" and rows[3]["itemString"] == f"item:{auctions[3]['itemId']}"


def test_scan_requires_open_auction_house():
    client = WowClient("classic")
    client.load_addon()
    client.slash("scan")
    assert client.mock.queries == 0
    assert any("open the auction house" in m for m in client.messages)


def test_classic_throttle_blocks_scan():
    client = open_client("classic", mock_auctions(5))
    client.mock.canQueryAll = False
    client.slash("scan")
    assert client.mock.queries == 0
    assert any("15 minutes" in m for m in client.messages)


def test_closing_auction_house_aborts_scan():
    client = open_client("modern", mock_auctions(5))
    client.slash("scan")
    client.fire("AUCTION_HOUSE_CLOSED")
    client.run_timers()
    assert client.db["scans"] in ([], {})
    assert any("aborted: auction house closed" in m for m in client.messages)


def test_timeout_aborts_when_list_never_arrives():
    client = open_client("classic", mock_auctions(5))
    client.mock.listDelay = 10_000
    client.slash("scan")
    client.run_timers()
    assert client.db["scans"] in ([], {})
    assert any("timed out" in m for m in client.messages)


def test_second_scan_while_running_is_rejected():
    client = open_client("classic", mock_auctions(5))
    client.slash("scan")
    client.slash("scan")
    assert client.mock.queries == 1


def test_scans_accumulate_and_clear_needs_confirm():
    existing = {"schemaVersion": 1, "scans": [{"scanId": "old", "rowCount": 7}]}
    client = open_client("classic", mock_auctions(5), saved=existing)
    client.slash("scan")
    client.run_timers()
    assert len(client.db["scans"]) == 2

    client.slash("status")
    assert any("2 stored scan(s), 12 auction rows" in m for m in client.messages)

    client.slash("clear")
    assert len(client.db["scans"]) == 2
    client.slash("clear confirm")
    assert client.db["scans"] in ([], {})


def test_pack_row_strips_separators():
    client = WowClient("classic")
    client.load_addon()
    packed = client.ns.PackRow(client.to_lua([1, "a\tb\nc"]))
    assert packed.split("\t")[:2] == ["1", "a b c"]
    assert len(packed.split("\t")) == len(ROW_FORMAT)


@pytest.mark.parametrize("flavor", FLAVORS)
def test_end_to_end_addon_to_parquet(flavor: str, tmp_path: Path):
    auctions = mock_auctions(300, uncached={10: 999})
    client = open_client(flavor, auctions)
    client.slash("scan")
    client.run_timers()

    sv = tmp_path / "WoWFAH.lua"
    savedvars.dump({"WoWFAH_DB": client.db}, sv)
    [result] = ingest_savedvariables(sv, tmp_path / "data")
    assert result.status == "written" and result.rows == 300

    df = pl.read_parquet(tmp_path / "data" / "auctions" / "*.parquet")
    assert df["complete"].not_().sum() == 1
    expected = pl.DataFrame({
        "item_id": [a["itemId"] for a in auctions],
        "count": [a["count"] for a in auctions],
        "buyout": [a["buyout"] for a in auctions],
    })
    assert df.select("item_id", "count", "buyout").cast(pl.Int64).equals(expected.cast(pl.Int64))
