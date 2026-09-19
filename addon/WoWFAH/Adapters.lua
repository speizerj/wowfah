local _, ns = ...

-- Each adapter searches one item at a time, a page at a time:
--   Available()                    -> bool, whether this client has the API
--   events                         -> events that may carry search results
--   IsReady()                      -> bool, whether the server accepts a query now
--   Query(item, page)              -> sends the search for page (0-based)
--   Matches(item, event, ...)      -> bool, whether the event answers the pending query
--   ReadPage(item, event)          -> hasMore, notCommodity
--   DumpRows(item, event, limit)   -> raw API return values as strings, for probe logs
--   pageSize                       -> listings per page when the API pages by count, else nil
--   resendEvents                   -> optional {event = true}: the pending query was swallowed and
--                                     should be resent now; the event's first arg is the item id
-- ReadPage records results with ns.AddListing / ns.AddBidOnly and may set
-- item.reportedListings (total the server says exists) and item.unreadable.
ns.adapters = {}

-- Classic Era style: QueryAuctionItems by exact name, 50 auctions per page.
local Classic = { name = "classic", events = { "AUCTION_ITEM_LIST_UPDATE" }, pageSize = 50 }
ns.adapters.classic = Classic

function Classic.Available()
    return type(QueryAuctionItems) == "function" and type(GetAuctionItemInfo) == "function"
end

function Classic:IsReady()
    return (CanSendAuctionQuery()) and true or false
end

function Classic:Query(item, page)
    -- text, minLevel, maxLevel, page, usable, rarity, getAll, exactMatch, filterData
    QueryAuctionItems(item.entry.name, nil, nil, page, false, nil, false, true, nil)
end

function Classic:Matches()
    return true
end

function Classic:ReadPage(item)
    local perPage = NUM_AUCTION_ITEMS_PER_PAGE or self.pageSize
    local batch, total = GetNumAuctionItems("list")
    batch, total = batch or 0, total or 0
    item.reportedListings = total
    local wanted = item.entry.itemId
    for i = 1, batch do
        local _, _, count, _, _, _, _, _, _, buyout, _, _, _, _, _, _, itemId = GetAuctionItemInfo("list", i)
        if not itemId then
            item.unreadable = item.unreadable + 1
        elseif itemId == wanted then -- exact name search can still return same-named items
            count = count or 1
            if buyout and buyout > 0 then
                ns.AddListing(item, math.floor(buyout / count + 0.5), count,
                    GetAuctionItemTimeLeft("list", i) or 0, count)
            else
                ns.AddBidOnly(item, count)
            end
        end
    end
    return batch > 0 and (item.pagesRead + 1) * perPage < total, false
end

function Classic:DumpRows(_, _, limit)
    local batch, total = GetNumAuctionItems("list")
    local lines = { "GetNumAuctionItems: " .. ns.Describe(batch, total) }
    for i = 1, math.min(limit, batch or 0) do
        lines[#lines + 1] = string.format("[%d] %s | timeLeft=%s", i,
            ns.TryDescribe(GetAuctionItemInfo, "list", i), ns.TryDescribe(GetAuctionItemTimeLeft, "list", i))
    end
    return lines
end

-- Modern C_AuctionHouse style: commodity search, more results on request.
local Modern = {
    name = "modern",
    events = { "COMMODITY_SEARCH_RESULTS_UPDATED", "COMMODITY_SEARCH_RESULTS_ADDED", "ITEM_SEARCH_RESULTS_UPDATED" },
    -- Searching an item the client hasn't cached yet only fetches its info; the search itself
    -- is dropped. Seen live on the Forever beta: resending once the info arrives works at once.
    resendEvents = { ITEM_KEY_ITEM_INFO_RECEIVED = true },
}
ns.adapters.modern = Modern

function Modern.Available()
    return type(C_AuctionHouse) == "table" and type(C_AuctionHouse.SendSearchQuery) == "function"
end

function Modern:IsReady()
    local ready = C_AuctionHouse.IsThrottledMessageSystemReady
    return ready == nil or ready() and true or false
end

function Modern:Query(item, page)
    local itemId = item.entry.itemId
    if page == 0 then
        item.read = 0
        C_AuctionHouse.SendSearchQuery(C_AuctionHouse.MakeItemKey(itemId), {}, false)
    else
        C_AuctionHouse.RequestMoreCommoditySearchResults(itemId)
    end
end

function Modern:Matches(item, event, arg)
    if event == "ITEM_SEARCH_RESULTS_UPDATED" then
        return type(arg) == "table" and arg.itemID == item.entry.itemId
    end
    return arg == item.entry.itemId
end

-- Seconds -> the classic time left buckets used across the pipeline.
local function timeLeftBucket(seconds)
    if not seconds then
        return 0
    elseif seconds < 30 * 60 then
        return 1
    elseif seconds < 2 * 3600 then
        return 2
    elseif seconds < 12 * 3600 then
        return 3
    end
    return 4
end
ns.TimeLeftBucket = timeLeftBucket

function Modern:ReadPage(item, event)
    if event == "ITEM_SEARCH_RESULTS_UPDATED" then
        return false, true
    end
    local itemId = item.entry.itemId
    local n = C_AuctionHouse.GetNumCommoditySearchResults(itemId) or 0
    local before = item.read or 0
    for i = before + 1, n do
        local r = C_AuctionHouse.GetCommoditySearchResultInfo(itemId, i)
        if r and r.unitPrice then
            ns.AddListing(item, r.unitPrice, 0, timeLeftBucket(r.timeLeftSeconds), r.quantity or 0)
        else
            item.unreadable = item.unreadable + 1
        end
    end
    item.read = n
    -- One search returns the market already grouped into price tiers, cheapest first, and
    -- the aggregate call gives total depth -- so never page. If the server held rows back,
    -- the ladder is the cheap end only; flag it, total units still come from the aggregate.
    local okQty, total = pcall(C_AuctionHouse.GetCommoditySearchResultsQuantity, itemId)
    item.reportedQuantity = okQty and total or nil
    item.capped = not C_AuctionHouse.HasFullCommoditySearchResults(itemId)
    return false, false
end

function Modern:DumpRows(item, event, limit)
    local itemId = item.entry.itemId
    local lines = {}
    if C_AuctionHouse.GetItemCommodityStatus then
        lines[1] = "GetItemCommodityStatus: " .. ns.TryDescribe(C_AuctionHouse.GetItemCommodityStatus, itemId)
    end
    if event == "ITEM_SEARCH_RESULTS_UPDATED" then
        return lines
    end
    local ok, n = pcall(C_AuctionHouse.GetNumCommoditySearchResults, itemId)
    n = (ok and n) or 0
    lines[#lines + 1] = string.format("GetNumCommoditySearchResults: %s, HasFull: %s",
        ok and tostring(n) or "ERROR", ns.TryDescribe(C_AuctionHouse.HasFullCommoditySearchResults, itemId))
    -- These claim to give total depth/max price without walking every page -- unverified
    -- against a live client yet. If they check out, ReadPage can use them instead of paging
    -- to completion for every item (see Modern:ReadPage TODO).
    if C_AuctionHouse.GetCommoditySearchResultsQuantity then
        lines[#lines + 1] = "GetCommoditySearchResultsQuantity: " ..
            ns.TryDescribe(C_AuctionHouse.GetCommoditySearchResultsQuantity, itemId)
    end
    if C_AuctionHouse.GetMaxCommoditySearchResultPrice then
        lines[#lines + 1] = "GetMaxCommoditySearchResultPrice: " ..
            ns.TryDescribe(C_AuctionHouse.GetMaxCommoditySearchResultPrice, itemId)
    end
    -- The new rows, plus index 0 and n + 1, which show whether results are 0- or 1-based.
    local first = (item.read or 0) + 1
    local indexes = { 0 }
    for i = first, math.min(first + limit - 1, n) do
        indexes[#indexes + 1] = i
    end
    indexes[#indexes + 1] = n + 1
    for _, i in ipairs(indexes) do
        lines[#lines + 1] = string.format("[%d] %s", i,
            ns.TryDescribe(C_AuctionHouse.GetCommoditySearchResultInfo, itemId, i))
    end
    return lines
end

function ns.DetectAdapter()
    if Modern.Available() then
        return Modern
    end
    if Classic.Available() then
        return Classic
    end
    return nil, "no supported auction house API found on this client"
end
