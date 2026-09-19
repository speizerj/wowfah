local _, ns = ...

-- A small panel next to the auction house window: Scan, Abort, Save & reload, and a
-- status line. Everything it does is also available as a /wowfah command.
local UI = {}
ns.UI = UI

local WIDTH, HEIGHT = 280, 78

local function makeButton(parent, text, width, onClick)
    local b = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
    b:SetSize(width, 22)
    b:SetText(text)
    b:SetScript("OnClick", onClick)
    return b
end

function UI:Create(anchor)
    local f = CreateFrame("Frame", "WoWFAHPanel", anchor or UIParent, "BackdropTemplate")
    f:SetSize(WIDTH, HEIGHT)
    if anchor then
        f:SetPoint("TOPLEFT", anchor, "TOPRIGHT", 4, 0)
    else
        f:SetPoint("CENTER")
    end
    if f.SetBackdrop then
        f:SetBackdrop({
            bgFile = "Interface\\Tooltips\\UI-Tooltip-Background",
            edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
            tile = true, tileSize = 16, edgeSize = 16,
            insets = { left = 4, right = 4, top = 4, bottom = 4 },
        })
        f:SetBackdropColor(0, 0, 0, 0.85)
    end

    local title = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    title:SetPoint("TOPLEFT", 10, -8)
    title:SetText("WoWFAH")

    local status = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    status:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -4)
    status:SetPoint("RIGHT", f, "RIGHT", -10, 0)
    status:SetJustifyH("LEFT")

    self.scan = makeButton(f, "Scan", 70, function() ns.RunCommand("scan", "") end)
    self.abort = makeButton(f, "Abort", 70, function() ns.RunCommand("abort", "") end)
    -- Scans only reach disk (and `wowfah watch`) when the game saves SavedVariables.
    self.save = makeButton(f, "Save & reload", 110, function() ReloadUI() end)
    self.scan:SetPoint("BOTTOMLEFT", 8, 8)
    self.abort:SetPoint("LEFT", self.scan, "RIGHT", 4, 0)
    self.save:SetPoint("LEFT", self.abort, "RIGHT", 4, 0)

    self.frame, self.status = f, status
end

-- Called on AUCTION_HOUSE_SHOW; builds the panel the first time.
function UI:Attach()
    if not self.frame then
        -- Modern client: AuctionHouseFrame; Classic: AuctionFrame.
        self:Create(AuctionHouseFrame or AuctionFrame)
    end
    self.frame:Show()
    self:Refresh()
end

function UI:StatusText()
    if ns.Scan:IsRunning() then
        return ns.Scan:Progress() or "scan starting..."
    end
    local lines = { ns.lastResult or "ready: open the AH and press Scan" }
    if (ns.unsavedScans or 0) > 0 then
        lines[#lines + 1] = string.format("|cffffd100%d scan(s) not saved yet: press Save & reload|r", ns.unsavedScans)
    end
    return table.concat(lines, "\n")
end

function UI:Refresh()
    if not self.frame then
        return
    end
    local running = ns.Scan:IsRunning()
    self.scan:SetEnabled(not running and ns.ahOpen)
    self.abort:SetEnabled(running)
    self.save:SetEnabled(not running)
    self.status:SetText(self:StatusText())
end
