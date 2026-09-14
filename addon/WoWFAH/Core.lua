local addonName, ns = ...

ns.VERSION = "0.1.0"
ns.SCHEMA_VERSION = 1

-- Column order for packed rows. Stored alongside every scan so the offline
-- pipeline can decode old scans after this list changes. Keep in sync with
-- wowfah/schema.py ROW_FORMAT.
ns.ROW_FORMAT = {
    "itemId", "itemString", "name", "count", "quality", "level",
    "minBid", "minIncrement", "buyout", "bidAmount", "highBidder",
    "owner", "timeLeft", "saleStatus", "complete",
}

local FIELD_SEP = "\t"

function ns.Print(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99WoWFAH|r: " .. tostring(msg))
end

-- Packs a row into one tab-separated string; nil becomes an empty field.
-- One string per auction keeps SavedVariables small and fast to load.
function ns.PackRow(values)
    local out = {}
    for i = 1, #ns.ROW_FORMAT do
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
    db.schemaVersion = ns.SCHEMA_VERSION
    db.scans = db.scans or {}
    return db
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
        handler(...)
    end
end)

ns.RegisterEvent("ADDON_LOADED", function(name)
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

local function countRows(db)
    local rows = 0
    for _, scan in ipairs(db.scans) do
        rows = rows + (scan.rowCount or 0)
    end
    return rows
end

local commands = {}

function commands.scan()
    ns.Scan:Start()
end

function commands.abort()
    if ns.Scan:IsRunning() then
        ns.Scan:Abort("aborted by user")
    else
        ns.Print("no scan running")
    end
end

function commands.status()
    local db = ns.InitDB()
    ns.Print(string.format("%d stored scan(s), %d auction rows", #db.scans, countRows(db)))
    local st = ns.Scan.state
    if st then
        ns.Print(string.format("scan running: phase=%s read=%d/%d", st.phase, (st.cursor or 1) - 1, st.total or 0))
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
    ns.Print("/wowfah scan | status | abort | clear")
end

SLASH_WOWFAH1 = "/wowfah"
SlashCmdList.WOWFAH = function(msg)
    local cmd, arg = (msg or ""):match("^%s*(%S*)%s*(.-)%s*$")
    local fn = commands[cmd:lower()] or commands.help
    fn(arg)
end
