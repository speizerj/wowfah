-- Minimal WoW client mock for running the addon under lupa.
-- The Python harness loads auctions into M.auctions:
--   { itemId, name, count, buyout, minBid, timeLeft, timeLeftSeconds, unreadable }
-- Server behaviour knobs: listDelay, queryCooldown (throttle after each query),
-- modernPageSize, nonCommodity[itemId], silentNames[name] (queries never answered).

local M = {
    frames = {},
    timers = {},
    timerSeq = 0,
    now = 0,
    baseTime = 1789646400,
    messages = {},
    auctions = {},
    queryLog = {},
    throttledUntil = 0,
    throttleViolations = 0,
    queryCooldown = 0.5,
    realm = "Dreamscythe",
    faction = "Alliance",
    listDelay = 0.2,
    modernPageSize = 100,
    nonCommodity = {},
    silentNames = {},
}
WOWMOCK = M

function CreateFrame()
    local f = { events = {}, scripts = {} }
    function f:RegisterEvent(e) self.events[e] = true end
    function f:UnregisterEvent(e) self.events[e] = nil end
    function f:SetScript(name, fn) self.scripts[name] = fn end
    M.frames[#M.frames + 1] = f
    return f
end

function M.fire(event, ...)
    for _, f in ipairs(M.frames) do
        if f.events[event] and f.scripts.OnEvent then
            f.scripts.OnEvent(f, event, ...)
        end
    end
end

C_Timer = {
    After = function(delay, fn)
        M.timerSeq = M.timerSeq + 1
        M.timers[#M.timers + 1] = { at = M.now + delay, seq = M.timerSeq, fn = fn }
    end,
}

-- Runs due timers in time order until none remain or maxSteps is hit.
function M.runTimers(maxSteps)
    local steps = 0
    while #M.timers > 0 and steps < maxSteps do
        local best = 1
        for i = 2, #M.timers do
            local t, b = M.timers[i], M.timers[best]
            if t.at < b.at or (t.at == b.at and t.seq < b.seq) then
                best = i
            end
        end
        local timer = table.remove(M.timers, best)
        if timer.at > M.now then
            M.now = timer.at
        end
        timer.fn()
        steps = steps + 1
    end
    return steps
end

function GetServerTime() return M.baseTime + math.floor(M.now) end
function time() return GetServerTime() end
function GetRealmName() return M.realm end
function UnitFactionGroup() return M.faction, M.faction end

DEFAULT_CHAT_FRAME = {
    AddMessage = function(_, msg) M.messages[#M.messages + 1] = msg end,
}
SlashCmdList = {}

local function ready()
    return M.now >= M.throttledUntil
end

local function sendQuery(entry)
    if not ready() then
        M.throttleViolations = M.throttleViolations + 1
    end
    M.throttledUntil = M.now + M.queryCooldown
    M.queryLog[#M.queryLog + 1] = entry
end

local function answer(name, event, ...)
    if M.silentNames[name] then
        return
    end
    local args = { ... }
    C_Timer.After(M.listDelay, function() M.fire(event, (table.unpack or unpack)(args)) end)
end

local function namesById(itemId)
    for _, a in ipairs(M.auctions) do
        if a.itemId == itemId then
            return a.name
        end
    end
end

function M.installClassic()
    NUM_AUCTION_ITEMS_PER_PAGE = 50
    local results, page = {}, 0

    function QueryAuctionItems(text, _, _, p, _, _, getAll, exactMatch)
        sendQuery({ text = text, page = p, getAll = getAll, exactMatch = exactMatch })
        results, page = {}, p
        for _, a in ipairs(M.auctions) do
            if (exactMatch and a.name == text) or (not exactMatch and a.name:find(text, 1, true)) then
                results[#results + 1] = a
            end
        end
        answer(text, "AUCTION_ITEM_LIST_UPDATE")
    end
    function CanSendAuctionQuery() return ready(), false end
    function GetNumAuctionItems()
        local batch = math.max(0, math.min(50, #results - page * 50))
        return batch, #results
    end
    local function at(i)
        return results[page * 50 + i]
    end
    function GetAuctionItemInfo(_, i)
        local a = at(i)
        if not a then
            return nil
        end
        local itemId = (not a.unreadable) and a.itemId or nil
        return a.name, 136235, a.count, 1, true, 1, "", a.minBid, 0, a.buyout, 0, false, nil,
            "Seller", nil, 0, itemId, itemId ~= nil
    end
    function GetAuctionItemTimeLeft(_, i)
        local a = at(i)
        return a and a.timeLeft
    end
end

function M.installModern()
    local loaded, rows = {}, {}

    local function commodityRows(itemId)
        local out = {}
        for _, a in ipairs(M.auctions) do
            if a.itemId == itemId and a.buyout > 0 then
                out[#out + 1] = {
                    itemID = itemId,
                    quantity = a.count,
                    unitPrice = math.floor(a.buyout / a.count + 0.5),
                    timeLeftSeconds = a.timeLeftSeconds,
                }
            end
        end
        table.sort(out, function(x, y) return x.unitPrice < y.unitPrice end)
        return out
    end

    C_AuctionHouse = {
        MakeItemKey = function(itemId)
            return { itemID = itemId, itemLevel = 0, itemSuffix = 0, battlePetSpeciesID = 0 }
        end,
        IsThrottledMessageSystemReady = ready,
        SendSearchQuery = function(itemKey, sorts, separateOwnerItems)
            local itemId = itemKey.itemID
            sendQuery({ itemId = itemId, kind = "search" })
            local name = namesById(itemId)
            if M.nonCommodity[itemId] then
                return answer(name, "ITEM_SEARCH_RESULTS_UPDATED", itemKey)
            end
            rows[itemId] = commodityRows(itemId)
            loaded[itemId] = math.min(M.modernPageSize, #rows[itemId])
            answer(name, "COMMODITY_SEARCH_RESULTS_UPDATED", itemId)
        end,
        RequestMoreCommoditySearchResults = function(itemId)
            sendQuery({ itemId = itemId, kind = "more" })
            loaded[itemId] = math.min(loaded[itemId] + M.modernPageSize, #rows[itemId])
            answer(namesById(itemId), "COMMODITY_SEARCH_RESULTS_ADDED", itemId)
        end,
        GetNumCommoditySearchResults = function(itemId) return loaded[itemId] or 0 end,
        HasFullCommoditySearchResults = function(itemId)
            return rows[itemId] ~= nil and loaded[itemId] >= #rows[itemId]
        end,
        GetCommoditySearchResultInfo = function(itemId, index)
            if index > (loaded[itemId] or 0) then
                return nil
            end
            return rows[itemId][index]
        end,
    }
end
