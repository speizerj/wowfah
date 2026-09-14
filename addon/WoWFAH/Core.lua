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

function ns.InitDB()
    WoWFAH_DB = WoWFAH_DB or {}
    local db = WoWFAH_DB
    if db.schemaVersion ~= ns.SCHEMA_VERSION then
        -- Scans from an older layout can't be ingested by the current pipeline.
        db.scans = {}
    end
    db.schemaVersion = ns.SCHEMA_VERSION
    db.scans = db.scans or {}
    return db
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
    ns.Scan:Start(category)
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
    ns.Print(string.format("%d stored scan(s), %d item scans", #db.scans, items))
    local st = ns.Scan.state
    if st and st.item then
        ns.Print(string.format("scan running: item %d/%d %s, page %d",
            st.index, #st.entries, st.item.entry.name, st.item.pagesRead + 1))
    end
end

function commands.clear(arg)
    local db = ns.InitDB()
    if arg ~= "confirm" then
        ns.Print(string.format("this deletes %d stored scan(s); type /wowfah clear confirm", #db.scans))
        return
    end
    db.scans = {}
    ns.Print("stored scans cleared")
end

function commands.help()
    ns.Print("/wowfah scan [category] | list | status | abort | clear")
end

SLASH_WOWFAH1 = "/wowfah"
SlashCmdList.WOWFAH = function(msg)
    local cmd, arg = (msg or ""):match("^%s*(%S*)%s*(.-)%s*$")
    local fn = commands[cmd:lower()] or commands.help
    fn(arg)
end
