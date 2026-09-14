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
