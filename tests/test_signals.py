from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from wowfah.signals import SignalConfig, features, fill, format_copper, latest, signals
from wowfah.watchlist import Entry

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
HERB = Entry(2447, "Peacebloom", "herb", "1-20", ("leveling",))
LOTUS = Entry(13468, "Black Lotus", "herb", "55-60", ("raid",))
CLOTH = Entry(2589, "Linen Cloth", "cloth", "1-20", ())


def market(prices: list[float], quantities: list[float] | float = 1000, entry: Entry = CLOTH,
           step_hours: float = 6, start: datetime = START) -> pl.DataFrame:
    if not isinstance(quantities, list):
        quantities = [quantities] * len(prices)
    times = [start + timedelta(hours=step_hours * i) for i in range(len(prices))]
    return pl.DataFrame({
        "scan_id": [f"s{int(t.timestamp())}" for t in times],
        "item_id": entry.item_id,
        "name": entry.name,
        "scanned_at": times,
        "quantity": [float(q) for q in quantities],
        "min_unit_price": [int(p * 0.9) for p in prices],
        "p10_unit_price": [int(p) for p in prices],
        "median_unit_price": [int(p * 1.2) for p in prices],
        "price": [float(p) for p in prices],
    }).with_columns(pl.col("scanned_at").dt.cast_time_unit("us"))


def flat(n: int, level: float = 100) -> list[float]:
    # Small alternating wiggle so medians and slopes are well defined.
    return [level * (1.01 if i % 2 else 0.99) for i in range(n)]


def last(df: pl.DataFrame) -> dict:
    return df.sort("scanned_at").row(-1, named=True)


FLAT_20D = flat(80)  # 20 days at 4 scans/day: mature


def test_trend_is_log_price_slope_per_day():
    prices = [100 * math.exp(0.05 * i / 4) for i in range(40)]
    row = last(features(market(prices)))
    assert row["trend_7d"] == pytest.approx(0.05, rel=1e-6)
    assert row["trend_3d"] == pytest.approx(0.05, rel=1e-6)
    # The current scan is excluded, so a final crash doesn't bend the trend.
    crashed = last(features(market(prices[:-1] + [1.0])))
    assert crashed["trend_3d"] == pytest.approx(0.05, rel=1e-6)


def test_baseline_excludes_the_current_scan():
    row = last(features(market(FLAT_20D + [50])))
    assert row["baseline_price"] == pytest.approx(100, rel=0.02)
    assert row["deviation"] == pytest.approx(-0.5, abs=0.02)
    assert row["history_days"] == pytest.approx(20)


def test_mature_buy_on_dip_with_supply_glut():
    row = last(signals(market(FLAT_20D + [70], [1000] * 80 + [2000]), [CLOTH]))
    assert row["mode"] == "mature" and row["signal"] == "BUY"
    assert row["reason"] == "price -30% vs 14d, supply x2.0, margin 36%"


def test_no_buy_without_supply_glut():
    row = last(signals(market(FLAT_20D + [70], 1000), [CLOTH]))
    assert row["signal"] is None


def test_no_buy_into_a_falling_knife():
    falling = [100 * math.exp(-0.06 * i / 4) for i in range(80)]
    row = last(signals(market(falling + [falling[-1] * 0.7], [1000] * 80 + [2000]), [CLOTH]))
    assert row["deviation"] < -0.2 and row["trend_7d"] < -0.03
    assert row["signal"] is None


def test_margin_gate():
    df = market(FLAT_20D + [78], [1000] * 80 + [2000])
    assert last(signals(df, [CLOTH]))["signal"] == "BUY"
    assert last(signals(df, [CLOTH], SignalConfig(min_margin=0.3)))["signal"] is None


def test_mature_sell_on_spike_with_thin_supply():
    row = last(signals(market(FLAT_20D + [130], [1000] * 80 + [600]), [CLOTH]))
    assert row["signal"] == "SELL" and row["reason"] == "price +30% vs 14d, supply x0.6"


def test_launch_flags_dips_only_in_rising_markets():
    # Launch-mode signals are descriptive tags (DIP/SPIKE/FADE), not trade instructions
    # like mature mode's BUY/SELL -- see the signals.py module docstring.
    prices = flat(12) + [80]  # day 3, dip of 20%
    assert last(signals(market(prices, entry=HERB), [HERB]))["signal"] == "DIP"
    row = last(signals(market(prices, entry=LOTUS), [LOTUS]))
    assert row["mode"] == "launch" and row["signal"] == "DIP"
    assert row["reason"] == "dip -20% vs 3d in a rising market"
    # No prior for untagged items.
    assert last(signals(market(prices), [CLOTH]))["signal"] is None
    # A leveling item whose bracket the population has already passed isn't rising.
    assert last(signals(market(prices, entry=HERB), [HERB], SignalConfig(launch_days_to_60=5)))["signal"] is None


def test_launch_flags_spikes_and_fading_demand():
    row = last(signals(market(flat(12) + [140]), [CLOTH]))
    assert row["signal"] == "SPIKE" and row["reason"] == "spike +40% vs 3d"

    fading = [100 * math.exp(-0.05 * i / 4) for i in range(20)]
    row = last(signals(market(fading, entry=HERB), [HERB], SignalConfig(launch_days_to_60=5)))
    assert row["signal"] == "FADE" and row["reason"].startswith("demand fading, 3d trend -5%/day")


def test_launch_day_uses_given_launch_date():
    # Same prices, but the item was first scanned 30 days after launch: the leveling window is over.
    prices = flat(12) + [80]
    df = market(prices, entry=HERB, start=START + timedelta(days=30))
    assert last(signals(df, [HERB], launch=START))["signal"] != "DIP"
    assert last(signals(df, [HERB]))["signal"] == "DIP"


def ladder(rows: list[tuple[int, int]], scan_id: str = "s1", item_id: int = 2589) -> pl.LazyFrame:
    return pl.DataFrame({
        "scan_id": scan_id, "item_id": item_id,
        "unit_price": [r[0] for r in rows], "quantity": [r[1] for r in rows],
    }).lazy()


def test_fill_respects_budget_impact_and_share():
    lad = ladder([(100, 50), (104, 50), (120, 500)])
    order = pl.DataFrame({"scan_id": ["s1"], "item_id": [2589], "price": [100.0], "quantity": [600.0]})

    r = fill(lad, order, budget_copper=7_000, max_impact=0.05, max_share=1).row(0, named=True)
    assert (r["units"], r["cost"]) == (69, 50 * 100 + 19 * 104)

    r = fill(lad, order, budget_copper=10**9, max_impact=0.05, max_share=1).row(0, named=True)
    assert r["units"] == 100 and r["max_unit_price"] == 104  # 120c listings are above the impact limit

    r = fill(lad, order, budget_copper=10**9, max_impact=1, max_share=0.1).row(0, named=True)
    assert r["units"] == 60 and r["avg_unit_price"] == pytest.approx((50 * 100 + 10 * 104) / 60)

    none = pl.DataFrame({"scan_id": ["s1"], "item_id": [2589], "price": [50.0], "quantity": [600.0]})
    assert fill(lad, none, 10**9).height == 0


def test_format_copper():
    assert [format_copper(v) for v in (7, 105, 10_000, 1_234_567, None)] == ["7c", "1s 5c", "1g 0s 0c",
                                                                            "123g 45s 67c", ""]


def test_latest_reports_last_scan_with_fill():
    df = market(FLAT_20D + [70], [1000] * 80 + [2000])
    sig = signals(df, [CLOTH])
    last_scan = sig["scan_id"][-1]
    table = latest(sig, ladder([(65, 100), (70, 300)], scan_id=last_scan), [CLOTH], trade_gold=1)
    [row] = table.to_dicts()
    assert row["signal"] == "BUY" and row["category"] == "cloth"
    # 1g budget: 100 units at 65c, then 3500c buys 50 more at 70c.
    assert row["units"] == 150 and row["avg_unit_price"] == pytest.approx(10_000 / 150)
    assert latest(sig.head(10), ladder([]), [CLOTH], 1).height == 0
    assert latest(sig.head(10), ladder([]), [CLOTH], 1, include_quiet=True).height == 1


def test_launch_tags_are_never_filled_as_orders():
    # DIP is launch mode's buy-side tag; latest() should never treat it as an order to fill.
    prices = flat(12) + [80]
    sig = signals(market(prices, entry=HERB), [HERB])
    last_scan = sig["scan_id"][-1]
    row = latest(sig, ladder([(60, 500)], scan_id=last_scan, item_id=HERB.item_id), [HERB], trade_gold=100).row(
        0, named=True)
    assert row["signal"] == "DIP"
    assert row["units"] is None and row["avg_unit_price"] is None
