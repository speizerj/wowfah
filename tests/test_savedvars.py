from __future__ import annotations

import pytest

from wowfah import savedvars

WOW_STYLE = """
WoWFAH_DB = {
["schemaVersion"] = 1,
["scans"] = {
{
["realm"] = "Pyrewood Village",
["rows"] = {
"2589\\titem:2589\\tLinen Cloth", -- [1]
}, -- [2]
}, -- [1]
},
}
OtherVar = nil
"""


def test_parses_client_written_file():
    data = savedvars.loads(WOW_STYLE)
    assert data == {
        "WoWFAH_DB": {
            "schemaVersion": 1,
            "scans": [{"realm": "Pyrewood Village", "rows": ["2589\titem:2589\tLinen Cloth"]}],
        }
    }


def test_round_trip_preserves_values():
    value = {
        "Var": {
            "str": 'quote " backslash \\ newline \n tab \t',
            "unicode": "Zul'Gurub — Ænima",
            "int": 12345678901,
            "neg": -3,
            "float": 1.5,
            "bools": [True, False],
            "nested": [[1, 2], {"k": "v"}],
            5: "int key",
        }
    }
    assert savedvars.loads(savedvars.dumps(value)) == value


def test_sparse_or_mixed_tables_become_dicts():
    data = savedvars.loads("X = {[1] = 'a', [3] = 'c'}\nY = {'a', k = 1}")
    assert data["X"] == {1: "a", 3: "c"}
    assert data["Y"] == {1: "a", "k": 1}


def test_file_has_no_access_to_lua_stdlib():
    with pytest.raises(Exception):
        savedvars.loads("X = os.time()")
    with pytest.raises(Exception):
        savedvars.loads("X = io.open('/etc/passwd')")


def test_syntax_error_raises():
    with pytest.raises(Exception):
        savedvars.loads("X = {")


def test_dumps_rejects_non_finite():
    with pytest.raises(ValueError):
        savedvars.dumps({"X": float("nan")})
