from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from wowfah import watchlist
from wowfah.cli import main
from wowfah.query import connect
from wowfah.schema import ITEM_SCANS_SCHEMA, LADDER_SCHEMA, SCANS_SCHEMA
from wowfah.signals import fetch_market, signals
from wowfah.simulate import Population, SimConfig, bot_factor, parse_bracket, simulate, write

SMALL = SimConfig(days=49, scans_per_day=3, categories=("herb", "cloth", "flask"), depth_scale=0.3, seed=11)


@pytest.fixture(scope="module")
def frames() -> dict[str, pl.DataFrame]:
    return simulate(SMALL)


@pytest.fixture(scope="module")
def sim_dir(tmp_path_factory, frames) -> Path:
    out = tmp_path_factory.mktemp("sim")
    write(frames, out)
    return out


def daily(frames, name: str, column: str = "fair_price") -> list[float]:
    ids = frames["item_scans"].filter(pl.col("name") == name)["item_id"].unique()
    return (
        frames["truth"].filter(pl.col("item_id").is_in(ids))
        .group_by(pl.col("scanned_at").dt.truncate("1d")).agg(pl.col(column).median())
        .sort("scanned_at")[column].to_list()
    )


def test_schemas_and_determinism(frames):
    assert frames["scans"].schema == pl.Schema(SCANS_SCHEMA)
    assert frames["item_scans"].schema == pl.Schema(ITEM_SCANS_SCHEMA)
    assert frames["ladder"].schema == pl.Schema(LADDER_SCHEMA)
    watched = [e for e in watchlist.load() if e.category in SMALL.categories]
    assert frames["item_scans"]["item_id"].n_unique() == len(watched)
    assert simulate(SMALL)["ladder"].equals(frames["ladder"])
    assert not simulate(SimConfig(**{**SMALL.__dict__, "seed": 12}))["ladder"].equals(frames["ladder"])


def test_ladder_quantities_match_item_scans(frames):
    ladder_qty = frames["ladder"].group_by("scan_id", "item_id").agg(pl.col("quantity").sum())
    joined = frames["item_scans"].join(ladder_qty, on=["scan_id", "item_id"], how="left", suffix="_ladder")
    assert (joined["quantity"] == joined["quantity_ladder"].fill_null(0)).all()


def test_leveling_items_peak_then_fall_and_raid_items_rise(frames):
    linen = daily(frames, "Linen Cloth")
    assert max(linen[:7]) > 2 * linen[-1]
    titans = daily(frames, "Flask of the Titans")  # scarce ingredients early, then raid demand
    assert titans[-1] > min(titans)


def test_population_and_bots():
    pop = Population(days_to_60=40)
    assert pop.median_level(0) == 1 and pop.median_level(40) == 60 and pop.median_level(80) == 60
    assert pop.at_least(60, 60) > 0.4 > pop.at_least(60, 10)
    assert pop.within(1, 20, 1) > pop.within(1, 20, 35)
    assert bot_factor(5, []) == 1 and bot_factor(40, []) == pytest.approx(1.8)
    assert bot_factor(30, [30]) < bot_factor(36, [30]) < bot_factor(40, [30])
    assert parse_bracket("35-45") == (35, 45) and parse_bracket("") == (1, 60)


def test_supply_dumps_show_up_as_quantity_spikes(frames):
    dumps = frames["events"].filter(pl.col("kind") == "supply_dump")
    assert dumps.height > 0
    ev = dumps.row(0, named=True)
    series = frames["truth"].filter(pl.col("item_id") == ev["item_id"]).sort("scanned_at")
    before = series.filter(pl.col("scanned_at") < ev["at"])["supply"].tail(1)
    after = series.filter(pl.col("scanned_at") >= ev["at"])["supply"].head(1)
    if before.len() and after.len():
        assert after[0] > before[0]


def test_views_and_signals_run_on_simulated_data(sim_dir):
    con = connect(sim_dir)
    assert con.execute("SELECT count(*) FROM market").fetchone()[0] > 0
    sig = signals(fetch_market(sim_dir), watchlist.load())
    assert set(sig["mode"].unique()) == {"launch", "mature"}
    tags = set(sig["signal"].drop_nulls().unique())
    assert tags >= {"BUY", "SELL"}  # mature: tradeable
    assert tags & {"DIP", "SPIKE", "FADE"}  # launch: descriptive only
    assert tags <= {"BUY", "SELL", "DIP", "SPIKE", "FADE"}


def test_cli_simulate_signals_backtest(tmp_path: Path, capsys):
    data = tmp_path / "sim"
    assert main(["simulate", str(data), "--days", "21", "--scans-per-day", "3", "--categories", "herb,cloth",
                 "--depth-scale", "0.3"]) == 0
    assert "simulated" in capsys.readouterr().out
    assert main(["signals", "--data-dir", str(data), "--all"]) == 0
    out = capsys.readouterr().out
    assert "Peacebloom" in out and "buy_units_100g" in out
    assert main(["backtest", "--data-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "Forward returns" in out and "return_on_capital" in out
