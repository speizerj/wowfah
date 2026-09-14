from __future__ import annotations

from pathlib import Path

import pytest

from wowfah.cli import main
from wowfah.query import connect


def test_views_exist_and_join(data_dir: Path):
    con = connect(data_dir)
    n_scans, n_auctions = con.execute(
        "SELECT count(DISTINCT s.scan_id), count(*) FROM auctions a JOIN scans s USING (scan_id)"
    ).fetchone()
    assert (n_scans, n_auctions) == (3, 1200)


def test_unit_buyout_excludes_bid_only_auctions(data_dir: Path):
    con = connect(data_dir)
    bad = con.execute("""
        SELECT count(*) FROM auctions
        WHERE (buyout = 0 AND unit_buyout IS NOT NULL)
           OR (buyout > 0 AND abs(unit_buyout * count - buyout) > 1e-6)
    """).fetchone()[0]
    assert bad == 0
    assert con.execute("SELECT count(*) FROM auctions WHERE buyout = 0").fetchone()[0] > 0


def test_item_prices_aggregates(data_dir: Path):
    con = connect(data_dir)
    listings, quantity = con.execute(
        "SELECT sum(listings), sum(quantity) FROM item_prices"
    ).fetchone()
    expected = con.execute("SELECT count(*), sum(count) FROM auctions").fetchone()
    assert (listings, quantity) == expected

    # Dummy prices drift up 5% per scan, so Linen Cloth's median should rise.
    medians = [r[0] for r in con.execute("""
        SELECT median_unit_buyout FROM item_prices WHERE item_id = 2589 ORDER BY scanned_at
    """).fetchall()]
    assert len(medians) == 3
    assert medians[0] < medians[-1]


def test_connect_without_data_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        connect(tmp_path)


def test_cli_dummy_ingest_sql(tmp_path: Path, capsys):
    sv = tmp_path / "sv" / "WoWFAH.lua"
    data = tmp_path / "data"
    assert main(["dummy", str(sv), "--scans", "2", "--auctions", "50"]) == 0
    assert main(["ingest", str(sv), "--data-dir", str(data)]) == 0
    assert "2 scan(s) written" in capsys.readouterr().out
    assert main(["sql", "SELECT count(*) AS n FROM auctions", "--data-dir", str(data)]) == 0
    assert "100" in capsys.readouterr().out
