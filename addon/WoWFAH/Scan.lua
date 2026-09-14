local _, ns = ...

local Scan = {}
ns.Scan = Scan

local CHUNK_SIZE = 1000      -- rows read per frame
local MAX_RETRY_PASSES = 5   -- re-reads for rows whose item data wasn't cached
local RETRY_DELAY = 1.0      -- seconds between retry passes
local QUERY_TIMEOUT = 300    -- seconds to wait for the snapshot to arrive

function Scan:IsRunning()
    return self.state ~= nil
end

function Scan:Start()
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
    local ok, reason = adapter:CanQueryAll()
    if not ok then
        return ns.Print(reason)
    end

    local st = {
        adapter = adapter,
        phase = "query",
        startedAt = ns.Now(),
        rows = {},
        pending = {},
        pass = 0,
    }
    self.state = st

    ns.RegisterEvent(adapter.listEvent, function()
        self:OnListReady(st)
    end)
    C_Timer.After(QUERY_TIMEOUT, function()
        if self.state == st and st.phase == "query" then
            self:Abort("timed out waiting for auction data")
        end
    end)
    adapter:QueryAll()
    ns.Print(string.format("full scan requested (%s API); the client may freeze briefly", adapter.name))
end

function Scan:OnListReady(st)
    if self.state ~= st or st.phase ~= "query" then
        return
    end
    ns.UnregisterEvent(st.adapter.listEvent)
    st.phase = "read"
    st.total = st.adapter:GetNumItems()
    st.cursor = 1
    self:ReadChunk(st)
end

function Scan:ReadChunk(st)
    if self.state ~= st then
        return
    end
    local last = math.min(st.cursor + CHUNK_SIZE - 1, st.total)
    for i = st.cursor, last do
        local row, complete = st.adapter:ReadRow(i)
        st.rows[i] = row
        if not complete then
            st.pending[#st.pending + 1] = i
        end
    end
    st.cursor = last + 1
    if st.cursor <= st.total then
        C_Timer.After(0, function()
            self:ReadChunk(st)
        end)
    else
        self:RetryPending(st)
    end
end

function Scan:RetryPending(st)
    if #st.pending == 0 or st.pass >= MAX_RETRY_PASSES then
        return self:Finish(st)
    end
    st.pass = st.pass + 1
    st.phase = "retry"
    C_Timer.After(RETRY_DELAY, function()
        if self.state ~= st then
            return
        end
        local stillPending = {}
        for _, i in ipairs(st.pending) do
            local row, complete = st.adapter:ReadRow(i)
            if row then
                st.rows[i] = row
            end
            if not complete then
                stillPending[#stillPending + 1] = i
            end
        end
        st.pending = stillPending
        self:RetryPending(st)
    end)
end

function Scan:Finish(st)
    local packed = {}
    for i = 1, st.total do
        local row = st.rows[i]
        if row then
            packed[#packed + 1] = row
        end
    end
    local realm = GetRealmName()
    local faction = UnitFactionGroup("player")
    local db = ns.InitDB()
    local scan = {
        scanId = string.format("%s-%s-%d", realm, faction, st.startedAt),
        addonVersion = ns.VERSION,
        api = st.adapter.name,
        realm = realm,
        faction = faction,
        startedAt = st.startedAt,
        finishedAt = ns.Now(),
        listed = st.total,
        rowCount = #packed,
        incomplete = #st.pending,
        rowFormat = ns.ROW_FORMAT,
        rows = packed,
    }
    db.scans[#db.scans + 1] = scan
    self.state = nil
    ns.Print(string.format("scan complete: %d auctions stored (%d incomplete); /reload or log out to save",
        scan.rowCount, scan.incomplete))
end

function Scan:Abort(reason)
    local st = self.state
    if not st then
        return
    end
    if st.phase == "query" then
        ns.UnregisterEvent(st.adapter.listEvent)
    end
    self.state = nil
    ns.Print("scan aborted: " .. reason)
end
