-- Minimal WoW client mock for running the addon under lupa.
-- Auctions are loaded into M.auctions by the Python harness; each auction may
-- set uncachedReads = N so its first N reads look like uncached item data.

local M = {
    frames = {},
    timers = {},
    timerSeq = 0,
    now = 0,
    baseTime = 1789646400,
    messages = {},
    auctions = {},
    queries = 0,
    canQueryAll = true,
    realm = "Dreamscythe",
    faction = "Alliance",
    listDelay = 2,
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

local function info(a)
    if not a then
        return nil
    end
    local cached = (a.uncachedReads or 0) <= 0
    if not cached then
        a.uncachedReads = a.uncachedReads - 1
    end
    local name = cached and a.name or nil
    local owner = cached and a.owner or nil
    return name, 136235, a.count, a.quality, true, a.level, "", a.minBid, a.minIncrement,
        a.buyout, a.bidAmount, a.highBidder, nil, owner, nil, a.saleStatus, a.itemId, cached
end

local function link(a)
    if not a or (a.uncachedReads or 0) > 0 then
        return nil
    end
    return "|cffffffff|Hitem:" .. a.itemId .. "::::::::60:::::|h[" .. a.name .. "]|h|r"
end

function M.installClassic()
    function QueryAuctionItems(...)
        M.queries = M.queries + 1
        M.lastQuery = { ... }
        C_Timer.After(M.listDelay, function() M.fire("AUCTION_ITEM_LIST_UPDATE") end)
    end
    function CanSendAuctionQuery() return true, M.canQueryAll end
    function GetNumAuctionItems() return #M.auctions, #M.auctions end
    function GetAuctionItemInfo(_, i) return info(M.auctions[i]) end
    function GetAuctionItemLink(_, i) return link(M.auctions[i]) end
    function GetAuctionItemTimeLeft(_, i)
        local a = M.auctions[i]
        return a and a.timeLeft
    end
end

function M.installModern()
    C_AuctionHouse = {
        ReplicateItems = function()
            M.queries = M.queries + 1
            C_Timer.After(M.listDelay, function() M.fire("REPLICATE_ITEM_LIST_UPDATE") end)
        end,
        GetNumReplicateItems = function() return #M.auctions end,
        GetReplicateItemInfo = function(index) return info(M.auctions[index + 1]) end,
        GetReplicateItemLink = function(index) return link(M.auctions[index + 1]) end,
        GetReplicateItemTimeLeft = function(index)
            local a = M.auctions[index + 1]
            return a and a.timeLeft
        end,
    }
end
