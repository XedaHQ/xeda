"""Check the built-in flows and stable behavior against their release baseline."""

import copy
import json

import pytest

from tests.test_public_api import tool

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


def test_a_new_code_is_additive_and_a_removed_code_breaking() -> None:
    old = {"flows": {}, "codes": {"node": ["old"]}}
    new = {"flows": {}, "codes": {"node": ["new"]}}
    additive, breaking = tool.classify(old, new, kind="contract")
    assert additive and breaking


def test_the_code_inventory_exists() -> None:
    from xeda.flow_runner.error_codes import ALL_CODES

    assert "internal_error" in ALL_CODES["node"]


def test_the_session_variables_are_exactly_the_contract_list() -> None:
    from xeda.generation import SESSION_VARIABLES

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
    from xeda.flow.flow import FLOW_NAME
    from xeda.flow_runner.chains import OUTPUT_NAME

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
    from xeda.flow.metrics import METRIC_PATH

    assert bool(METRIC_PATH.fullmatch(path)) == ok


def test_the_private_files_have_their_names_places_and_keys() -> None:
    from xeda.local_settings import LOCAL_FILES

    assert LOCAL_FILES == (
        ("config.yaml", "$XDG_CONFIG_HOME/xeda", ("flows", "env", "run_root")),
        ("<stem>.local.yaml", "beside the project file", ("flows", "env", "run_root")),
    )


def test_all_literal_codes_are_in_the_inventory() -> None:
    import ast
    from pathlib import Path
    from xeda.flow_runner.error_codes import ALL_CODES, _SETTINGS_CODES
    from xeda import records

    found = set(_SETTINGS_CODES.values())
    found.update(
        getattr(records, name)
        for name in records.__all__
        if name.isupper() and isinstance(getattr(records, name), str)
    )
    for path in (Path(__file__).parent.parent / "src" / "xeda").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                found.update(
                    k.value.value
                    for k in node.keywords
                    if k.arg == "code"
                    and isinstance(k.value, ast.Constant)
                    and isinstance(k.value.value, str)
                )
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
    from xeda.flow_runner.error_codes import ALL_CODES

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
