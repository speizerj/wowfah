# wowfah

Manual auction house snapshots from WoW Forever, dumped for offline economy analysis.

- `addon/WoWFAH/`: an in-game addon that captures a full AH snapshot when you ask for one and saves the raw rows.
- `wowfah/`: a Python pipeline that turns SavedVariables into Parquet and queries it with DuckDB.

## Addon

Copy `addon/WoWFAH` into `Interface/AddOns/`, then at the auction house run:

| Command | Effect |
| --- | --- |
| `/wowfah scan` | full snapshot (needs the AH window open) |
| `/wowfah status` | stored scans and rows, plus progress of a running scan |
| `/wowfah abort` | cancel a running scan |
| `/wowfah clear confirm` | delete stored scans (do this after ingesting) |

Data is written to disk on `/reload` or logout:
`WTF/Account/<ACCOUNT>/SavedVariables/WoWFAH.lua`.

The addon picks its API at runtime. On clients with `C_AuctionHouse.ReplicateItems` it uses the modern path; on clients with `QueryAuctionItems` it uses Classic getAll, which the server throttles to about one full scan every 15 minutes. Rows whose item data isn't cached yet are retried a few times. Any that still fail are kept with `complete = 0`. The `## Interface` number in the `.toc` is a placeholder until the client build is known.

## Pipeline

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'

wowfah ingest path/to/WoWFAH.lua             # -> data/auctions/*.parquet, data/scans/*.parquet
wowfah sql "SELECT * FROM item_prices LIMIT 20"
wowfah dummy /tmp/WoWFAH.lua --scans 4       # fake SavedVariables for experimenting
```

Ingest is idempotent: a scan that is already in `data/scans/` is skipped unless you pass `--force`.

DuckDB views:

- `scans`: one row per snapshot (realm, faction, API, timing, and row/incomplete counts).
- `auctions`: one row per auction per scan. Money is in copper and covers the whole stack. `unit_buyout` is null for bid-only auctions.
- `item_prices`: per scan and item: listings, quantity, sellers, and min/median unit buyout.

The column schema lives in `wowfah/schema.py`, and the addon's packed row order (`ns.ROW_FORMAT`) must match it. Each scan stores its own `rowFormat`, so older scans still decode after the format changes.

## Tests

```sh
.venv/bin/python -m pytest
```

`tests/test_addon.py` runs the real addon Lua under lupa against a mocked WoW API (`tests/wow_mock.lua`). It covers both API flavors, chunked reads, cache retries, throttling, aborts, and a full addon → SavedVariables → Parquet round trip.
