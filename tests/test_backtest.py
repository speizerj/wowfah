from __future__ import annotations

import polars as pl
import pytest

from tests.test_signals import CLOTH, FLAT_20D, HERB, flat, market
from wowfah.backtest import TradeConfig, forward_returns, simulate_trades, summarize_returns
from wowfah.signals import signals


def dip_and_recover(recovery: list[float]) -> pl.DataFrame:
    prices = FLAT_20D + [70] + recovery
    quantities = [1000] * 80 + [2000] + [1000] * len(recovery)
    return signals(market(prices, quantities), [CLOTH])


def ladder_for(sig: pl.DataFrame, depth: int = 1000) -> pl.LazyFrame:
    # Every scan: `depth` units spread evenly from 0.9x to 1.1x the signal price.
    return sig.select(
        "scan_id", "item_id",
        unit_price=pl.concat_list([(pl.col("price") * f).round(0).cast(pl.Int64) for f in (0.9, 1.0, 1.1)]),
        quantity=pl.lit(depth // 3),
    ).explode("unit_price").lazy()


def test_forward_returns_and_episodes():
    sig = dip_and_recover(flat(12))
    fwd = forward_returns(sig)
    buy = fwd.filter(pl.col("signal") == "BUY")
    assert buy.height >= 1 and buy["episode_start"].sum() == 1
    first = buy.row(0, named=True)
    assert first["fwd_1d"] == pytest.approx(100, abs=2)
    assert first["ret_1d"] == pytest.approx(first["fwd_1d"] * 0.95 / 70 - 1)

    summary = summarize_returns(fwd)
    row = summary.filter((pl.col("mode") == "mature") & (pl.col("signal") == "BUY")).row(0, named=True)
    assert row["n"] == 1 and row["hit_rate_3d"] == 1.0
    assert set(summary["signal"]) >= {"BUY", "(any scan, buy)"}


def test_sell_return_is_value_of_selling_now():
    prices = FLAT_20D + [130] + [100] * 8
    sig = signals(market(prices, [1000] * 80 + [600] + [1000] * 8), [CLOTH])
    sell = forward_returns(sig).filter(pl.col("signal") == "SELL").row(0, named=True)
    assert sell["ret_1d"] == pytest.approx(130 / 100 - 1)


def test_trade_takes_profit_after_recovery():
    sig = dip_and_recover(flat(12))
    trades, summary = simulate_trades(sig, ladder_for(sig), tcfg=TradeConfig(trade_gold=1))
    [t] = trades.to_dicts()
    assert t["exit_reason"] == "take profit" and t["status"] == "closed"
    assert t["units"] > 0 and t["pnl"] > 0 and t["hold_days"] < 1
    assert summary["trades"] == 1 and summary["win_rate"] == 1.0 and summary["pnl_gold"] > 0


def test_trade_exits_at_max_hold_when_price_stays_down():
    sig = dip_and_recover([70] * 40)
    trades, _ = simulate_trades(sig, ladder_for(sig), tcfg=TradeConfig(trade_gold=1, max_hold_days=2))
    t = trades.row(0, named=True)
    assert t["exit_reason"] == "max hold" and t["pnl"] < 0


def test_positions_still_open_are_marked():
    sig = dip_and_recover([70] * 2)
    trades, _ = simulate_trades(sig, ladder_for(sig), tcfg=TradeConfig(trade_gold=1))
    t = trades.row(0, named=True)
    assert t["status"] == "open (marked)" and t["exit_reason"] == "still open"


def test_no_trade_without_cash_or_signals():
    sig = dip_and_recover(flat(12))
    trades, summary = simulate_trades(sig, ladder_for(sig), tcfg=TradeConfig(capital_gold=0.001, trade_gold=1))
    assert trades.height == 0 and summary["pnl_gold"] == 0 and summary["win_rate"] is None

    quiet = signals(market(FLAT_20D), [CLOTH])
    trades, _ = simulate_trades(quiet, ladder_for(quiet))
    assert trades.height == 0


def test_trade_sim_never_acts_on_launch_mode_tags():
    # DIP/SPIKE/FADE are descriptive (see signals.py); the trade simulator only trades
    # mature BUY/SELL, so a market that never leaves launch mode should produce zero trades.
    prices = flat(12) + [80]  # a DIP in launch mode, well within the 14-day mature threshold
    sig = signals(market(prices, entry=HERB), [HERB])
    assert set(sig["mode"].unique()) == {"launch"}
    assert "DIP" in set(sig["signal"].drop_nulls())
    trades, summary = simulate_trades(sig, ladder_for(sig))
    assert trades.height == 0 and summary["trades"] == 0
