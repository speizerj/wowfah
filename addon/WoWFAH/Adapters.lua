local _, ns = ...

-- Each adapter exposes the same interface:
--   Available()        -> bool, whether this client has the API
--   CanQueryAll()      -> ok, reason
--   QueryAll()         -> requests a full snapshot; listEvent fires when ready
--   GetNumItems()      -> number of auctions in the snapshot
--   ReadRow(i)         -> packedRow or nil, complete (i is 1-based)
ns.adapters = {}

local function itemStringFromLink(link)
    if not link then
        return nil
    end
    return link:match("|H(item:[^|]+)|h")
end

local function buildRow(itemId, link, name, count, quality, level, minBid, minIncrement,
                        buyout, bidAmount, highBidder, owner, timeLeft, saleStatus, hasAllInfo)
    if not itemId then
        return nil, false
    end
    local complete = (hasAllInfo and link ~= nil and name ~= nil) and true or false
    local itemString = itemStringFromLink(link) or ("item:" .. itemId)
    return ns.PackRow({
        itemId, itemString, name, count, quality, level,
        minBid, minIncrement, buyout, bidAmount, highBidder and 1 or 0,
        owner, timeLeft, saleStatus, complete and 1 or 0,
    }), complete
end

-- Classic Era style: QueryAuctionItems with getAll.
local Classic = { name = "classic", listEvent = "AUCTION_ITEM_LIST_UPDATE" }
ns.adapters.classic = Classic

function Classic.Available()
    return type(QueryAuctionItems) == "function" and type(GetAuctionItemInfo) == "function"
end

function Classic:CanQueryAll()
    local _, canQueryAll = CanSendAuctionQuery()
    if canQueryAll then
        return true
    end
    return false, "full scan not available yet (the server allows one every ~15 minutes)"
end

function Classic:QueryAll()
    QueryAuctionItems("", nil, nil, 0, nil, nil, true, false, nil)
end

function Classic:GetNumItems()
    local batch = GetNumAuctionItems("list")
    return batch or 0
end

function Classic:ReadRow(i)
    local name, _, count, quality, _, level, _, minBid, minIncrement, buyout, bidAmount,
        highBidder, _, owner, ownerFullName, saleStatus, itemId, hasAllInfo = GetAuctionItemInfo("list", i)
    return buildRow(itemId, GetAuctionItemLink("list", i), name, count, quality, level,
        minBid, minIncrement, buyout, bidAmount, highBidder, ownerFullName or owner,
        GetAuctionItemTimeLeft("list", i), saleStatus, hasAllInfo)
end

-- Modern C_AuctionHouse style: ReplicateItems (0-based indices).
local Modern = { name = "modern", listEvent = "REPLICATE_ITEM_LIST_UPDATE" }
ns.adapters.modern = Modern

function Modern.Available()
    return type(C_AuctionHouse) == "table" and type(C_AuctionHouse.ReplicateItems) == "function"
end

function Modern:CanQueryAll()
    -- No client-side throttle query exists; the server silently rate limits.
    return true
end

function Modern:QueryAll()
    C_AuctionHouse.ReplicateItems()
end

function Modern:GetNumItems()
    return C_AuctionHouse.GetNumReplicateItems() or 0
end

function Modern:ReadRow(i)
    local index = i - 1
    local name, _, count, quality, _, level, _, minBid, minIncrement, buyout, bidAmount,
        highBidder, _, owner, ownerFullName, saleStatus, itemId, hasAllInfo =
        C_AuctionHouse.GetReplicateItemInfo(index)
    return buildRow(itemId, C_AuctionHouse.GetReplicateItemLink(index), name, count, quality, level,
        minBid, minIncrement, buyout, bidAmount, highBidder, ownerFullName or owner,
        C_AuctionHouse.GetReplicateItemTimeLeft(index), saleStatus, hasAllInfo)
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
