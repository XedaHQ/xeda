"""The opt-in real-tool tests still plan: their settings, designs and sources are valid today.

The opt-in layers do not run in CI, so a change of a settings model, a design field or a source
type does not fail them there, and they go stale unnoticed. This test runs each of them in a
child process as far as its launch plan (`tests/optin_plan_probe.py`): the layer checks pass, no
tool starts, and a launch ends the test after it planned the run. A stale settings value, design
field, source type or flows table fails there with the error that a real launch gives.

An opt-in test is a test that reaches a `require_*` check of an opt-in layer and a launch of a
flow, through its own body, a fixture or a helper of its module (a static scan, here). A test that
sets or reads the variable of a layer by name tests the layer itself and stays out.

Not covered: what a test does after its launch (the checks of results, metrics and artifacts, and
the end evidence of a testbench), the layers of external Bluespec repositories and of a remote
host, and a settings mapping that a test builds after its first launch. `test_optin_literals.py`
holds the checks that need no launch.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .optin_plan_probe import OPT_IN_REQUIREMENTS, PLANNED

pytestmark = pytest.mark.python_compat

TESTS = Path(__file__).parent
CHECKOUT = TESTS.parent
THIS_FILE = Path(__file__).name

#: Tests that the scan finds but that launch no real flow, with why. They pass here. The key is a
#: node id without the parameters.
NO_LAUNCH: dict[str, str] = {
    "tests/test_yosys_asic_abc.py::test_yosys_mfs3_missing_obeys_require_tools": (
        "it replaces the runner, to test how a missing ABC command is reported"
    ),
}

#: Tests that need real files before their launch, so they cannot reach it here, with what they
#: need. The key is a node id without the parameters.
NEEDS_THE_FILES: dict[str, str] = {
    "tests/test_openroad_real.py::test_the_chain_writes_nothing_outside_its_run_root": (
        "it copies the PDK store"
    ),
    "tests/test_platform_conformance.py::test_real_yosys_maps_a_counter_to_the_cells_of_the_platform": (
        "it locates the files of the platform in the PDK store"
    ),
    "tests/test_platform_conformance.py::test_real_yosys_uses_no_cell_that_the_platform_excludes": (
        "it locates the files of the platform in the PDK store"
    ),
}


def _names(node: ast.AST) -> set[str]:
    """Every name, attribute name and parameter name inside `node`."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.arg):
            found.add(child.arg)
    return found


def _strings(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


#: The names that start a launch of a flow.
LAUNCHERS = frozenset({"DefaultRunner", "run_flow", "launch_flow"})


def opt_in_tests(path: Path) -> list[str]:
    """The names of the opt-in test functions of one test module that launch a flow.

    A function or class of the module reaches the names that its body uses, and, through a name
    of the module, those that function or class reaches in turn."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for name, node in definitions.items():
        if isinstance(node, ast.ClassDef) and name.startswith("Test"):
            raise AssertionError(f"{path.name}: the class {name}: tests are functions here")
    direct = {name: _names(node) - {name} for name, node in definitions.items()}
    reached: dict[str, set[str]] = {}
    for name in definitions:
        found: set[str] = set()
        pending = [name]
        while pending:
            for used in direct.get(pending.pop(), ()):
                if used not in found:
                    found.add(used)
                    pending.append(used)
        reached[name] = found
    return sorted(
        name
        for name, node in definitions.items()
        if name.startswith("test_")
        and reached[name] & set(OPT_IN_REQUIREMENTS)
        and reached[name] & LAUNCHERS
        and not any(text.startswith("XEDA_TESTS_") for text in _strings(node))
    )


def opt_in_node_ids() -> list[str]:
    ids = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name != THIS_FILE:
            ids.extend(f"tests/{path.name}::{name}" for name in opt_in_tests(path))
    return ids


def test_the_scan_finds_the_opt_in_tests_of_every_layer() -> None:
    ids = opt_in_node_ids()
    for module in (
        "test_dockerized",
        "test_vivado_real",
        "test_openxc7_real",
        "test_openroad_real",
        "test_platform_conformance",
    ):
        assert any(f"tests/{module}.py::" in node_id for node_id in ids), module
    assert len(ids) > 60


def test_every_opt_in_test_plans_its_launch(tmp_path: Path) -> None:
    ids = opt_in_node_ids()
    outcomes = tmp_path / "outcomes.jsonl"
    environment = dict(os.environ)
    environment["XEDA_TESTS_WORK_DIR"] = str(tmp_path / "work")
    # a `docker` that answers every query with success, and runs nothing: a test checks for its
    # images with `docker image inspect` before it launches
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "docker").write_text("#!/bin/sh\nexit 0\n")
    (shim / "docker").chmod(0o755)
    environment["PATH"] = str(shim) + os.pathsep + environment.get("PATH", "")
    done = subprocess.run(
        [
            sys.executable,
            *("-m", "pytest", "-p", "tests.optin_plan_probe", "-p", "no:cacheprovider"),
            *("-o", "addopts=", "-q", "-rfEs", "--tb=short", "--basetemp", str(tmp_path / "bt")),
            *("--optin-outcomes", str(outcomes)),
            *ids,
        ],
        cwd=CHECKOUT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
    )
    failed = {
        line.split()[1].split("[")[0]
        for line in done.stdout.splitlines()
        if line.startswith(("FAILED ", "ERROR "))
    }
    assert PLANNED in done.stdout, done.stdout[-6000:] + done.stderr[-2000:]
    # every test either reaches its plan or is a reviewed test that needs real files: a test that
    # passes, or skips for another reason (a check that the probe does not make pass), is wrong
    records = [json.loads(line) for line in outcomes.read_text(encoding="utf-8").splitlines()]
    planned = {
        r["node"] for r in records if r["outcome"] == "skipped" and r["reason"].endswith(PLANNED)
    }
    reviewed = set(NEEDS_THE_FILES) | set(NO_LAUNCH)
    other = sorted(
        f"{r['node']}: {r['outcome']} at {r['when']} {r['reason']}".strip()
        for r in records
        if r["node"] not in planned and r["node"].split("[")[0] not in reviewed
    )
    assert other == [], "\n".join(other)
    passed = {r["node"].split("[")[0] for r in records if r["outcome"] == "passed"}
    assert passed == set(NO_LAUNCH), f"pass, so launch nothing: {sorted(passed - set(NO_LAUNCH))}"
    assert failed == set(NEEDS_THE_FILES), (
        f"fail here, but need no real file: {sorted(failed - set(NEEDS_THE_FILES))}\n"
        f"need real files, but plan: {sorted(set(NEEDS_THE_FILES) - failed)}\n"
        + done.stdout[-6000:]
        + done.stderr[-2000:]
    )
