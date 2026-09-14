from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path("data")


def cmd_ingest(args: argparse.Namespace) -> int:
    from wowfah.ingest import ingest_savedvariables

    results = ingest_savedvariables(args.savedvariables, args.data_dir, force=args.force)
    for r in results:
        print(f"{r.status:8} {r.rows:7} rows  {r.scan_id}")
    written = sum(r.status == "written" for r in results)
    print(f"{written} scan(s) written, {len(results) - written} skipped")
    return 0


def cmd_dummy(args: argparse.Namespace) -> int:
    from wowfah import savedvars
    from wowfah.dummy import generate_db

    db = generate_db(seed=args.seed, scans=args.scans, auctions_per_scan=args.auctions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    savedvars.dump({"WoWFAH_DB": db}, args.output)
    print(f"wrote {args.scans} fake scan(s) to {args.output}")
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    from wowfah.query import connect

    con = connect(args.data_dir)
    con.sql(args.query).show(max_rows=args.max_rows)
    return 0


def main(argv: list[str] | None = None) -> int:
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
    p.add_argument("--auctions", type=int, default=500)
    p.set_defaults(func=cmd_dummy)

    p = sub.add_parser("sql", help="run SQL against the views: scans, auctions, item_prices")
    p.add_argument("query")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--max-rows", type=int, default=40)
    p.set_defaults(func=cmd_sql)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
