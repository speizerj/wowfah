"""Synthetic launch economy for developing and backtesting signals.

Writes the same Parquet layout as `wowfah ingest` (scans/, item_scans/, ladder/) plus
truth/ with the model's fair prices and events, so signals can be checked against what
really happened. The model is deliberately simple and shares its broad assumptions with
the launch priors in signals.py (demand follows the leveling population), but draws its
own pace, shocks and noise, so it tests the mechanics rather than proving the strategy.

Per item, per scan:
    population   median level rises to 60 over `days_to_60` (random per run)
    demand       leveling: players in the bracket; raid: players at 60; pvp: a mix
    supply       players who can farm it, plus bots after week 2, cut by ban waves,
                 plus random supply dumps that decay over ~1 day
    fair price   base * inflation * (demand / supply) ** 0.7, weekly raid-night demand
    shock        persistent per-item price shock (AR(1), `shock_half_life_days`), so dips
                 and spikes don't all revert by the next scan
    listings     Poisson count from supply, lognormal prices around fair * shock, undercut tail
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

from wowfah import watchlist
from wowfah.ingest import _write_atomic
from wowfah.schema import ITEM_SCANS_SCHEMA, LADDER_SCHEMA, SCANS_SCHEMA

CATEGORY_PRICE = {
    "herb": 5, "ore": 6, "bar": 12, "stone": 3, "gem": 25, "cloth": 3, "leather": 4, "enchanting": 15,
    "elemental": 20, "fish": 4, "potion": 20, "elixir": 25, "flask": 150, "food": 8, "consumable": 15,
    "engineering": 20,
}
SMALL_STACK = {"potion", "elixir", "flask", "consumable", "engineering"}
BOT_FARMED = {"herb", "ore", "cloth", "leather", "fish", "elemental"}
TIME_LEFT_P = np.array([1, 2, 4, 6]) / 13


@dataclass(frozen=True)
class SimConfig:
    days: int = 42
    scans_per_day: int = 4
    scan_hours: tuple[int, ...] = (9, 13, 18, 22)
    skip_probability: float = 0.1  # manual scans get missed
    shock_sigma: float = 0.15  # stationary std of the log price shock
    shock_half_life_days: float = 1.5  # 0 = independent every scan
    depth_scale: float = 1.0
    seed: int = 1
    launch: datetime = datetime(2026, 10, 1, tzinfo=timezone.utc)
    categories: tuple[str, ...] | None = None
    realm: str = "Simulated"
    faction: str = "Alliance"


@dataclass
class SimItem:
    entry: watchlist.Entry
    lo: int
    hi: int
    base_price: float  # copper per unit
    base_depth: float  # listings at supply 1.0
    max_stack: int
    dumps: list[tuple[float, float]] = field(default_factory=list)  # (day, extra supply)


def parse_bracket(bracket: str) -> tuple[int, int]:
    try:
        lo, hi = (int(x) for x in bracket.split("-"))
        return lo, hi
    except ValueError:
        return 1, 60


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class Population:
    def __init__(self, days_to_60: float):
        self.days_to_60 = days_to_60

    def median_level(self, day: float) -> float:
        return min(60.0, 1 + 59 * (max(day, 0) / self.days_to_60) ** 0.75)

    def spread(self, day: float) -> float:
        return 4 + 8 * min(day / self.days_to_60, 1)

    def at_least(self, level: float, day: float) -> float:
        return 1 - _phi((level - self.median_level(day)) / self.spread(day))

    def within(self, lo: float, hi: float, day: float) -> float:
        m, s = self.median_level(day), self.spread(day)
        return _phi((hi + 5 - m) / s) - _phi((lo - 5 - m) / s)


def demand_supply(item: SimItem, pop: Population, day: float, bot_factor: float) -> tuple[float, float]:
    tags = set(item.entry.tags)
    farmers = 0.1 + 0.9 * pop.at_least(item.lo, day)
    if "raid" in tags:
        demand = 0.1 + 0.9 * pop.at_least(58, day)
    elif "leveling" in tags:
        demand = 0.15 + pop.within(item.lo, item.hi, day)
    elif "pvp" in tags:
        demand = 0.15 + 0.5 * pop.at_least(item.lo, day) + 0.35 * pop.at_least(58, day)
    else:
        demand = 0.2 + 0.5 * pop.within(item.lo, item.hi, day) + 0.3 * pop.at_least(58, day)
    supply = farmers
    if item.entry.category in BOT_FARMED:
        supply *= bot_factor
    for dump_day, extra in item.dumps:
        if day >= dump_day:
            supply += extra * math.exp(-(day - dump_day) / 1.2)
    return demand, supply


def bot_factor(day: float, ban_days: list[float]) -> float:
    ramp = 1 + 0.8 * min(max((day - 14) / 14, 0), 1)
    for ban in ban_days:
        if ban <= day < ban + 7:
            return 1 + (ramp - 1) * 0.2 + (ramp - 1) * 0.8 * (day - ban) / 7
    return ramp


def build_items(entries: list[watchlist.Entry], cfg: SimConfig, rng: np.random.Generator) -> list[SimItem]:
    items = []
    for e in entries:
        lo, hi = parse_bracket(e.bracket)
        item_rng = np.random.default_rng(e.item_id)  # stable per item across runs
        base = CATEGORY_PRICE.get(e.category, 10) * math.exp(0.085 * hi) * item_rng.lognormal(0, 0.4)
        depth = min(max(600 * (base / 50) ** -0.4, 6), 800)
        item = SimItem(e, lo, hi, base, depth, 5 if e.category in SMALL_STACK else 20)
        n_dumps = rng.poisson(0.1 * cfg.days)
        item.dumps = [(float(rng.uniform(2, cfg.days)), float(rng.uniform(1.0, 2.5))) for _ in range(n_dumps)]
        items.append(item)
    return items


def scan_times(cfg: SimConfig, rng: np.random.Generator) -> list[datetime]:
    hours = cfg.scan_hours[: cfg.scans_per_day]
    times = []
    for d in range(cfg.days):
        for h in hours:
            if rng.random() < cfg.skip_probability:
                continue
            jitter = timedelta(minutes=float(rng.uniform(-40, 40)))
            times.append(cfg.launch + timedelta(days=d, hours=h) + jitter)
    return times


def raid_night(t: datetime) -> float:
    # Weekly reset Wednesday; raids Wed-Sun evenings, consumables bought before.
    return 1.0 if t.weekday() in (2, 3, 4, 5, 6) and 16 <= t.hour <= 22 else 0.0


def simulate(cfg: SimConfig, entries: list[watchlist.Entry] | None = None) -> dict[str, pl.DataFrame]:
    rng = np.random.default_rng(cfg.seed)
    entries = entries if entries is not None else watchlist.load()
    if cfg.categories:
        entries = [e for e in entries if e.category in cfg.categories]
    items = build_items(entries, cfg, rng)
    pop = Population(days_to_60=float(rng.uniform(30, 50)))
    ban_days = sorted(float(d) for d in rng.uniform(18, cfg.days, size=cfg.days // 14)) if cfg.days > 18 else []
    times = scan_times(cfg, rng)

    ladder_parts, item_scan_rows, scan_rows, truth_rows = [], [], [], []
    shock = np.zeros(len(items))
    prev_day = None
    for t in times:
        day = (t - cfg.launch).total_seconds() / 86400
        started = int(t.timestamp())
        scan_id = f"{cfg.realm}-{cfg.faction}-{started}"
        bots = bot_factor(day, ban_days)
        weekend = 1.15 if t.weekday() >= 5 else 1.0
        inflation = (1 + day / 20) ** 0.6
        common_noise = rng.lognormal(0, 0.04)
        gap = 0.25 if prev_day is None else day - prev_day
        prev_day = day
        rho = 0.5 ** (gap / cfg.shock_half_life_days) if cfg.shock_half_life_days > 0 else 0.0
        shock = rho * shock + math.sqrt(1 - rho**2) * cfg.shock_sigma * rng.standard_normal(len(items))

        counts, fairs = [], []
        for item in items:
            demand, supply = demand_supply(item, pop, day, bots)
            if "raid" in item.entry.tags:
                demand *= 1 + 0.3 * raid_night(t)
            ratio = min(max(demand / supply, 0.15), 10)
            fair = item.base_price * inflation * ratio ** 0.7 * common_noise
            fairs.append(fair * math.exp(shock[len(fairs)]))
            counts.append(rng.poisson(item.base_depth * supply * weekend * cfg.depth_scale))
            truth_rows.append((scan_id, t, item.entry.item_id, fair, demand, supply))

        n_total = int(sum(counts))
        idx = np.repeat(np.arange(len(items)), counts)
        fair_arr = np.asarray(fairs)[idx]
        undercut = np.where(rng.random(n_total) < 0.25, rng.uniform(0.85, 0.97, n_total), 1.0)
        unit = np.maximum(1, np.rint(fair_arr * rng.lognormal(0, 0.08, n_total) * undercut)).astype(np.int64)
        max_stack = np.asarray([it.max_stack for it in items])[idx]
        stack = np.where(max_stack == 5, rng.choice([1, 5], n_total, p=[0.4, 0.6]),
                         rng.choice([1, 5, 10, 20], n_total, p=[0.1, 0.2, 0.3, 0.4]))
        time_left = rng.choice([1, 2, 3, 4], n_total, p=TIME_LEFT_P)
        bid_only = rng.random(n_total) < 0.03

        listings = pl.DataFrame({
            "item_idx": idx, "unit_price": unit, "stack_size": stack, "time_left": time_left, "bid_only": bid_only,
        })
        buyout = listings.filter(~pl.col("bid_only"))
        ladder = (
            buyout.group_by("item_idx", "unit_price", "stack_size", "time_left")
            .agg(listings=pl.len(), quantity=pl.col("stack_size").sum())
            .with_columns(scan_id=pl.lit(scan_id), scanned_at=pl.lit(t))
        )
        ladder_parts.append(ladder)

        per_item = listings.group_by("item_idx").agg(
            listings_read=pl.len(),
            quantity=pl.col("stack_size").filter(~pl.col("bid_only")).sum(),
            bid_only_listings=pl.col("bid_only").sum(),
            bid_only_quantity=pl.col("stack_size").filter(pl.col("bid_only")).sum(),
        )
        by_idx = {r["item_idx"]: r for r in per_item.iter_rows(named=True)}
        for i, item in enumerate(items):
            r = by_idx.get(i, {})
            n = int(r.get("listings_read") or 0)
            item_scan_rows.append({
                "scan_id": scan_id, "realm": cfg.realm, "faction": cfg.faction,
                "item_id": item.entry.item_id, "name": item.entry.name, "status": "ok",
                "started_at": t, "finished_at": t, "pages": max(1, -(-n // 50)),
                "reported_listings": n, "listings_read": n, "quantity": int(r.get("quantity") or 0),
                "bid_only_listings": int(r.get("bid_only_listings") or 0),
                "bid_only_quantity": int(r.get("bid_only_quantity") or 0), "unreadable": 0,
            })
        scan_rows.append({
            "scan_id": scan_id, "source": "simulated", "api": "simulated", "addon_version": None,
            "schema_version": 2, "realm": cfg.realm, "faction": cfg.faction, "category": None,
            "status": "complete", "started_at": t, "finished_at": t,
            "items_requested": len(items), "items_scanned": len(items), "ingested_at": t,
        })

    item_ids = pl.DataFrame({"item_idx": np.arange(len(items)), "item_id": [it.entry.item_id for it in items]})
    ladder = (
        pl.concat(ladder_parts)
        .join(item_ids, on="item_idx")
        .with_columns(realm=pl.lit(cfg.realm), faction=pl.lit(cfg.faction))
        .select(pl.col(c).cast(t) for c, t in LADDER_SCHEMA.items())
        .sort("scanned_at", "item_id", "unit_price", "stack_size", "time_left")
    )
    truth = pl.DataFrame(
        truth_rows, schema={"scan_id": pl.String, "scanned_at": LADDER_SCHEMA["scanned_at"], "item_id": pl.Int64,
                            "fair_price": pl.Float64, "demand": pl.Float64, "supply": pl.Float64},
        orient="row",
    )
    events = pl.DataFrame(
        [("ban_wave", None, cfg.launch + timedelta(days=d)) for d in ban_days]
        + [("supply_dump", it.entry.item_id, cfg.launch + timedelta(days=d)) for it in items for d, _ in it.dumps],
        schema={"kind": pl.String, "item_id": pl.Int64, "at": LADDER_SCHEMA["scanned_at"]},
        orient="row",
    )
    return {
        "scans": pl.DataFrame(scan_rows, schema=SCANS_SCHEMA),
        "item_scans": pl.DataFrame(item_scan_rows, schema=ITEM_SCANS_SCHEMA),
        "ladder": ladder,
        "truth": truth,
        "events": events,
        "meta": pl.DataFrame({"days_to_60": [pop.days_to_60], "launch": [cfg.launch], "seed": [cfg.seed]}),
    }


def write(frames: dict[str, pl.DataFrame], data_dir: str | Path) -> None:
    """Write in the ingest layout, one file per table (the views glob *.parquet)."""
    data_dir = Path(data_dir)
    for name in ("scans", "item_scans", "ladder"):
        _write_atomic(frames[name], data_dir / name / "simulated.parquet")
    for name in ("truth", "events", "meta"):
        _write_atomic(frames[name], data_dir / "truth" / f"{name}.parquet")
