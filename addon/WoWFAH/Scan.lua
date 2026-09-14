local _, ns = ...

local Scan = {}
ns.Scan = Scan

local READY_POLL = 0.1            -- seconds between throttle checks
local PAGE_TIMEOUT = 30           -- seconds to wait for one page of results
local MAX_CONSECUTIVE_TIMEOUTS = 3
local MAX_PAGES = 5000            -- per item; a safety stop, not an expected depth

function Scan:IsRunning()
    return self.state ~= nil
end

-- Ladder accumulation, called by adapters. Rows are keyed by price, stack size and time left.
function ns.AddListing(item, unitPrice, stackSize, timeLeft, quantity)
    local key = unitPrice .. "\t" .. stackSize .. "\t" .. timeLeft
    local row = item.ladder[key]
    if not row then
        row = { unitPrice, stackSize, timeLeft, 0, 0 }
        item.ladder[key] = row
    end
    row[4] = row[4] + 1
    row[5] = row[5] + quantity
    item.listingsRead = item.listingsRead + 1
    item.quantity = item.quantity + quantity
end

function ns.AddBidOnly(item, quantity)
    item.listingsRead = item.listingsRead + 1
    item.bidOnlyListings = item.bidOnlyListings + 1
    item.bidOnlyQuantity = item.bidOnlyQuantity + quantity
end

function Scan:Start(category)
    if self.state then
        return ns.Print("a scan is already running")
    end
    if not ns.ahOpen then
        return ns.Print("open the auction house first")
    end
    local adapter, err = ns.DetectAdapter()
    if not adapter then
        return ns.Print(err)
    end
    local entries = ns.WatchlistFor(category)
    if #entries == 0 then
        return ns.Print(category ~= "" and ("no watched items in category " .. category) or "the watchlist is empty")
    end

    local st = {
        adapter = adapter,
        category = (category ~= "" and category) or nil,
        entries = entries,
        index = 0,
        startedAt = ns.Now(),
        items = {},
        timeouts = 0,
        seq = 0,
    }
    self.state = st
    for _, event in ipairs(adapter.events) do
        ns.RegisterEvent(event, function(...)
            self:OnEvent(st, ...)
        end)
    end
    ns.Print(string.format("scanning %d item(s) at full depth (%s API); /wowfah abort to stop", #entries, adapter.name))
    self:NextItem(st)
end

function Scan:NextItem(st)
    st.index = st.index + 1
    local entry = st.entries[st.index]
    if not entry then
        return self:Finish(st, "complete")
    end
    st.item = {
        entry = entry,
        startedAt = ns.Now(),
        pagesRead = 0,
        ladder = {},
        listingsRead = 0,
        quantity = 0,
        bidOnlyListings = 0,
        bidOnlyQuantity = 0,
        unreadable = 0,
    }
    self:SendWhenReady(st)
end

function Scan:SendWhenReady(st)
    if self.state ~= st then
        return
    end
    if not st.adapter:IsReady() then
        C_Timer.After(READY_POLL, function()
            self:SendWhenReady(st)
        end)
        return
    end
    st.seq = st.seq + 1
    local seq = st.seq
    st.waiting = seq
    st.adapter:Query(st.item, st.item.pagesRead)
    C_Timer.After(PAGE_TIMEOUT, function()
        if self.state == st and st.waiting == seq then
            self:OnTimeout(st)
        end
    end)
end

function Scan:OnEvent(st, event, ...)
    if self.state ~= st or not st.waiting or not st.adapter:Matches(st.item, event, ...) then
        return
    end
    st.waiting = nil
    local item = st.item
    local hasMore, notCommodity = st.adapter:ReadPage(item, event)
    if notCommodity then
        return self:FinishItem(st, "not_commodity")
    end
    item.pagesRead = item.pagesRead + 1
    st.timeouts = 0
    if hasMore and item.pagesRead < MAX_PAGES then
        return self:SendWhenReady(st)
    end
    self:FinishItem(st, "ok")
end

function Scan:OnTimeout(st)
    st.waiting = nil
    st.timeouts = st.timeouts + 1
    self:FinishItem(st, "timeout")
end

local function packLadder(ladder)
    local rows = {}
    for _, row in pairs(ladder) do
        rows[#rows + 1] = row
    end
    table.sort(rows, function(a, b)
        if a[1] ~= b[1] then return a[1] < b[1] end
        if a[2] ~= b[2] then return a[2] < b[2] end
        return a[3] < b[3]
    end)
    local packed = {}
    for i, row in ipairs(rows) do
        packed[i] = ns.PackFields(row, #ns.LADDER_FORMAT)
    end
    return packed
end

function Scan:FinishItem(st, status)
    local item = st.item
    st.item = nil
    st.items[#st.items + 1] = {
        itemId = item.entry.itemId,
        name = item.entry.name,
        status = status,
        startedAt = item.startedAt,
        finishedAt = ns.Now(),
        pages = item.pagesRead,
        reportedListings = item.reportedListings,
        listingsRead = item.listingsRead,
        quantity = item.quantity,
        bidOnlyListings = item.bidOnlyListings,
        bidOnlyQuantity = item.bidOnlyQuantity,
        unreadable = item.unreadable,
        ladder = packLadder(item.ladder),
    }
    if st.timeouts >= MAX_CONSECUTIVE_TIMEOUTS then
        return self:Abort("the server stopped answering searches")
    end
    self:NextItem(st)
end

function Scan:Finish(st, status)
    for _, event in ipairs(st.adapter.events) do
        ns.UnregisterEvent(event)
    end
    self.state = nil
    if #st.items == 0 then
        return
    end
    local realm = GetRealmName()
    local faction = UnitFactionGroup("player")
    local db = ns.InitDB()
    local failed = 0
    for _, item in ipairs(st.items) do
        if item.status ~= "ok" then
            failed = failed + 1
        end
    end
    db.scans[#db.scans + 1] = {
        scanId = string.format("%s-%s-%d", realm, faction, st.startedAt),
        addonVersion = ns.VERSION,
        api = st.adapter.name,
        realm = realm,
        faction = faction,
        category = st.category,
        status = status,
        startedAt = st.startedAt,
        finishedAt = ns.Now(),
        itemsRequested = #st.entries,
        itemsScanned = #st.items,
        ladderFormat = ns.LADDER_FORMAT,
        items = st.items,
    }
    ns.Print(string.format("scan %s: %d/%d item(s) stored (%d not ok); /reload or log out to save",
        status, #st.items, #st.entries, failed))
end

-- Stops the scan. Items already finished are kept; the one in progress is dropped.
function Scan:Abort(reason)
    local st = self.state
    if not st then
        return
    end
    st.item = nil
    st.waiting = nil
    ns.Print("scan aborted: " .. reason)
    self:Finish(st, "aborted")
end
