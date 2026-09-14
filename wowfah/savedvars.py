"""Read and write WoW SavedVariables files.

Reading executes the file as Lua inside an empty environment (no stdlib, no
globals), so a SavedVariables file can only define data.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import lupa
from lupa import LuaRuntime

_LOADER = """
function(src, chunkname)
  local env = {}
  local chunk, err = load(src, chunkname, "t", env)
  if not chunk then error(err, 0) end
  chunk()
  return env
end
"""


def new_runtime() -> LuaRuntime:
    return LuaRuntime(unpack_returned_tuples=True, register_eval=False, register_builtins=False)


def lua_to_python(value: Any) -> Any:
    """Convert Lua tables to lists (keys exactly 1..n) or dicts, recursively."""
    if lupa.lua_type(value) != "table":
        return value
    items = list(value.items())
    n = len(items)
    if n and all(type(k) is int for k, _ in items) and {k for k, _ in items} == set(range(1, n + 1)):
        by_key = dict(items)
        return [lua_to_python(by_key[i]) for i in range(1, n + 1)]
    return {k: lua_to_python(v) for k, v in items}


def loads(text: str, chunkname: str = "=SavedVariables") -> dict[str, Any]:
    lua = new_runtime()
    env = lua.eval(_LOADER)(text, chunkname)
    return lua_to_python(env) or {}


def load(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_bytes().decode("utf-8", errors="replace")
    return loads(text, f"@{path.name}")


def _quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot serialize non-finite number {value!r}")
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _key(key: Any) -> str:
    if isinstance(key, str):
        return f"[{_quote(key)}]"
    if isinstance(key, int) and not isinstance(key, bool):
        return f"[{key}]"
    raise TypeError(f"unsupported table key {key!r}")


def _write(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        out.append("{\n")
        for k, v in value.items():
            out.append(f"{_key(k)} = ")
            _write(v, out)
            out.append(",\n")
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("{\n")
        for i, v in enumerate(value, start=1):
            _write(v, out)
            out.append(f", -- [{i}]\n")
        out.append("}")
    else:
        out.append(_scalar(value))


def dumps(variables: dict[str, Any]) -> str:
    """Serialize top-level variables in the same layout the WoW client writes."""
    out: list[str] = []
    for name, value in variables.items():
        if not name.isidentifier():
            raise ValueError(f"invalid variable name {name!r}")
        out.append(f"\n{name} = ")
        _write(value, out)
        out.append("\n")
    return "".join(out)


def dump(variables: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(dumps(variables), encoding="utf-8")
