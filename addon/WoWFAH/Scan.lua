local _, ns = ...

local Scan = {}
ns.Scan = Scan

local READY_POLL = 0.1            -- seconds between throttle checks
local PAGE_TIMEOUT = 30           -- seconds to wait for one page of results
local MAX_CONSECUTIVE_TIMEOUTS = 3
local MAX_PAGES = 5000            -- per item; a safety stop, not an expected depth
local PROBE_LINGER = 3            -- seconds a probe keeps listening for stray events
local PROBE_ROWS = 5              -- raw rows dumped per probed page
local MAX_PROBES = 5              -- probe logs kept in SavedVariables

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

-- Appends a timestamped line to a probe's log; no-op for normal scans.
function Scan:Log(st, fmt, ...)
    if st.log then
        st.log[#st.log + 1] = string.format("%8.3f  ", ns.Clock() - st.clock0) .. string.format(fmt, ...)
    end
end

-- entries: watchlist entries to scan. opts: category, probe (bool), maxPages.
function Scan:Start(entries, opts)
    opts = opts or {}
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
    if #entries == 0 then
        return ns.Print(opts.category and ("no watched items in category " .. opts.category) or "the watchlist is empty")
    end

    local st = {
        adapter = adapter,
        category = opts.category,
        probe = opts.probe,
        maxPages = opts.maxPages or MAX_PAGES,
        entries = entries,
        index = 0,
        startedAt = ns.Now(),
        clock0 = ns.Clock(),
        items = {},
        pagesDone = 0,
        okItems = 0,
        okPages = 0,
        timeouts = 0,
        seq = 0,
        log = opts.probe and {} or nil,
    }
    self.state = st
    for _, event in ipairs(adapter.events) do
        ns.RegisterEvent(event, function(...)
            self:OnEvent(st, ...)
        end)
    end

    if st.probe then
        for _, line in ipairs(ns.Environment(adapter)) do
            self:Log(st, "%s", line)
        end
        ns.Print(string.format("probing %s for up to %d page(s) (%s API)", entries[1].name, st.maxPages, adapter.name))
    else
        -- Only estimate up front when every item has a page count from an earlier scan.
        local eta = self:Estimate(st, true)
        ns.Print(string.format("scanning %d item(s) at full depth (%s API)%s; /wowfah abort to stop", #entries,
            adapter.name, eta and (", roughly " .. ns.FormatDuration(eta)) or ""))
    end
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
    self:Log(st, "item %s (id %s)", entry.name, tostring(entry.itemId))
    self:SendWhenReady(st)
end

function Scan:SendWhenReady(st)
    if self.state ~= st then
        return
    end
    if not st.adapter:IsReady() then
        if not st.throttledSince then
            st.throttledSince = ns.Clock()
        end
        C_Timer.After(READY_POLL, function()
            self:SendWhenReady(st)
        end)
        return
    end
    if st.throttledSince then
        self:Log(st, "throttle cleared after %.3fs", ns.Clock() - st.throttledSince)
        st.throttledSince = nil
    end
    st.seq = st.seq + 1
    local seq = st.seq
    st.waiting = seq
    st.sentAt = ns.Clock()
    self:Log(st, "query page %d", st.item.pagesRead)
    st.adapter:Query(st.item, st.item.pagesRead)
    C_Timer.After(PAGE_TIMEOUT, function()
        if self.state == st and st.waiting == seq then
            self:OnTimeout(st)
        end
    end)
end

function Scan:OnEvent(st, event, ...)
    if self.state ~= st then
        return
    end
    local matches = st.waiting and st.item and st.adapter:Matches(st.item, event, ...)
    if st.log then
        self:Log(st, "event %s(%s)%s", event, ns.Describe(...),
            matches and string.format(" answered after %.3fs", ns.Clock() - st.sentAt) or " [stray: not waiting for it]")
    end
    if not matches then
        return
    end
    st.waiting = nil
    local item = st.item
    if st.log then
        for _, line in ipairs(st.adapter:DumpRows(item, event, PROBE_ROWS)) do
            self:Log(st, "  %s", line)
        end
    end
    local before = item.listingsRead
    local hasMore, notCommodity = st.adapter:ReadPage(item, event)
    if notCommodity then
        self:Log(st, "not a commodity on this client")
        return self:FinishItem(st, "not_commodity")
    end
    item.pagesRead = item.pagesRead + 1
    st.pagesDone = st.pagesDone + 1
    st.timeouts = 0
    self:Log(st, "page read: %d listing(s), %d unreadable so far, reported total %s, more: %s",
        item.listingsRead - before, item.unreadable, tostring(item.reportedListings), tostring(hasMore))
    if hasMore and item.pagesRead < st.maxPages then
        return self:SendWhenReady(st)
    end
    self:FinishItem(st, "ok")
end

function Scan:OnTimeout(st)
    st.waiting = nil
    st.timeouts = st.timeouts + 1
    self:Log(st, "timed out after %ds", PAGE_TIMEOUT)
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
    if status == "ok" then
        st.okItems = st.okItems + 1
        st.okPages = st.okPages + item.pagesRead
        if not st.probe then
            ns.InitDB().itemStats[item.entry.itemId] = { pages = item.pagesRead, listings = item.listingsRead }
        end
    end
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

-- Expected pages for an item: from the server's reported total when known, else the last scan, else nil.
local function expectedPages(st, item, entry)
    local perPage = st.adapter.pageSize
    if item and item.reportedListings and perPage then
        return math.max(1, math.ceil(item.reportedListings / perPage))
    end
    local stats = ns.InitDB().itemStats[entry.itemId]
    return stats and stats.pages or nil
end

-- Seconds left, pages left, seconds per page. Nil when there's nothing to base it on,
-- or with requireHistory when some remaining item has never been scanned.
function Scan:Estimate(st, requireHistory)
    local db = ns.InitDB()
    local elapsed = ns.Clock() - st.clock0
    local secPerPage = (st.pagesDone > 0 and elapsed / st.pagesDone) or db.secPerPage
    if not secPerPage then
        return nil
    end
    local fallback = st.okItems > 0 and st.okPages / st.okItems or 1
    local pagesLeft = 0
    if st.item then
        local expected = expectedPages(st, st.item, st.item.entry) or fallback
        pagesLeft = math.max(expected - st.item.pagesRead, 1)
    end
    for i = st.index + 1, #st.entries do
        local expected = expectedPages(st, nil, st.entries[i])
        if not expected and requireHistory then
            return nil
        end
        pagesLeft = pagesLeft + (expected or fallback)
    end
    return pagesLeft * secPerPage, pagesLeft, secPerPage
end

function Scan:Progress()
    local st = self.state
    if not st or not st.item then
        return nil
    end
    local item = st.item
    local expected = expectedPages(st, item, item.entry)
    local page = expected and string.format("page %d/%d", item.pagesRead + 1, expected)
        or string.format("page %d", item.pagesRead + 1)
    local eta, _, secPerPage = self:Estimate(st)
    return string.format("scan running: item %d/%d %s, %s, %s elapsed, %s", st.index, #st.entries,
        item.entry.name, page, ns.FormatDuration(ns.Clock() - st.clock0),
        eta and string.format("~%s left (%.1fs/page)", ns.FormatDuration(eta), secPerPage) or "no estimate yet")
end

function Scan:Finish(st, status)
    if st.probe and not st.lingering then
        -- Keep listening briefly so late or duplicate events show up in the log.
        st.lingering = true
        st.waiting = nil
        self:Log(st, "done (%s); listening %ds for stray events", status, PROBE_LINGER)
        C_Timer.After(PROBE_LINGER, function()
            if self.state == st then
                self:Finish(st, status)
            end
        end)
        return
    end
    for _, event in ipairs(st.adapter.events) do
        ns.UnregisterEvent(event)
    end
    self.state = nil
    local db = ns.InitDB()
    local elapsed = ns.Clock() - st.clock0

    if st.probe then
        return self:SaveProbe(st, db, status)
    end
    if st.pagesDone > 0 then
        db.secPerPage = elapsed / st.pagesDone
    end
    if #st.items == 0 then
        return
    end
    local realm = GetRealmName()
    local faction = UnitFactionGroup("player")
    local failed = #st.items - st.okItems
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
    ns.Print(string.format("scan %s: %d/%d item(s) stored (%d not ok), %d page(s) in %s%s; /reload or log out to save",
        status, #st.items, #st.entries, failed, st.pagesDone, ns.FormatDuration(elapsed),
        st.pagesDone > 0 and string.format(" (%.1fs/page)", elapsed / st.pagesDone) or ""))
end

function Scan:SaveProbe(st, db, status)
    local item = st.items[1]
    table.insert(db.probes, {
        addonVersion = ns.VERSION,
        api = st.adapter.name,
        startedAt = st.startedAt,
        status = status,
        target = st.entries[1].name,
        itemStatus = item and item.status,
        pages = item and item.pages,
        listingsRead = item and item.listingsRead,
        reportedListings = item and item.reportedListings,
        log = st.log,
    })
    while #db.probes > MAX_PROBES do
        table.remove(db.probes, 1)
    end
    ns.Print(string.format("probe %s: %s, %d page(s), %d listing(s) read, reported %s; %d log lines saved, /reload to write",
        status, item and item.status or "no result", item and item.pages or 0, item and item.listingsRead or 0,
        tostring(item and item.reportedListings), #st.log))
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
    self:Log(st, "aborted: %s", reason)
    st.lingering = true -- no point waiting for stray events after an abort
    self:Finish(st, "aborted")
end
