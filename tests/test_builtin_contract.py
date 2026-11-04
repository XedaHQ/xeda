"""Check the built-in flows and stable behavior against their release baseline."""

import ast
import copy
import json
import re

import pytest

from tests.test_public_api import tool
from .source_layout import IMPL_DIR

pytestmark = pytest.mark.python_compat


def test_the_builtin_contract_is_compatible_with_the_newest_baseline() -> None:
    baseline = json.loads((tool.newest_baseline() / "builtin.json").read_text())
    assert not tool.classify(baseline, tool.builtin_contract(), kind="contract")[1]


def test_the_frozen_rules_are_compatible_with_the_newest_baseline() -> None:
    baseline = json.loads((tool.newest_baseline() / "behavior.json").read_text())
    assert not tool.classify(baseline, tool.behavior_snapshot(), kind="behavior")[1]


def test_a_changed_unit_is_breaking() -> None:
    old = {
        "flows": {
            "a": {
                "settings": {"clock": {"type": "time", "unit": "ns", "default": None}},
                "outputs": {},
                "metrics": {},
                "artifacts": [],
            }
        },
        "codes": {},
    }
    new = copy.deepcopy(old)
    new["flows"]["a"]["settings"]["clock"]["unit"] = "ps"
    assert tool.classify(old, new, kind="contract")[1]


def _flow(*, experimental: bool = False, **settings: dict) -> dict:
    return {
        **({"experimental": True} if experimental else {}),
        "settings": settings,
        "outputs": {},
        "metrics": {"m": {"type": "number", "unit": None}},
        "artifacts": ["log"],
    }


def _contract(**flows: dict) -> dict:
    return {"flows": flows, "codes": {}}


def _group(default: dict, *, marked: bool = False) -> dict:
    """A group `g` with the leaf `g.x`, which `marked` marks, and the sibling `g.y`."""
    leaf = {"type": "boolean", "unit": None, "default": default["x"]}
    return {
        "g": {"type": "G", "unit": None, "default": default},
        "g.x": leaf | ({"experimental": True} if marked else {}),
        "g.y": {"type": "boolean", "unit": None, "default": default["y"]},
    }


def test_nothing_of_a_flow_marked_experimental_in_the_old_baseline_is_judged() -> None:
    old = _contract(a=_flow(experimental=True, s={"type": "integer", "unit": None, "default": 1}))
    new = _contract(a=_flow(experimental=True))
    new["flows"]["a"]["metrics"] = {}
    new["flows"]["a"]["artifacts"] = []
    assert tool.classify(old, new, kind="contract") == ([], [])
    assert tool.classify(old, _contract(), kind="contract") == ([], [])  # the flow is gone


def test_a_flow_marked_experimental_only_in_the_new_contract_is_still_judged() -> None:
    old = _contract(a=_flow(s={"type": "integer", "unit": None, "default": 1}))
    new = _contract(a=_flow(experimental=True))
    additive, breaking = tool.classify(old, new, kind="contract")
    assert "flows.a: newly marked experimental" in breaking
    assert "a.settings.s: settings removed" in breaking
    assert not additive


def test_a_removed_flow_mark_is_additive() -> None:
    old = _contract(a=_flow(experimental=True))
    assert tool.classify(old, _contract(a=_flow()), kind="contract") == (
        ["flows.a: no longer experimental"],
        [],
    )


def test_a_marked_setting_is_not_judged_but_its_siblings_and_group_are() -> None:
    old = _contract(a=_flow(**_group({"x": True, "y": True}, marked=True)))
    # The marked leaf changes its default, then leaves, in the group default too.
    moved = _group({"x": False, "y": True}, marked=True)
    assert tool.classify(old, _contract(a=_flow(**moved)), kind="contract") == ([], [])
    del moved["g.x"]
    assert tool.classify(old, _contract(a=_flow(**moved)), kind="contract") == ([], [])
    changed = _contract(a=_flow(**_group({"x": True, "y": False}, marked=True)))
    breaking = tool.classify(old, changed, kind="contract")[1]
    assert any(m.startswith("a.settings.g.y: changed") for m in breaking)
    assert any(m.startswith("a.settings.g: changed") for m in breaking)


def test_a_setting_marked_only_in_the_new_contract_is_still_judged() -> None:
    old = _contract(a=_flow(**_group({"x": True, "y": True})))
    new = _contract(a=_flow(**_group({"x": False, "y": True}, marked=True)))
    breaking = tool.classify(old, new, kind="contract")[1]
    assert "a.settings.g.x: newly marked experimental" in breaking
    assert any(m.startswith("a.settings.g.x: changed") for m in breaking)
    assert any(m.startswith("a.settings.g: changed") for m in breaking)


def test_a_removed_setting_mark_is_additive() -> None:
    old = _contract(a=_flow(**_group({"x": True, "y": True}, marked=True)))
    new = _contract(a=_flow(**_group({"x": True, "y": True})))
    assert tool.classify(old, new, kind="contract") == (
        ["a.settings.g.x: no longer experimental"],
        [],
    )


def test_the_builtin_contract_records_the_marks() -> None:
    flows = tool.builtin_contract()["flows"]
    assert {n for n, f in flows.items() if f.get("experimental")} == {"xezim", "vcs", "dc"}
    marked = [k for k, v in flows["openroad_place"]["settings"].items() if v.get("experimental")]
    assert marked == ["global_placement.timing_driven.repair_timing"]


def test_a_new_code_is_additive_and_a_removed_code_breaking() -> None:
    old = {"flows": {}, "codes": {"node": ["old"]}}
    new = {"flows": {}, "codes": {"node": ["new"]}}
    additive, breaking = tool.classify(old, new, kind="contract")
    assert additive and breaking


def test_the_code_inventory_exists() -> None:
    from xeda._internal.flow_runner.error_codes import ALL_CODES

    assert "internal_error" in ALL_CODES["node"]


def test_the_session_variables_are_exactly_the_contract_list() -> None:
    from xeda._internal.generation import SESSION_VARIABLES

    assert SESSION_VARIABLES == frozenset(
        {
            "PWD",
            "OLDPWD",
            "SHLVL",
            "_",
            "COLUMNS",
            "LINES",
            "TERM_SESSION_ID",
            "ITERM_SESSION_ID",
            "WINDOWID",
            "TMUX",
            "TMUX_PANE",
            "STY",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "SSH_CLIENT",
            "SSH_CONNECTION",
            "SSH_TTY",
            "XDG_SESSION_ID",
            "XEDA_RUN_ROOT",
            "XEDA_REMOTE",
            "XEDA_LOG_LEVEL",
            "XEDA_DETAILED_LOGS",
        }
    )


@pytest.mark.parametrize(
    "text, ok",
    [
        ("a", True),
        ("a" * 64, True),
        ("a1_b2", True),
        ("_a", False),
        ("1a", False),
        ("A", False),
        ("a-b", False),
        ("", False),
    ],
)
def test_the_flow_and_output_name_grammar_boundaries(text: str, ok: bool) -> None:
    from xeda._internal.flow.flow import FLOW_NAME
    from xeda._internal.flow_runner.chains import OUTPUT_NAME

    assert bool(FLOW_NAME.fullmatch(text)) == ok
    assert bool(OUTPUT_NAME.fullmatch(text)) == ok


@pytest.mark.parametrize(
    "path, ok",
    [
        ("nextpnr.fmax", True),
        ("a.b.c_d", True),
        ("a." + "b" * 60, True),
        ("a..b", False),
        ("a.B", False),
        (".a", False),
    ],
)
def test_the_metric_path_grammar_boundaries(path: str, ok: bool) -> None:
    from xeda._internal.flow.metrics import METRIC_PATH

    assert bool(METRIC_PATH.fullmatch(path)) == ok


def test_the_private_files_have_their_names_places_and_keys() -> None:
    from xeda._internal.local_settings import LOCAL_FILES

    assert LOCAL_FILES == (
        ("config.yaml", "$XDG_CONFIG_HOME/xeda", ("flows", "env", "run_root")),
        ("<stem>.local.yaml", "beside the project file", ("flows", "env", "run_root")),
    )


def _literal_values(node: ast.expr) -> set[str]:
    """The texts that an expression can evaluate to: a literal, or a branch of `or` or `if`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return {node.value}
    if isinstance(node, ast.BoolOp):
        return set().union(*map(_literal_values, node.values))
    if isinstance(node, ast.IfExp):
        return _literal_values(node.body) | _literal_values(node.orelse)
    return set()


def _is_code_table(node: ast.Assign) -> bool:
    """A module table of codes, such as `_MISSING_FILE_CODES`, that code reads to pick a code."""
    return any(
        isinstance(t, ast.Name) and t.id.endswith("_CODES") and t.id != "ALL_CODES"
        for t in node.targets
    )


def _table_codes(node: ast.expr) -> set[str]:
    """The snake-case texts of a table: the values of a mapping, or the members of a collection."""
    parts = node.values if isinstance(node, ast.Dict) else [node]
    return {
        c.value
        for part in parts
        if part is not None
        for c in ast.walk(part)
        if isinstance(c, ast.Constant)
        and isinstance(c.value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]*", c.value)
    }


def test_all_literal_codes_are_in_the_inventory() -> None:
    from xeda._internal.flow_runner.error_codes import ALL_CODES, _SETTINGS_CODES
    from xeda._internal import records
    from xeda._internal.flow_runner import error_codes

    found = set(_SETTINGS_CODES.values())
    found.update(
        getattr(records, name)
        for name in records.__all__
        if name.isupper() and isinstance(getattr(records, name), str)
    )
    found.update(
        value
        for name, value in vars(error_codes).items()
        if name.isupper() and isinstance(value, str) and value.islower()
    )
    for path in IMPL_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for k in node.keywords:
                    if k.arg == "code":
                        found.update(_literal_values(k.value))
            elif isinstance(node, ast.Assign) and _is_code_table(node):
                found.update(_table_codes(node.value))
    assert found <= set().union(*ALL_CODES.values())


@pytest.mark.parametrize(
    "row, expected",
    [
        pytest.param(
            "node",
            {
                "nonzero_exit",
                "timeout",
                "executable_not_found",
                "reported_failure",
                "missing_report",
                "missing_output",
                "invalid_metric",
                "rejected",
                "producer_failed",
                "canceled",
                "storage_error",
                "internal_error",
                "missing_generated",
                "flow_not_importable",
            },
        ),
        pytest.param(
            "launch",
            {
                "generator_failed",
                "delivery_failed",
                "remote_failed",
                "remote_fetch_failed",
                "remote_detached",
                "canceled",
                "no_successful_run",
                "cleanup_failed",
            },
        ),
        pytest.param(
            "DesignError",
            {
                "design_file_not_found",
                "invalid_design",
                "unknown_target",
                "target_required",
                "removed_alias",
                "removed_key",
                "fetch_failed",
                "dependency_cycle",
            },
        ),
    ],
)
def test_error_codes_match_the_published_list(row: str, expected: set[str]) -> None:
    from xeda._internal.flow_runner.error_codes import ALL_CODES

    assert ALL_CODES[row] == expected


def test_a_new_cli_command_is_additive() -> None:
    old = {"cli": {"xeda": {}}}
    new = {"cli": {"xeda": {}, "xeda new": {}}}
    additive, breaking = tool.classify(old, new, kind="behavior")
    assert additive and not breaking


def test_a_new_required_setting_is_breaking() -> None:
    old = {
        "flows": {"a": {"settings": {}, "outputs": {}, "metrics": {}, "artifacts": []}},
        "codes": {},
    }
    new = copy.deepcopy(old)
    new["flows"]["a"]["settings"]["required"] = {"type": "integer", "unit": None}
    assert tool.classify(old, new, kind="contract")[1]


def test_the_listings_mark_only_the_experimental_entries() -> None:
    from xeda.introspect import flow_info, settings_info

    assert flow_info("xezim")["experimental"] is True
    assert "experimental" not in flow_info("ghdl_sim")
    fields = settings_info("openroad_place")["fields"]
    assert [f["name"] for f in fields if f.get("experimental")] == [
        "global_placement.timing_driven.repair_timing"
    ]
