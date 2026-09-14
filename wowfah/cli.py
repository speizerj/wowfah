from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path("data")


def cmd_ingest(args: argparse.Namespace) -> int:
    from wowfah.ingest import ingest_savedvariables

    results = ingest_savedvariables(args.savedvariables, args.data_dir, force=args.force)
    for r in results:
        print(f"{r.status:8} {r.items:4} items {r.ladder_rows:7} ladder rows  {r.scan_id}")
    written = sum(r.status == "written" for r in results)
    print(f"{written} scan(s) written, {len(results) - written} skipped")
    return 0


def cmd_probes(args: argparse.Namespace) -> int:
    from wowfah import savedvars
    from wowfah.ingest import DB_VARIABLE

    db = savedvars.load(args.savedvariables).get(DB_VARIABLE) or {}
    probes = db.get("probes") or []
    if isinstance(probes, dict):
        probes = []
    if not probes:
        print("no probe logs stored")
        return 0
    for p in probes:
        print(f"== probe {p.get('target')} ({p.get('api')} API, {p.get('status')}): item {p.get('itemStatus')}, "
              f"{p.get('pages')} page(s), {p.get('listingsRead')} read, reported {p.get('reportedListings')}")
        log = p.get("log") or []
        for line in [] if isinstance(log, dict) else log:
            print(line)
    return 0


def cmd_dummy(args: argparse.Namespace) -> int:
    from wowfah import savedvars
    from wowfah.dummy import generate_db

    db = generate_db(seed=args.seed, scans=args.scans, depth_scale=args.depth_scale)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    savedvars.dump({"WoWFAH_DB": db}, args.output)
    print(f"wrote {args.scans} fake scan(s) to {args.output}")
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    from wowfah.query import connect

    con = connect(args.data_dir)
    con.sql(args.query).show(max_rows=args.max_rows)
    return 0


def _launch(value: str | None):
    from datetime import datetime, timezone

    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None


def _signal_config(args: argparse.Namespace):
    from wowfah.signals import SignalConfig

    return SignalConfig(launch_days_to_60=args.days_to_60)


def cmd_simulate(args: argparse.Namespace) -> int:
    from wowfah.simulate import SimConfig, simulate, write

    cfg = SimConfig(days=args.days, scans_per_day=args.scans_per_day, seed=args.seed, depth_scale=args.depth_scale,
                    categories=tuple(args.categories.split(",")) if args.categories else None)
    frames = simulate(cfg)
    write(frames, args.data_dir)
    meta = frames["meta"].row(0, named=True)
    print(f"simulated {frames['scans'].height} scans of {frames['item_scans']['item_id'].n_unique()} items "
          f"({frames['ladder'].height} ladder rows) -> {args.data_dir}; median player hits 60 on day "
          f"{meta['days_to_60']:.0f}")
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    import polars as pl

    from wowfah import watchlist
    from wowfah.signals import fetch_market, format_copper, ladder_frame, latest, signals

    entries = watchlist.load()
    sig = signals(fetch_market(args.data_dir), entries, _signal_config(args), _launch(args.launch_date))
    table = latest(sig, ladder_frame(args.data_dir), entries, args.trade_gold, include_quiet=args.all)
    if table.height == 0:
        print("no signals at the latest scan")
        return 0
    money = ["price", "baseline_price", "avg_unit_price"]
    out = table.with_columns(
        pl.col("scanned_at").dt.strftime("%m-%d %H:%M"),
        *[pl.col(c).map_elements(format_copper, return_dtype=pl.String) for c in money],
        pl.col("quantity").cast(pl.Int64),
        (pl.col("short_deviation") * 100).round(0).alias("vs_3d_pct"),
        (pl.col("trend_3d") * 100).round(1).alias("trend_3d_pct_day"),
    ).drop("short_deviation", "trend_3d").rename(
        {"units": f"buy_units_{args.trade_gold:g}g", "avg_unit_price": "buy_avg"})
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=220, fmt_str_lengths=60, tbl_hide_dataframe_shape=True):
        print(out)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    import polars as pl

    from wowfah import watchlist
    from wowfah.backtest import TradeConfig, forward_returns, simulate_trades, summarize_returns
    from wowfah.signals import fetch_market, ladder_frame, signals

    cfg = _signal_config(args)
    sig = signals(fetch_market(args.data_dir), watchlist.load(), cfg, _launch(args.launch_date))
    tcfg = TradeConfig(capital_gold=args.capital_gold, trade_gold=args.trade_gold, max_hold_days=args.max_hold_days)
    trades, summary = simulate_trades(sig, ladder_frame(args.data_dir), cfg, tcfg)
    pct = [pl.col(c).mul(100).round(1) for c in ("mean_ret_1d", "mean_ret_3d", "mean_ret_7d", "hit_rate_3d")]
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, tbl_hide_dataframe_shape=True):
        print("Forward returns after each signal episode, % (after the AH cut for buys):")
        print(summarize_returns(forward_returns(sig, cfg)).with_columns(pct))
        print(f"\nTrade simulation: {tcfg}")
        for k, v in summary.items():
            print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")
        if trades.height:
            print(trades.group_by("mode", "exit_reason").agg(
                trades=pl.len(), pnl_gold=(pl.col("pnl").sum() / 10_000).round(1),
                avg_return_pct=(pl.col("return").mean() * 100).round(1),
                win_rate_pct=((pl.col("pnl") > 0).mean() * 100).round(0),
                avg_price_impact_pct=(pl.col("price_impact").mean() * 100).round(1),
            ).sort("mode", "exit_reason"))
    return 0


def cmd_items_import(args: argparse.Namespace) -> int:
    from wowfah import items
    from wowfah.ingest import _write_atomic

    raw = args.data_dir / "raw" / args.branch
    sparse = args.itemsparse or items.download("ItemSparse", args.branch, raw / "ItemSparse.csv")
    item = args.item or items.download("Item", args.branch, raw / "Item.csv")
    df = items.build_items(sparse, item, args.branch)
    _write_atomic(df, items.items_path(args.data_dir))
    print(f"{df.height} items ({df['is_commodity'].sum()} match the commodity rule) -> {items.items_path(args.data_dir)}")
    return 0


def _check_against_items(entries, data_dir: Path) -> int:
    from wowfah import items, watchlist

    df = items.load_items(data_dir)
    if df is None:
        print("no item data; run `wowfah items import` to check ids and names")
        return 0
    errors, warnings = watchlist.check(entries, df)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    return 1 if errors else 0


def cmd_watchlist_check(args: argparse.Namespace) -> int:
    from wowfah import watchlist

    entries = watchlist.load(args.csv)
    print(f"{len(entries)} watched items in {args.csv}")
    return _check_against_items(entries, args.data_dir)


def cmd_watchlist_export(args: argparse.Namespace) -> int:
    from wowfah import watchlist

    entries = watchlist.load(args.csv)
    if _check_against_items(entries, args.data_dir):
        print("not exported; fix the errors above")
        return 1
    watchlist.export(args.csv, args.output)
    print(f"exported {len(entries)} items to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from wowfah import watchlist

    parser = argparse.ArgumentParser(prog="wowfah", description="WoW Forever auction house data pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="convert a WoWFAH.lua SavedVariables file to Parquet")
    p.add_argument("savedvariables", type=Path)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--force", action="store_true", help="rewrite scans that were already ingested")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("probes", help="print probe logs stored by /wowfah probe")
    p.add_argument("savedvariables", type=Path)
    p.set_defaults(func=cmd_probes)

    p = sub.add_parser("dummy", help="write a fake SavedVariables file")
    p.add_argument("output", type=Path)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--scans", type=int, default=3)
    p.add_argument("--depth-scale", type=float, default=1.0, help="multiply every item's listing count")
    p.set_defaults(func=cmd_dummy)

    p = sub.add_parser("sql", help="run SQL against the views: scans, item_scans, ladder, market, buy_price(n), ...")
    p.add_argument("query")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--max-rows", type=int, default=40)
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("simulate", help="write a synthetic launch economy (for developing signals)")
    p.add_argument("data_dir", type=Path)
    p.add_argument("--days", type=int, default=42)
    p.add_argument("--scans-per-day", type=int, default=4)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--depth-scale", type=float, default=1.0)
    p.add_argument("--categories", help="comma-separated watchlist categories (default: all)")
    p.set_defaults(func=cmd_simulate)

    for name, func, help_ in [
        ("signals", cmd_signals, "mature BUY/SELL and launch DIP/SPIKE/FADE at the latest scan of each item"),
        ("backtest", cmd_backtest, "forward returns and a trade simulation over all scans"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
        p.add_argument("--launch-date", help="YYYY-MM-DD (default: first scan)")
        p.add_argument("--days-to-60", type=float, default=40.0, help="assumed days until the median player is 60")
        p.add_argument("--trade-gold", type=float, default=100 if name == "signals" else 250)
        if name == "signals":
            p.add_argument("--all", action="store_true", help="include items without a signal")
        else:
            p.add_argument("--capital-gold", type=float, default=5000)
            p.add_argument("--max-hold-days", type=float, default=7)
        p.set_defaults(func=func)

    items_p = sub.add_parser("items", help="item metadata").add_subparsers(dest="items_command", required=True)
    p = items_p.add_parser("import", help="build data/items/items.parquet from wago.tools DB2 CSVs")
    p.add_argument("--branch", default="wow_classic_era", help="wago.tools branch to download")
    p.add_argument("--itemsparse", type=Path, help="local ItemSparse.csv instead of downloading")
    p.add_argument("--item", type=Path, help="local Item.csv instead of downloading")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.set_defaults(func=cmd_items_import)

    wl = sub.add_parser("watchlist", help="the commodity watchlist").add_subparsers(dest="wl_command", required=True)
    for name, func, help_ in [
        ("check", cmd_watchlist_check, "validate watchlist.csv against item data"),
        ("export", cmd_watchlist_export, "validate and write addon/WoWFAH/Watchlist.lua"),
    ]:
        p = wl.add_parser(name, help=help_)
        p.add_argument("--csv", type=Path, default=watchlist.DEFAULT_CSV)
        p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
        if name == "export":
            p.add_argument("--output", type=Path, default=watchlist.DEFAULT_LUA)
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
