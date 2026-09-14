local addonName, ns = ...

ns.VERSION = "0.2.0"
ns.SCHEMA_VERSION = 2

-- Field order for packed ladder rows. Stored alongside every scan so the
-- offline pipeline can decode old scans after this list changes. Keep in sync
-- with wowfah/schema.py LADDER_FORMAT.
ns.LADDER_FORMAT = { "unitPrice", "stackSize", "timeLeft", "listings", "quantity" }

local FIELD_SEP = "\t"

function ns.Print(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99WoWFAH|r: " .. tostring(msg))
end

-- Packs values into one tab-separated string; nil becomes an empty field.
-- One string per ladder row keeps SavedVariables small and fast to load.
function ns.PackFields(values, n)
    local out = {}
    for i = 1, n do
        local v = values[i]
        if v == nil then
            out[i] = ""
        else
            out[i] = (tostring(v):gsub("[\t\r\n]", " "))
        end
    end
    return table.concat(out, FIELD_SEP)
end

function ns.Now()
    if GetServerTime then
        return GetServerTime()
    end
    return time()
end

-- Sub-second clock for timing pages; not a wall-clock time.
function ns.Clock()
    if GetTimePreciseSec then
        return GetTimePreciseSec()
    end
    return GetTime()
end

function ns.FormatDuration(seconds)
    seconds = math.floor(seconds + 0.5)
    if seconds < 60 then
        return seconds .. "s"
    elseif seconds < 3600 then
        return string.format("%dm%02ds", math.floor(seconds / 60), seconds % 60)
    end
    return string.format("%dh%02dm", math.floor(seconds / 3600), math.floor(seconds % 3600 / 60))
end

-- One-line description of any values, expanding tables one level with sorted keys.
function ns.Describe(...)
    local parts = {}
    for i = 1, select("#", ...) do
        local v = select(i, ...)
        if type(v) == "table" then
            local keys = {}
            for k in pairs(v) do
                keys[#keys + 1] = k
            end
            table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
            local fields = {}
            for _, k in ipairs(keys) do
                local fv = v[k]
                fields[#fields + 1] = tostring(k) .. "=" .. (type(fv) == "table" and "{...}" or tostring(fv))
            end
            parts[#parts + 1] = "{" .. table.concat(fields, ", ") .. "}"
        elseif type(v) == "string" then
            parts[#parts + 1] = string.format("%q", v)
        else
            parts[#parts + 1] = tostring(v)
        end
    end
    return table.concat(parts, ", ")
end

-- Client facts the adapters depend on, for probe logs.
function ns.Environment(adapter)
    local lines = { "addon " .. ns.VERSION .. ", adapter " .. adapter.name }
    if GetBuildInfo then
        lines[#lines + 1] = "build: " .. ns.Describe(GetBuildInfo())
    end
    lines[#lines + 1] = "realm/faction: " .. ns.Describe(GetRealmName(), UnitFactionGroup("player"))
    if GetCurrentRegion then
        lines[#lines + 1] = "region: " .. ns.Describe(GetCurrentRegion(), GetCurrentRegionName and GetCurrentRegionName())
    end
    lines[#lines + 1] = "WOW_PROJECT_ID=" .. tostring(WOW_PROJECT_ID)
        .. " NUM_AUCTION_ITEMS_PER_PAGE=" .. tostring(NUM_AUCTION_ITEMS_PER_PAGE)
    local globals = { "QueryAuctionItems", "CanSendAuctionQuery", "GetNumAuctionItems", "GetAuctionItemInfo",
        "GetAuctionItemTimeLeft", "GetTimePreciseSec" }
    local found = {}
    for _, name in ipairs(globals) do
        found[#found + 1] = name .. "=" .. type(_G[name])
    end
    lines[#lines + 1] = "globals: " .. table.concat(found, " ")
    if type(C_AuctionHouse) == "table" then
        local names = {}
        for k, v in pairs(C_AuctionHouse) do
            if type(v) == "function" then
                names[#names + 1] = k
            end
        end
        table.sort(names)
        lines[#lines + 1] = "C_AuctionHouse: " .. table.concat(names, " ")
    else
        lines[#lines + 1] = "C_AuctionHouse: " .. type(C_AuctionHouse)
    end
    return lines
end

function ns.InitDB()
    WoWFAH_DB = WoWFAH_DB or {}
    local db = WoWFAH_DB
    if db.schemaVersion ~= ns.SCHEMA_VERSION then
        -- Scans from an older layout can't be ingested by the current pipeline.
        db.scans = {}
    end
    db.schemaVersion = ns.SCHEMA_VERSION
    db.scans = db.scans or {}
    db.probes = db.probes or {}
    db.itemStats = db.itemStats or {} -- itemId -> { pages, listings } from the last ok scan, for estimates
    return db
end

-- Watchlist entry by item id or name (case-insensitive), falling back to the client's item cache.
function ns.FindItem(query)
    local id = tonumber(query)
    for _, e in ipairs(ns.WATCHLIST or {}) do
        if (id and e[1] == id) or (not id and e[2]:lower() == query:lower()) then
            return { itemId = e[1], name = e[2], category = e[3] }
        end
    end
    if GetItemInfo then
        local name, link = GetItemInfo(id or query)
        local linkId = link and tonumber(link:match("item:(%d+)"))
        if name and linkId then
            return { itemId = linkId, name = name }
        end
    end
    return nil
end

-- Watchlist entries matching a category, or all of them when category is empty.
function ns.WatchlistFor(category)
    local out = {}
    for _, e in ipairs(ns.WATCHLIST or {}) do
        if category == nil or category == "" or e[3] == category then
            out[#out + 1] = { itemId = e[1], name = e[2], category = e[3] }
        end
    end
    return out
end

-- Event dispatch: one handler per event.
local frame = CreateFrame("Frame")
local handlers = {}

function ns.RegisterEvent(event, handler)
    handlers[event] = handler
    frame:RegisterEvent(event)
end

function ns.UnregisterEvent(event)
    handlers[event] = nil
    frame:UnregisterEvent(event)
end

frame:SetScript("OnEvent", function(_, event, ...)
    local handler = handlers[event]
    if handler then
        handler(event, ...)
    end
end)

ns.RegisterEvent("ADDON_LOADED", function(_, name)
    if name ~= addonName then
        return
    end
    ns.InitDB()
    ns.UnregisterEvent("ADDON_LOADED")
end)

ns.ahOpen = false

ns.RegisterEvent("AUCTION_HOUSE_SHOW", function()
    ns.ahOpen = true
end)

ns.RegisterEvent("AUCTION_HOUSE_CLOSED", function()
    ns.ahOpen = false
    if ns.Scan:IsRunning() then
        ns.Scan:Abort("auction house closed")
    end
end)

local commands = {}

function commands.scan(category)
    category = category ~= "" and category or nil
    ns.Scan:Start(ns.WatchlistFor(category), { category = category })
end

-- /wowfah probe <item name or id> [pages]
function commands.probe(arg)
    local query, pages = arg:match("^(.-)%s+(%d+)$")
    if not query or query == "" then
        query, pages = arg, nil
    end
    if query == "" then
        return ns.Print("usage: /wowfah probe <item name or id> [pages, default 3]")
    end
    local entry = ns.FindItem(query)
    if not entry then
        return ns.Print("unknown item " .. query .. " (not watched and not in the client's item cache)")
    end
    ns.Scan:Start({ entry }, { probe = true, maxPages = math.min(tonumber(pages) or 3, 20) })
end

function commands.abort()
    if ns.Scan:IsRunning() then
        ns.Scan:Abort("aborted by user")
    else
        ns.Print("no scan running")
    end
end

function commands.list()
    local counts, order = {}, {}
    for _, e in ipairs(ns.WATCHLIST or {}) do
        if not counts[e[3]] then
            order[#order + 1] = e[3]
        end
        counts[e[3]] = (counts[e[3]] or 0) + 1
    end
    local parts = {}
    for _, c in ipairs(order) do
        parts[#parts + 1] = string.format("%s (%d)", c, counts[c])
    end
    ns.Print(string.format("%d watched items: %s", #(ns.WATCHLIST or {}), table.concat(parts, ", ")))
end

function commands.status()
    local db = ns.InitDB()
    local items = 0
    for _, scan in ipairs(db.scans) do
        items = items + (scan.itemsScanned or 0)
    end
    ns.Print(string.format("%d stored scan(s), %d item scans, %d probe log(s)", #db.scans, items, #db.probes))
    local progress = ns.Scan:Progress()
    if progress then
        ns.Print(progress)
    end
end

function commands.clear(arg)
    local db = ns.InitDB()
    if arg ~= "confirm" then
        ns.Print(string.format("this deletes %d stored scan(s) and %d probe log(s); type /wowfah clear confirm",
            #db.scans, #db.probes))
        return
    end
    db.scans = {}
    db.probes = {}
    ns.Print("stored scans and probe logs cleared")
end

function commands.help()
    ns.Print("/wowfah scan [category] | probe <item> [pages] | list | status | abort | clear")
end

SLASH_WOWFAH1 = "/wowfah"
SlashCmdList.WOWFAH = function(msg)
    local cmd, arg = (msg or ""):match("^%s*(%S*)%s*(.-)%s*$")
    local fn = commands[cmd:lower()] or commands.help
    fn(arg)
end
