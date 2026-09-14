"""Backtests for signals.py.

forward_returns  what the price did 1/3/7 days after each signal episode (first scan of a run
                 of the same signal), against the same measure over every scan. Runs on every
                 signal value, including launch mode's DIP/SPIKE/FADE, so their forward returns
                 can be checked even though nothing trades on them.
simulate_trades  buys against the real ladder with a fixed gold budget per trade, then sells
                 at a small undercut of the cheapest listing, paying the AH cut, limited to a
                 share of the listed quantity per scan. Only trades mature BUY/SELL -- launch
                 mode's DIP/SPIKE/FADE are descriptive (see signals.py) and are never ordered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import polars as pl

from wowfah.signals import SignalConfig, fill

COPPER_PER_GOLD = 10_000
HORIZONS = {"1d": timedelta(days=1), "3d": timedelta(days=3), "7d": timedelta(days=7)}


def _with_episodes(sig: pl.DataFrame) -> pl.DataFrame:
    prev = pl.col("signal").shift(1).over("item_id")
    return sig.sort("item_id", "scanned_at").with_columns(
        episode_start=pl.col("signal").is_not_null() & (prev.is_null() | (prev != pl.col("signal")))
    )


def forward_returns(sig: pl.DataFrame, cfg: SignalConfig = SignalConfig()) -> pl.DataFrame:
    """Adds fwd_<h> (future price) and ret_<h> columns to every row.

    BUY and unsignalled rows: return from buying now and selling at that future price after the cut.
    SELL rows: how much better selling now was than selling at the future price.
    """
    df = _with_episodes(sig)
    future = df.select("item_id", pl.col("scanned_at").alias("_target"), pl.col("price").alias("_fwd")).sort("_target")
    for name, delta in HORIZONS.items():
        df = (
            df.with_columns(_target=pl.col("scanned_at") + delta)
            .sort("_target")
            .join_asof(future, on="_target", by="item_id", strategy="forward", tolerance="1d",
                       check_sortedness=False)
            .rename({"_fwd": f"fwd_{name}"})
            .drop("_target")
        )
        fwd = pl.col(f"fwd_{name}")
        df = df.with_columns(
            pl.when(pl.col("signal") == "SELL").then(pl.col("price") / fwd - 1)
            .otherwise(fwd * (1 - cfg.ah_cut) / pl.col("price") - 1)
            .alias(f"ret_{name}")
        )
    return df.sort("item_id", "scanned_at")


def summarize_returns(fwd: pl.DataFrame) -> pl.DataFrame:
    rets = [f"ret_{h}" for h in HORIZONS]
    aggs = [pl.len().alias("n")] + [pl.col(r).mean().alias(f"mean_{r}") for r in rets] + [
        (pl.col("ret_3d") > 0).mean().alias("hit_rate_3d")]
    episodes = fwd.filter(pl.col("episode_start")).group_by("mode", "signal").agg(aggs)
    everything = fwd.group_by("mode").agg(aggs).with_columns(signal=pl.lit("(any scan, buy)"))
    return pl.concat([episodes, everything.select(episodes.columns)]).sort("mode", "signal")


@dataclass(frozen=True)
class TradeConfig:
    capital_gold: float = 5000
    trade_gold: float = 250
    max_hold_days: float = 7
    take_profit: float = 0.05  # sell once proceeds after the cut beat cost by this much
    max_impact: float = 0.05  # don't buy listings more than this far above the signal price
    max_buy_share: float = 0.10  # of the listed quantity
    sell_undercut: float = 0.01  # list this far under the cheapest listing
    max_sell_share: float = 0.2  # of the listed quantity, per scan


def simulate_trades(
    sig: pl.DataFrame,
    ladder: pl.LazyFrame,
    cfg: SignalConfig = SignalConfig(),
    tcfg: TradeConfig = TradeConfig(),
) -> tuple[pl.DataFrame, dict]:
    budget = int(tcfg.trade_gold * COPPER_PER_GOLD)
    buys = sig.filter(pl.col("signal") == "BUY")
    fills = fill(ladder, buys, budget, tcfg.max_impact, tcfg.max_buy_share) if buys.height else pl.DataFrame(
        schema={"scan_id": pl.String, "item_id": pl.Int64, "units": pl.Int64, "cost": pl.Int64,
                "max_unit_price": pl.Int64, "avg_unit_price": pl.Float64})
    fills_by_key = {(r["scan_id"], r["item_id"]): r for r in fills.iter_rows(named=True)}

    cash = tcfg.capital_gold * COPPER_PER_GOLD
    positions: dict[int, dict] = {}
    trades: list[dict] = []
    max_exposure = 0.0
    last_row: dict[int, dict] = {}

    for row in sig.sort("scanned_at").iter_rows(named=True):
        item_id = row["item_id"]
        last_row[item_id] = row
        pos = positions.get(item_id)
        if pos:
            held = (row["scanned_at"] - pos["opened_at"]).total_seconds() / 86400
            net_unit = min(row["min_unit_price"], row["price"]) * (1 - tcfg.sell_undercut) * (1 - cfg.ah_cut)
            if not pos["exit_reason"]:
                if net_unit >= pos["avg_unit_price"] * (1 + tcfg.take_profit):
                    pos["exit_reason"] = "take profit"
                elif row["signal"] == "SELL":
                    pos["exit_reason"] = "sell signal"
                elif held >= tcfg.max_hold_days:
                    pos["exit_reason"] = "max hold"
            if pos["exit_reason"]:
                units = min(pos["units"] - pos["sold"], max(1, int(row["quantity"] * tcfg.max_sell_share)))
                proceeds = units * net_unit
                pos["sold"] += units
                pos["proceeds"] += proceeds
                cash += proceeds
                if pos["sold"] >= pos["units"]:
                    trades.append(_close(pos, row["scanned_at"], "closed"))
                    del positions[item_id]
        elif row["signal"] == "BUY":
            f = fills_by_key.get((row["scan_id"], item_id))
            if f and f["units"] > 0 and cash >= f["cost"]:
                cash -= f["cost"]
                positions[item_id] = {
                    "item_id": item_id, "name": row["name"], "mode": row["mode"], "opened_at": row["scanned_at"],
                    "units": f["units"], "cost": f["cost"], "avg_unit_price": f["avg_unit_price"],
                    "price_impact": f["avg_unit_price"] / row["price"] - 1, "sold": 0, "proceeds": 0.0,
                    "exit_reason": None,
                }
        max_exposure = max(max_exposure, sum(p["cost"] for p in positions.values()))

    for item_id, pos in positions.items():  # mark what's still held at the last price
        row = last_row[item_id]
        remaining = pos["units"] - pos["sold"]
        pos["proceeds"] += remaining * min(row["min_unit_price"], row["price"]) * (1 - tcfg.sell_undercut) * (
            1 - cfg.ah_cut)
        trades.append(_close(pos, row["scanned_at"], "open (marked)"))

    trade_df = pl.DataFrame(trades, schema={
        "item_id": pl.Int64, "name": pl.String, "mode": pl.String, "opened_at": sig.schema["scanned_at"],
        "closed_at": sig.schema["scanned_at"], "status": pl.String, "exit_reason": pl.String, "units": pl.Int64,
        "cost": pl.Float64, "proceeds": pl.Float64, "pnl": pl.Float64, "return": pl.Float64,
        "hold_days": pl.Float64, "price_impact": pl.Float64,
    })
    pnl = trade_df["pnl"].sum() if trade_df.height else 0.0
    summary = {
        "trades": trade_df.height,
        "pnl_gold": pnl / COPPER_PER_GOLD,
        "return_on_capital": pnl / (tcfg.capital_gold * COPPER_PER_GOLD),
        "win_rate": float((trade_df["pnl"] > 0).mean()) if trade_df.height else None,
        "avg_return": float(trade_df["return"].mean()) if trade_df.height else None,
        "avg_hold_days": float(trade_df["hold_days"].mean()) if trade_df.height else None,
        "max_exposure_gold": max_exposure / COPPER_PER_GOLD,
    }
    return trade_df, summary


def _close(pos: dict, closed_at, status: str) -> dict:
    return {
        "item_id": pos["item_id"], "name": pos["name"], "mode": pos["mode"], "opened_at": pos["opened_at"],
        "closed_at": closed_at, "status": status, "exit_reason": pos["exit_reason"] or "still open", "units": pos["units"],
        "cost": float(pos["cost"]), "proceeds": pos["proceeds"], "pnl": pos["proceeds"] - pos["cost"],
        "return": pos["proceeds"] / pos["cost"] - 1,
        "hold_days": (closed_at - pos["opened_at"]).total_seconds() / 86400,
        "price_impact": pos["price_impact"],
    }
