"""Loads the real addon files into lupa on top of tests/wow_mock.lua."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wowfah.savedvars import lua_to_python, new_runtime

ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = ROOT / "addon" / "WoWFAH"
MOCK = Path(__file__).with_name("wow_mock.lua")

_LOAD_FILE = """
function(src, chunkname, addonName, ns)
  local chunk = assert(load(src, chunkname))
  chunk(addonName, ns)
end
"""


def toc_files(toc: Path) -> list[str]:
    files = []
    for line in toc.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            files.append(line)
    return files


class WowClient:
    def __init__(self, flavor: str = "classic") -> None:
        self.lua = new_runtime()
        self.lua.execute(MOCK.read_text())
        self.mock = self.lua.globals().WOWMOCK
        {"classic": self.mock.installClassic, "modern": self.mock.installModern}[flavor]()

    def load_addon(self, saved: dict[str, Any] | None = None) -> None:
        if saved is not None:
            self.lua.globals().WoWFAH_DB = self.to_lua(saved)
        self.ns = self.lua.table()
        loader = self.lua.eval(_LOAD_FILE)
        for name in toc_files(ADDON_DIR / "WoWFAH.toc"):
            loader((ADDON_DIR / name).read_text(), f"@{name}", "WoWFAH", self.ns)
        self.fire("ADDON_LOADED", "WoWFAH")

    def to_lua(self, value: Any) -> Any:
        if isinstance(value, dict):
            t = self.lua.table()
            for k, v in value.items():
                t[k] = self.to_lua(v)
            return t
        if isinstance(value, list):
            t = self.lua.table()
            for i, v in enumerate(value, start=1):
                t[i] = self.to_lua(v)
            return t
        return value

    def set_auctions(self, auctions: list[dict[str, Any]]) -> None:
        self.mock.auctions = self.to_lua(auctions)

    def set_watchlist(self, entries: list[tuple[int, str, str]]) -> None:
        self.ns.WATCHLIST = self.to_lua([list(e) for e in entries])

    def fire(self, event: str, *args: Any) -> None:
        self.mock.fire(event, *args)

    def slash(self, msg: str) -> None:
        self.lua.globals().SlashCmdList.WOWFAH(msg)

    def run_timers(self, max_steps: int = 100_000) -> int:
        return self.mock.runTimers(max_steps)

    @property
    def messages(self) -> list[str]:
        return lua_to_python(self.mock.messages) or []

    @property
    def db(self) -> dict[str, Any]:
        return lua_to_python(self.lua.globals().WoWFAH_DB)
