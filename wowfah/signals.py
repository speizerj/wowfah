"""Buy/sell signals for commodities.

Price is the quantity-weighted 10th percentile unit price (`market.p10_unit_price`): what the
cheap end of the market costs once a few odd listings are ignored. All windows are
time-based and look only backwards, so the same code runs live and in backtests.

Two modes per item:

launch   first `mature_after_days` of an item's history. There's no baseline yet, and the
         "prior" (see launch_prior) is a guess from watchlist tags/bracket, not something
         learned from this item. So launch mode is descriptive, not a trade instruction: it
         tags DIP/SPIKE/FADE for the user's own judgment, and the trade simulator in
         backtest.py never acts on them (it only trades mature BUY/SELL).
             DIP    price well below its 3-day median, in a market the prior says is rising
             SPIKE  price well above its 3-day median
             FADE   the prior says demand has passed its peak, and price is trending down
mature   compare price and supply to a trailing 14-day median. These ARE trade instructions:
         BUY   price well below normal, supply above normal (a temporary glut), trend not
               collapsing, and reverting to normal would clear the AH cut with margin
         SELL  price well above normal and supply thin
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

from wowfah import watchlist
from wowfah.query import connect

DAY_MS = 86_400_000


@dataclass(frozen=True)
class SignalConfig:
    ah_cut: float = 0.05
    mature_after_days: float = 14.0
    baseline_window: str = "14d"
    min_baseline_obs: int = 12
    # mature mode
    buy_deviation: float = -0.20  # price vs baseline
    buy_supply_ratio: float = 1.25  # quantity vs baseline
    falling_knife_trend: float = -0.03  # 7d log-price slope per day; below this, don't buy
    min_margin: float = 0.10  # baseline * (1 - cut) vs price
    sell_deviation: float = 0.20
    sell_supply_ratio: float = 0.85
    # launch mode
    launch_days_to_60: float = 40.0  # assumed pace of the median player
    launch_dip: float = -0.15  # price vs 3d median that counts as a dip worth buying in a rising market
    launch_spike: float = 0.30  # price vs 3d median worth selling into


def fetch_market(data_dir: str | Path, con=None) -> pl.DataFrame:
    """The `market` view as a polars frame (timestamps come back as epoch ms to avoid pytz)."""
    con = con or connect(data_dir)
    cur = con.execute("""
        SELECT scan_id, item_id, name, epoch_ms(scanned_at) AS ts_ms, quantity,
               min_unit_price, p10_unit_price, median_unit_price
        FROM market
        WHERE p10_unit_price IS NOT NULL
    """)
    columns = [d[0] for d in cur.description]
    df = pl.DataFrame(cur.fetchall(), schema=columns, orient="row")
    return df.with_columns(
        scanned_at=pl.from_epoch("ts_ms", "ms").dt.replace_time_zone("UTC"),
        price=pl.col("p10_unit_price").cast(pl.Float64),
        quantity=pl.col("quantity").cast(pl.Float64),
    ).drop("ts_ms").sort("item_id", "scanned_at")


def _rolling_slope(y: str, window: str) -> pl.Expr:
    """Least-squares slope of y against days over a trailing window, excluding the current scan.

    Excluding it means a sudden dip or spike doesn't change the trend it is judged against.
    """
    x = pl.col("_days")
    by = dict(by="scanned_at", window_size=window, closed="left")
    mx, my = x.rolling_mean_by(**by), pl.col(y).rolling_mean_by(**by)
    mxy = (x * pl.col(y)).rolling_mean_by(**by)
    mxx = (x * x).rolling_mean_by(**by)
    var = mxx - mx * mx
    return pl.when(var > 1e-6).then((mxy - mx * my) / var).otherwise(None)


def features(market: pl.DataFrame, cfg: SignalConfig = SignalConfig()) -> pl.DataFrame:
    base = dict(by="scanned_at", window_size=cfg.baseline_window, closed="left")
    return (
        market.sort("item_id", "scanned_at")
        .with_columns(
            _days=((pl.col("scanned_at") - pl.col("scanned_at").min()).dt.total_milliseconds() / DAY_MS)
            .over("item_id"),
            _log_price=pl.col("price").log(),
            _one=pl.lit(1),
        )
        .with_columns(
            history_days=pl.col("_days"),
            baseline_price=pl.col("price").rolling_median_by(**base).over("item_id"),
            baseline_quantity=pl.col("quantity").rolling_median_by(**base).over("item_id"),
            baseline_obs=pl.col("_one").rolling_sum_by(**base).over("item_id"),
            median_3d=pl.col("price").rolling_median_by("scanned_at", window_size="3d", closed="left").over("item_id"),
            trend_3d=_rolling_slope("_log_price", "3d").over("item_id"),
            trend_7d=_rolling_slope("_log_price", "7d").over("item_id"),
        )
        .with_columns(
            deviation=pl.col("price") / pl.col("baseline_price") - 1,
            supply_ratio=pl.col("quantity") / pl.col("baseline_quantity"),
            short_deviation=pl.col("price") / pl.col("median_3d") - 1,
            margin=pl.col("baseline_price") * (1 - cfg.ah_cut) / pl.col("price") - 1,
        )
        .drop("_days", "_log_price", "_one")
    )


def launch_prior(entries: list[watchlist.Entry], cfg: SignalConfig) -> pl.DataFrame:
    """Per item, the days after launch when demand should be rising and when it should fade.

    Median player level is assumed to reach L at days_to_60 * ((L - 1) / 59) ** (1 / 0.75).
    leveling  rising until the median player reaches the top of the bracket, falling after
    raid/pvp  rising until two weeks after the median player hits 60
    other     no prior
    """
    from wowfah.simulate import parse_bracket  # shared bracket parsing, no simulation

    def reach(level: float) -> float:
        return cfg.launch_days_to_60 * (max(level - 1, 0) / 59) ** (1 / 0.75)

    rows = []
    for e in entries:
        lo, hi = parse_bracket(e.bracket)
        tags = set(e.tags)
        if "leveling" in tags:
            rows.append((e.item_id, 0.0, reach(hi)))
        elif tags & {"raid", "pvp"}:
            rows.append((e.item_id, 0.0, cfg.launch_days_to_60 + 14))
        else:
            rows.append((e.item_id, None, None))
    return pl.DataFrame(rows, schema={"item_id": pl.Int64, "rising_from": pl.Float64, "rising_until": pl.Float64},
                        orient="row")


def signals(
    market: pl.DataFrame,
    entries: list[watchlist.Entry],
    cfg: SignalConfig = SignalConfig(),
    launch: datetime | None = None,
) -> pl.DataFrame:
    """Features plus `mode`, `signal` and `reason` for every scan of every item.

    signal is BUY/SELL/null for mature rows (tradeable), or DIP/SPIKE/FADE/null for launch
    rows (descriptive only -- see the module docstring).
    """
    launch = launch or market["scanned_at"].min()
    df = features(market, cfg).join(launch_prior(entries, cfg), on="item_id", how="left").with_columns(
        launch_day=(pl.col("scanned_at") - pl.lit(launch).dt.cast_time_unit("ms")).dt.total_milliseconds() / DAY_MS,
    )
    mature = (pl.col("history_days") >= cfg.mature_after_days) & (pl.col("baseline_obs") >= cfg.min_baseline_obs)
    rising = pl.col("launch_day").is_between(pl.col("rising_from"), pl.col("rising_until"))
    falling = pl.col("rising_until").is_not_null() & (pl.col("launch_day") > pl.col("rising_until"))

    mature_buy = (
        mature
        & (pl.col("deviation") <= cfg.buy_deviation)
        & (pl.col("supply_ratio") >= cfg.buy_supply_ratio)
        & (pl.col("trend_7d") >= cfg.falling_knife_trend)
        & (pl.col("margin") >= cfg.min_margin)
    )
    mature_sell = mature & (pl.col("deviation") >= cfg.sell_deviation) & (pl.col("supply_ratio") <= cfg.sell_supply_ratio)
    launch_buy = ~mature & rising & (pl.col("short_deviation") <= cfg.launch_dip) & (pl.col("trend_3d") >= 0)
    launch_sell_spike = ~mature & (pl.col("short_deviation") >= cfg.launch_spike)
    launch_sell_fade = ~mature & falling & (pl.col("trend_3d") < 0)

    pct = lambda c: (pl.col(c) * 100).round(0).cast(pl.Int64).cast(pl.String)  # noqa: E731
    return df.with_columns(
        mode=pl.when(mature).then(pl.lit("mature")).otherwise(pl.lit("launch")),
        signal=pl.when(mature_buy).then(pl.lit("BUY"))
        .when(mature_sell).then(pl.lit("SELL"))
        .when(launch_buy).then(pl.lit("DIP"))
        .when(launch_sell_spike).then(pl.lit("SPIKE"))
        .when(launch_sell_fade).then(pl.lit("FADE")),
        reason=pl.when(mature_buy).then(pl.concat_str(
            pl.lit("price "), pct("deviation"), pl.lit("% vs 14d, supply x"), pl.col("supply_ratio").round(1),
            pl.lit(", margin "), pct("margin"), pl.lit("%")))
        .when(launch_buy).then(pl.concat_str(pl.lit("dip "), pct("short_deviation"), pl.lit("% vs 3d in a rising market")))
        .when(mature_sell).then(pl.concat_str(
            pl.lit("price +"), pct("deviation"), pl.lit("% vs 14d, supply x"), pl.col("supply_ratio").round(1)))
        .when(launch_sell_spike).then(pl.concat_str(pl.lit("spike +"), pct("short_deviation"), pl.lit("% vs 3d")))
        .when(launch_sell_fade).then(pl.concat_str(pl.lit("demand fading, 3d trend "), pct("trend_3d"),
                                                   pl.lit("%/day"))),
    )


def fill(
    ladder: pl.LazyFrame,
    orders: pl.DataFrame,
    budget_copper: int,
    max_impact: float = 0.05,
    max_share: float = 0.10,
) -> pl.DataFrame:
    """Walk the ladder for each order (scan_id, item_id, price, quantity), cheapest first.

    Spends up to budget_copper, never pays more than price * (1 + max_impact) per unit, and
    never takes more than max_share of the listed quantity. Treats listings as divisible.
    Returns units, cost, avg_unit_price and max_unit_price per order that could buy anything.
    """
    limits = orders.select(
        "scan_id", "item_id",
        _max_price=pl.col("price") * (1 + max_impact),
        _max_units=(pl.col("quantity") * max_share).floor().clip(lower_bound=1),
    ).unique(["scan_id", "item_id"])
    return (
        ladder.join(limits.lazy(), on=["scan_id", "item_id"])
        .filter(pl.col("unit_price") <= pl.col("_max_price"))
        .sort("scan_id", "item_id", "unit_price")
        .with_columns(cost=pl.col("unit_price") * pl.col("quantity"))
        .with_columns(
            spent_before=(pl.col("cost").cum_sum() - pl.col("cost")).over("scan_id", "item_id"),
            units_before=(pl.col("quantity").cum_sum() - pl.col("quantity")).over("scan_id", "item_id"),
        )
        .with_columns(
            take=pl.min_horizontal(
                pl.col("quantity"),
                ((budget_copper - pl.col("spent_before")).clip(lower_bound=0) / pl.col("unit_price")).floor(),
                (pl.col("_max_units") - pl.col("units_before")).clip(lower_bound=0),
            ).cast(pl.Int64)
        )
        .filter(pl.col("take") > 0)
        .group_by("scan_id", "item_id")
        .agg(
            units=pl.col("take").sum(),
            cost=(pl.col("take") * pl.col("unit_price")).sum(),
            max_unit_price=pl.col("unit_price").max(),
        )
        .with_columns(avg_unit_price=pl.col("cost") / pl.col("units"))
        .collect()
    )


def ladder_frame(data_dir: str | Path) -> pl.LazyFrame:
    return pl.scan_parquet(Path(data_dir) / "ladder" / "*.parquet").select(
        "scan_id", "item_id", "unit_price", "quantity")


def format_copper(copper: float | None) -> str:
    if copper is None:
        return ""
    c = int(round(copper))
    g, rest = divmod(c, 10_000)
    sv, cc = divmod(rest, 100)
    parts = [f"{g}g"] * (g > 0) + [f"{sv}s"] * (g > 0 or sv > 0) + [f"{cc}c"]
    return " ".join(parts)


def latest(sig: pl.DataFrame, ladder: pl.LazyFrame, entries: list[watchlist.Entry], trade_gold: float,
           include_quiet: bool = False) -> pl.DataFrame:
    """The most recent scan of each item, with what a mature BUY of trade_gold would fill at.

    Launch-mode DIP/SPIKE/FADE rows never get a fill: they're informational, not orders.
    """
    last = sig.sort("scanned_at").group_by("item_id", maintain_order=True).last()
    if not include_quiet:
        last = last.filter(pl.col("signal").is_not_null())
    buys = last.filter(pl.col("signal") == "BUY")
    fills = fill(ladder, buys, int(trade_gold * 10_000)) if buys.height else None
    if fills is not None:
        last = last.join(fills.select("scan_id", "item_id", "units", "avg_unit_price"), on=["scan_id", "item_id"],
                         how="left")
    else:
        last = last.with_columns(units=pl.lit(None, pl.Int64), avg_unit_price=pl.lit(None, pl.Float64))
    categories = watchlist.frame(entries).select("item_id", "category")
    return (
        last.join(categories, on="item_id", how="left")
        .sort(pl.col("signal").fill_null("~"), "mode", "category", "name")
        .select("scanned_at", "item_id", "name", "category", "mode", "signal", "reason", "price", "baseline_price",
                "short_deviation", "trend_3d", "quantity", "units", "avg_unit_price")
    )

