# wowfah

Manual, full-depth auction house scans of a commodity watchlist from WoW Forever, dumped for offline price analysis.

- `addon/WoWFAH/`: an in-game addon. It searches each watched item page by page until the whole market is read, then saves a price ladder per item.
- `wowfah/`: a Python pipeline that turns SavedVariables into Parquet and queries it with DuckDB.
- `watchlist.csv`: the commodities to scan. You edit it by hand and export it to the addon.

## Addon

Copy `addon/WoWFAH` into `Interface/AddOns/`, then at the auction house run:

| Command | Effect |
| --- | --- |
| `/wowfah scan` | scan every watched item at full depth (needs the AH window open) |
| `/wowfah scan <category>` | scan one category, e.g. `/wowfah scan herb` |
| `/wowfah probe <item> [pages]` | instrumented scan of one item (default 3 pages) that saves a diagnostic log instead of a scan |
| `/wowfah list` | watched item counts per category |
| `/wowfah status` | stored scans, plus progress and time left for a running scan |
| `/wowfah abort` | stop; items already finished are kept |
| `/wowfah clear confirm` | delete stored scans (do this after ingesting) |

Data is written to disk on `/reload` or logout:
`WTF/Account/<ACCOUNT>/SavedVariables/WoWFAH.lua`.

The addon picks its API at runtime:

- **Classic** (`QueryAuctionItems`): searches the exact item name, reads every 50-auction page, and keeps only rows whose item id matches. Some items share a name, e.g. Dark Iron Ore. It waits for `CanSendAuctionQuery` before each page. The server's total listing count is stored as `reportedListings`.
- **Modern** (`C_AuctionHouse`): commodity search, then `RequestMoreCommoditySearchResults` until the results are complete. Items that the client doesn't treat as commodities are stored with status `not_commodity`.

If a page gets no answer within 30s, that item is marked `timeout` and the scan moves on. Three timeouts in a row abort the scan. Closing the AH also aborts it, and in both cases the finished items are kept.

**Time estimates.** After each scan the addon remembers every item's page count and the average seconds per page. `/wowfah scan` prints a rough duration once every item has history. `/wowfah status` shows the current page against the expected total, elapsed time, and time left. The estimate uses the server's reported total for the current item and last scan's page counts for the rest.

**Probes.** `/wowfah probe Peacebloom` logs:
- the client build, region, and which API functions exist
- each query with its timing and throttle waits
- every event with its arguments, including duplicate or late events, which are flagged `[stray]`
- raw return values for the first rows of each page

It keeps listening 3s after the last page to catch late events. The last 5 probe logs are kept. Print them with `wowfah probes path/to/WoWFAH.lua`.

For each item, the stored ladder holds one row per `(unit price, stack size, time left)` with listing and unit counts. Bid-only auctions are counted separately. The `## Interface` number in the `.toc` is a placeholder until the client build is known.

## Pipeline

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'

wowfah items import                          # download Classic Era item data from wago.tools -> data/items/
wowfah watchlist check                       # validate watchlist.csv ids and names against item data
wowfah watchlist export                      # write addon/WoWFAH/Watchlist.lua
wowfah ingest path/to/WoWFAH.lua             # -> data/{scans,item_scans,ladder}/*.parquet
wowfah sql "SELECT * FROM market LIMIT 20"
wowfah probes path/to/WoWFAH.lua             # print /wowfah probe logs
wowfah signals                               # BUY/SELL at the latest scan of each item
wowfah backtest                              # forward returns + trade simulation over all scans
wowfah simulate data-sim --seed 3            # synthetic launch economy to try signals on
wowfah dummy /tmp/WoWFAH.lua --scans 4       # fake SavedVariables for experimenting
```

`items import` takes `--branch` (any wago.tools branch, e.g. a future WoW Forever one), or `--itemsparse`/`--item` for local CSVs. The commodity rule is: stackable, not bind-on-pickup, class Consumable/Reagent/Trade Goods, common quality or better. Watchlist items that don't match it are allowed with a warning. The dragonscales, for example, are class Miscellaneous.

Ingest is idempotent: a scan that is already in `data/scans/` is skipped unless you pass `--force`.

DuckDB views:

- `scans`: one row per scan (API, category filter, `complete`/`aborted`, timing, item counts).
- `item_scans`: one row per watched item per scan: status, pages, listings read against listings reported, units, bid-only units.
- `ladder`: raw price ladder rows. Money is copper per unit. `stack_size` 0 means partial buys are allowed (modern commodities).
- `market`: per scan and item, `ok` items only. Units, min price, quantity-weighted p10/p25/median, and expiring against very-long units.
- `buy_price(n)`: average and max unit price to buy the cheapest `n` units. It treats stacks as divisible.
- `items`, `watchlist`: item metadata (after `items import`) and `watchlist.csv`.

The column schema lives in `wowfah/schema.py`, and the addon's packed ladder order (`ns.LADDER_FORMAT`) must match it. Each scan stores its own `ladderFormat`, so older scans still decode after the format changes.

## Signals

`wowfah signals` shows the latest scan of every item that has a signal (`--all` for every item). Each row has:
- the reason
- price (quantity-weighted p10 unit price) and the 14-day baseline (mature items only)
- `vs_3d_pct` / `trend_3d_pct_day`: price against its 3-day median, and the trend before this scan (launch items; still shown for mature)
- how many units a `--trade-gold` buy would get and at what average price. The fill takes only listings within 5% of the signal price and at most 10% of what's listed -- **only for mature BUY/SELL**. Launch-mode rows always show `null` here: they're not orders, see below.

Each item is in one of two modes, depending on how much history it has, and the two modes speak in different voices on purpose:

**Mature (14+ days of history) -- real trade instructions**, because they're just measuring what this item's own market already did:

| Signal | When |
| --- | --- |
| BUY | price at least 20% under its 14-day median, supply at least 1.25x its median, the 7-day trend not collapsing, and a return to baseline clears the 5% AH cut by 10% or more |
| SELL | price at least 20% over its 14-day median with supply under 0.85x |

**Launch (under 14 days of history) -- descriptive tags, not orders.** There's no baseline yet, and the "prior" behind these tags is a guess from `watchlist.csv` (bracket, tags), not something learned from the item itself. So these are flagged for you to judge, not acted on: `wowfah signals` never shows a fill for them, and `wowfah backtest`'s trade simulation never trades them (it only trades mature BUY/SELL).

| Tag | When |
| --- | --- |
| DIP | the watchlist prior says demand is rising, and price is at least 15% below its 3-day median while the trend before this scan wasn't falling |
| SPIKE | price is 30% or more above its 3-day median |
| FADE | the prior says demand has passed its peak, and price is trending down |

**The launch prior** comes from `watchlist.csv`:
- `leveling` items are "rising" until the median player passes the top of the bracket, then "fading".
- `raid` and `pvp` items are rising until two weeks after the median player hits 60.
- Untagged items have no prior.

The pace is `--days-to-60` (default 40) and `--launch-date` (default: first scan). All thresholds live in `SignalConfig` in `wowfah/signals.py`.

**`wowfah backtest`** reports two things:
- The average 1/3/7-day return after each signal episode (including launch's DIP/SPIKE/FADE), compared with buying at any scan. This is how to check whether the descriptive tags are worth promoting to real signals later, without ever having traded on them.
- A trade simulation, mature BUY/SELL only. It fills against the real ladder, takes profit once proceeds after the cut beat cost by 5%, exits on a SELL signal or after 7 days, sells 1% under the cheapest listing, and sells at most 20% of listed quantity per scan.

**`wowfah simulate`** generates a synthetic launch economy:
- leveling-population demand waves
- bots from week 2, and ban waves
- supply dumps
- inflation
- raid-night demand
- persistent price shocks

It writes the ingest layout plus `truth/` (fair prices, events).

**Results on simulated data** (42 days, all 219 items, 5,000g capital, 250g per trade, 3 seeds each):

| Price shock half-life | Mature BUY, 3-day return | Launch DIP, 3-day return (not traded) | Trade sim P&L | Win rate |
| --- | --- | --- | --- | --- |
| 0.5 days | +43–47% | +26–40% | +1,770 to +2,160g | 90% |
| 1.5 days (default) | +48% | +36–46% | +1,360 to +1,850g | 88% |
| 4 days | +47–53% | +41–54% | +1,220 to +1,710g | 86% |

These numbers test the mechanics. They don't predict real profit.
- **The simulator is built on the same story the signals assume:** supply gluts revert, and demand follows the leveling population.
- **An early momentum version of launch BUY lost money** (−4 to −10% after 3 days) and was replaced by dip buying. That replacement was checked on held-out seeds, but it was still chosen by looking at simulated results.
- **Once real scans exist,** run `wowfah backtest` on them before trusting any threshold.

## Tests

```sh
.venv/bin/python -m pytest
```

`tests/test_addon.py` runs the real addon Lua under lupa against a mocked WoW API (`tests/wow_mock.lua`). It covers:

- both API flavors
- multi-page reads
- same-named decoy items
- throttling
- timeouts and aborts
- category filters
- a full addon → SavedVariables → Parquet → `market` round trip

It also fails if `Watchlist.lua` is out of date with `watchlist.csv`.

## To confirm in the beta

Run `/wowfah probe Peacebloom 5`, then `/reload`, then `wowfah probes WoWFAH.lua`. That log answers most of these:

- `.toc` Interface number, and which API flavor the client exposes.
- Classic: real query delay, pages per item, and whether stray `AUCTION_ITEM_LIST_UPDATE` events cause a page to be read twice. Compare `listings_read` with `reported_listings`.
- Modern: whether result indexes are 1-based, and whether `timeLeftSeconds` is filled in.
- How long a full watchlist scan takes. Split by category if it's too slow.
