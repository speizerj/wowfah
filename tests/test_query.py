from __future__ import annotations

from pathlib import Path

import pytest

from wowfah.cli import main
from wowfah.dummy import pack
from wowfah.ingest import ingest_db
from wowfah.query import connect
from wowfah.schema import LADDER_FORMAT, SUPPORTED_SCHEMA_VERSION


def hand_built(tmp_path: Path) -> Path:
    """One scan: Linen with a known ladder, Silk that timed out."""
    ladder = [
        pack([10, 20, 4, 1, 20]),  # 20 units @10
        pack([11, 10, 2, 3, 30]),  # 30 units @11
        pack([15, 20, 1, 2, 40]),  # 40 units @15
        pack([40, 10, 4, 1, 10]),  # 10 units @40
    ]
    items = [
        {"itemId": 2589, "name": "Linen Cloth", "status": "ok", "startedAt": 1789646400, "pages": 1,
         "listingsRead": 7, "quantity": 100, "bidOnlyListings": 1, "bidOnlyQuantity": 5, "ladder": ladder},
        {"itemId": 4306, "name": "Silk Cloth", "status": "timeout", "startedAt": 1789646410, "pages": 0,
         "listingsRead": 1, "quantity": 20, "ladder": [pack([80, 20, 4, 1, 20])]},
    ]
    db = {"schemaVersion": SUPPORTED_SCHEMA_VERSION, "scans": [{
        "scanId": "R-A-1", "realm": "R", "faction": "A", "status": "complete", "startedAt": 1789646400,
        "ladderFormat": list(LADDER_FORMAT), "items": items,
    }]}
    ingest_db(db, tmp_path / "data")
    return tmp_path / "data"


def test_views_exist_and_join(data_dir: Path):
    con = connect(data_dir)
    n_scans, n_items, n_market = con.execute("""
        SELECT count(DISTINCT s.scan_id), count(*), (SELECT count(*) FROM market)
        FROM item_scans i JOIN scans s USING (scan_id)
    """).fetchone()
    assert (n_scans, n_items, n_market) == (3, 27, 27)


def test_market_quantity_weighted_prices(tmp_path: Path):
    con = connect(hand_built(tmp_path))
    row = con.execute("""
        SELECT item_id, quantity, bid_only_quantity, min_unit_price, p10_unit_price, p25_unit_price,
               median_unit_price, expiring_quantity, very_long_quantity
        FROM market
    """).fetchall()
    # Silk timed out, so it isn't in the market. Cumulative units by price: 20, 50, 90, 100.
    assert row == [(2589, 100, 5, 10, 10, 11, 11, 70, 30)]


def test_buy_price_walks_the_ladder(tmp_path: Path):
    con = connect(hand_built(tmp_path))
    q = "SELECT avg_unit_price, max_unit_price, units, filled FROM buy_price({}) WHERE item_id = 2589"
    assert con.execute(q.format(20)).fetchone() == (10.0, 10, 20, True)
    assert con.execute(q.format(60)).fetchone() == (pytest.approx((20 * 10 + 30 * 11 + 10 * 15) / 60), 15, 60, True)
    assert con.execute(q.format(500)).fetchone() == (pytest.approx((20 * 10 + 30 * 11 + 40 * 15 + 10 * 40) / 100), 40, 100, False)
    assert con.execute("SELECT count(*) FROM buy_price(10) WHERE item_id = 4306").fetchone()[0] == 0


def test_market_prices_follow_dummy_drift(data_dir: Path):
    con = connect(data_dir)
    medians = [r[0] for r in con.execute(
        "SELECT median_unit_price FROM market WHERE item_id = 14047 ORDER BY scanned_at"
    ).fetchall()]
    assert len(medians) == 3 and medians[0] < medians[-1]


def test_watchlist_view(data_dir: Path, tmp_path: Path):
    csv = tmp_path / "wl.csv"
    csv.write_text("# comment\nitem_id,name,category,bracket,tags\n2589,Linen Cloth,cloth,1-20,leveling;cheap\n"
                   "13468,\"Black Lotus\",herb,55-60,\n")
    con = connect(data_dir, watchlist_csv=csv)
    assert con.execute("SELECT item_id, category, tags FROM watchlist ORDER BY item_id").fetchall() == [
        (2589, "cloth", ["leveling", "cheap"]), (13468, "herb", [])]
    assert "watchlist" not in {r[0] for r in connect(data_dir, watchlist_csv=None).execute("SHOW TABLES").fetchall()}


def test_connect_without_data_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        connect(tmp_path)


def test_cli_dummy_ingest_sql(tmp_path: Path, capsys):
    sv = tmp_path / "sv" / "WoWFAH.lua"
    data = tmp_path / "data"
    assert main(["dummy", str(sv), "--scans", "2"]) == 0
    assert main(["ingest", str(sv), "--data-dir", str(data)]) == 0
    assert "2 scan(s) written" in capsys.readouterr().out
    assert main(["sql", "SELECT count(*) AS n FROM market", "--data-dir", str(data)]) == 0
    assert "18" in capsys.readouterr().out
