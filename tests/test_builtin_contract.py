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
    assert breaking.count("a.settings.g.y: default changed from True to False") == 2  # leaf, group


def test_a_setting_marked_only_in_the_new_contract_is_still_judged() -> None:
    old = _contract(a=_flow(**_group({"x": True, "y": True})))
    new = _contract(a=_flow(**_group({"x": False, "y": True}, marked=True)))
    breaking = tool.classify(old, new, kind="contract")[1]
    assert "a.settings.g.x: newly marked experimental" in breaking
    assert breaking.count("a.settings.g.x: default changed from True to False") == 2


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
    old = {"cli": {"xeda": _command()}}
    new = {"cli": {"xeda": _command(), "xeda new": _command()}}
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


@pytest.fixture(scope="module")
def current() -> dict:
    return tool.builtin_contract()


def _judged(old: dict, mutate, kind: str = "contract") -> tuple[list[str], list[str]]:
    new = copy.deepcopy(old)
    mutate(new)
    return tool.classify(old, new, kind=kind)


def _option_schema(contract: dict, flow: str, setting: str) -> dict:
    return contract["flows"][flow]["settings"][setting]["schema"]


def test_the_snapshot_of_the_current_contract_has_no_difference(current: dict) -> None:
    assert tool.classify(current, copy.deepcopy(current), kind="contract") == ([], [])


def test_the_snapshot_records_definitions_inputs_and_required_settings(current: dict) -> None:
    flow = current["flows"]["openroad_place"]
    assert "PhysicalClock" in flow["$defs"]
    assert flow["required_settings"] == ["platform"]
    assert flow["inputs"]["netlist"] == {
        "kinds": ["VerilogNetlist"],
        "cardinality": "one",
        "required": True,
    }
    assert "schema" not in flow["settings"]["global_placement.timing_driven"]
    assert "schema" in flow["settings"]["global_placement.timing_driven.enabled"]


def test_a_removed_choice_of_a_setting_breaks_and_an_added_one_is_additive(current: dict) -> None:
    def remove(c: dict) -> None:
        _option_schema(c, "quartus", "final_placement_optimization")["enum"].pop()

    def add(c: dict) -> None:
        _option_schema(c, "quartus", "final_placement_optimization")["enum"].append("new-choice")

    assert _judged(current, remove)[1]
    additive, breaking = _judged(current, add)
    assert additive and not breaking


def _clock(c: dict) -> dict:
    return c["flows"]["openroad_place"]["$defs"]["PhysicalClock"]


def test_a_new_optional_field_of_a_nested_model_is_additive(current: dict) -> None:
    def optional(c: dict) -> None:
        _clock(c)["properties"]["extra"] = {"type": "integer"}

    additive, breaking = _judged(current, optional)
    assert additive and not breaking


def test_a_new_required_field_of_a_nested_model_breaks(current: dict) -> None:
    def required(c: dict) -> None:
        _clock(c)["properties"]["extra"] = {"type": "integer"}
        _clock(c)["required"] = [*_clock(c).get("required", []), "extra"]

    assert any("extra" in m for m in _judged(current, required)[1])


def test_a_removed_field_of_a_nested_model_breaks(current: dict) -> None:
    def remove(c: dict) -> None:
        del _clock(c)["properties"]["period"]

    assert _judged(current, remove)[1]


def test_a_new_group_leaf_is_additive_even_though_the_group_default_gains_a_key(
    current: dict,
) -> None:
    def add_leaf(c: dict) -> None:
        settings = c["flows"]["openroad_place"]["settings"]
        settings["global_placement.timing_driven.new_leaf"] = {
            **settings["global_placement.timing_driven.enabled"],
            "default": 1,
        }
        settings["global_placement.timing_driven"]["default"]["new_leaf"] = 1

    additive, breaking = _judged(current, add_leaf)
    assert additive and not breaking


def test_a_removed_key_of_a_group_default_breaks_and_a_changed_value_breaks(current: dict) -> None:
    key = "global_placement.timing_driven"

    def drop(c: dict) -> None:
        del c["flows"]["openroad_place"]["settings"][key]["default"]["enabled"]

    def change(c: dict) -> None:
        c["flows"]["openroad_place"]["settings"][key]["default"]["virtual_cts"] = True

    assert _judged(current, drop)[1]
    assert _judged(current, change)[1]


def test_a_changed_nested_default_of_a_definition_breaks(current: dict) -> None:
    def change(c: dict) -> None:
        for definition in c["flows"]["openroad_place"]["$defs"].values():
            for schema in definition.get("properties", {}).values():
                if "default" in schema:
                    schema["default"] = "changed"
                    return
        raise AssertionError("no definition has a default")

    assert _judged(current, change)[1]


def test_a_changed_display_type_is_not_judged(current: dict) -> None:
    def retype(c: dict) -> None:
        c["flows"]["quartus"]["settings"]["final_placement_optimization"]["type"] = "other text"

    assert _judged(current, retype) == ([], [])


def test_a_change_under_an_experimental_setting_is_not_judged(current: dict) -> None:
    leaf = "global_placement.timing_driven.repair_timing"
    assert current["flows"]["openroad_place"]["settings"][leaf]["experimental"]

    def change(c: dict) -> None:
        settings = c["flows"]["openroad_place"]["settings"]
        settings[leaf]["schema"]["type"] = "string"
        settings[leaf]["default"] = True
        settings["global_placement.timing_driven"]["default"]["repair_timing"] = True

    assert _judged(current, change) == ([], [])


def test_a_new_required_setting_name_breaks_and_a_removed_one_is_additive(current: dict) -> None:
    def add(c: dict) -> None:
        c["flows"]["openroad_place"]["required_settings"].append("extra")

    def remove(c: dict) -> None:
        c["flows"]["openroad_place"]["required_settings"].remove("platform")

    assert _judged(current, add)[1]
    additive, breaking = _judged(current, remove)
    assert additive and not breaking


def test_a_setting_that_gains_a_default_is_additive_and_one_that_loses_it_breaks() -> None:
    required = _contract(a=_flow(s={"type": "integer", "unit": None}))
    optional = _contract(a=_flow(s={"type": "integer", "unit": None, "default": 1}))
    additive, breaking = tool.classify(required, optional, kind="contract")
    assert additive and not breaking
    assert tool.classify(optional, required, kind="contract")[1]


def _inputs(c: dict) -> dict:
    return c["flows"]["openroad_place"]["inputs"]


def test_declared_inputs_follow_the_direction_of_a_user(current: dict) -> None:
    def remove(c: dict) -> None:
        del _inputs(c)["netlist"]

    def new_required(c: dict) -> None:
        _inputs(c)["extra"] = {"kinds": ["Sdc"], "cardinality": "one", "required": True}

    def new_optional(c: dict) -> None:
        _inputs(c)["extra"] = {"kinds": ["Sdc"], "cardinality": "optional", "required": False}

    def drop_kind(c: dict) -> None:
        _inputs(c)["netlist"]["kinds"] = []

    def add_kind(c: dict) -> None:
        _inputs(c)["netlist"]["kinds"].append("Sdc")

    def optional(c: dict) -> None:
        _inputs(c)["netlist"].update(cardinality="optional", required=False)

    assert _judged(current, remove)[1]
    assert _judged(current, new_required)[1]
    assert _judged(current, new_optional)[0] and not _judged(current, new_optional)[1]
    assert _judged(current, drop_kind)[1]
    assert _judged(current, add_kind)[0] and not _judged(current, add_kind)[1]
    assert _judged(current, optional)[0] and not _judged(current, optional)[1]
    relaxed = copy.deepcopy(current)
    optional(relaxed)
    assert tool.classify(relaxed, current, kind="contract")[1]


def test_a_changed_unit_of_a_real_setting_breaks(current: dict) -> None:
    flow, key = next(
        (n, k) for n, f in current["flows"].items() for k, v in f["settings"].items() if v["unit"]
    )

    def change(c: dict) -> None:
        c["flows"][flow]["settings"][key]["unit"] = "other"

    assert _judged(current, change)[1]


def _command(options: dict | None = None, arguments: list | None = None) -> dict:
    return {"options": options or {}, "arguments": arguments or []}


def _option(**changes: object) -> dict:
    return {
        "names": ["--mode", "-m"],
        "flag": False,
        "hidden": False,
        "multiple": False,
        "default": None,
        "type": "choice",
        "required": False,
        "nargs": 1,
        "choices": ["a", "b"],
        "case_sensitive": True,
        "environment": ["XEDA_MODE"],
        **changes,
    }


def _cli(*options: dict, arguments: list | None = None) -> dict:
    return {"cli": {"xeda x": _command({f"o{i}": o for i, o in enumerate(options)}, arguments)}}


@pytest.mark.parametrize(
    ("change", "breaks"),
    [
        ({"choices": ["a"]}, True),
        ({"choices": ["a", "b", "c"]}, False),
        ({"names": ["--mode"]}, True),
        ({"names": ["--mode", "-m", "--kind"]}, False),
        ({"environment": []}, True),
        ({"environment": ["XEDA_MODE", "XEDA_OTHER"]}, False),
        ({"required": True}, True),
        ({"case_sensitive": False}, True),
        ({"nargs": 2}, True),
        ({"type": "text"}, True),
        ({"flag": True}, True),
        ({"multiple": True}, True),
        ({"default": "a"}, True),
        ({"hidden": True}, None),
    ],
)
def test_option_rules(change: dict, breaks: bool | None) -> None:
    additive, breaking = tool.classify(_cli(_option()), _cli(_option(**change)), kind="behavior")
    assert bool(breaking) == bool(breaks)
    assert bool(additive) == (breaks is False)


def test_an_option_that_becomes_optional_is_additive() -> None:
    additive, breaking = tool.classify(
        _cli(_option(required=True)), _cli(_option()), kind="behavior"
    )
    assert additive and not breaking


def test_a_new_required_option_breaks() -> None:
    assert tool.classify(_cli(), _cli(_option(required=True)), kind="behavior")[1]
    assert tool.classify(_cli(), _cli(_option()), kind="behavior")[0]


def test_a_range_that_widens_is_additive_and_one_that_narrows_breaks() -> None:
    def ranged(low: int | None, high: int | None, is_open: bool = False) -> dict:
        return _option(type="integer range", choices=None, range=[low, high, is_open, False])

    wide, narrow = _cli(ranged(0, 10)), _cli(ranged(1, 10))
    assert tool.classify(narrow, wide, kind="behavior")[0]
    assert not tool.classify(narrow, wide, kind="behavior")[1]
    assert tool.classify(wide, narrow, kind="behavior")[1]
    assert tool.classify(wide, _cli(ranged(0, 5)), kind="behavior")[1]
    assert tool.classify(wide, _cli(ranged(0, 10, True)), kind="behavior")[1]
    assert not tool.classify(wide, _cli(ranged(None, None)), kind="behavior")[1]


def _argument(name: str, *, required: bool = False, nargs: int = 1) -> dict:
    return {"name": name, "type": "text", "required": required, "nargs": nargs}


def test_arguments_are_ordered_and_a_new_one_is_additive_only_when_optional_and_last() -> None:
    base = _cli(arguments=[_argument("a", required=True)])
    optional = _cli(arguments=[_argument("a", required=True), _argument("b")])
    required = _cli(arguments=[_argument("a", required=True), _argument("b", required=True)])
    swapped = _cli(arguments=[_argument("b"), _argument("a", required=True)])
    variadic = _cli(arguments=[_argument("a", nargs=-1)])
    after_variadic = _cli(arguments=[_argument("a", nargs=-1), _argument("b")])
    assert tool.classify(base, optional, kind="behavior")[0]
    assert not tool.classify(base, optional, kind="behavior")[1]
    assert tool.classify(base, required, kind="behavior")[1]
    assert tool.classify(base, swapped, kind="behavior")[1]
    assert tool.classify(optional, base, kind="behavior")[1]
    assert tool.classify(variadic, after_variadic, kind="behavior")[1]
    assert tool.classify(
        base, _cli(arguments=[_argument("a", required=True, nargs=2)]), kind="behavior"
    )[1]


def test_the_cli_snapshot_records_choices_environment_and_arguments() -> None:
    commands = tool.behavior_snapshot()["cli"]
    level = commands["xeda run"]["options"]["log_level"]
    assert level["choices"][0] == "debug" and level["case_sensitive"] is True
    assert level["environment"] == ["XEDA_LOG_LEVEL"]
    assert [a["name"] for a in commands["xeda run"]["arguments"]] == ["flow", "design_file"]
    assert commands["xeda remote clean"]["options"]["older_than"]["range"][0] == 0
