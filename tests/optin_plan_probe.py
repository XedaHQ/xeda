"""A pytest plugin that runs an opt-in real-tool test as far as the launch plan.

The opt-in layers (`XEDA_TESTS_VIVADO`, `_DOCKER`, `_OPENXC7`, `_OPENROAD`, `_YOSYS_TCL`,
`_PDK_ROOT`, `_ASIC_QOR`) do not run in CI, so nothing else notices that a settings value, a
design field or a source type in them went stale. With this plugin (`-p tests.optin_plan_probe`)

- every `require_*` check of `tool_utils` passes, and no tool is needed;
- the first launch of a test (`DefaultRunner.launch_flow`) validates its settings and design and
  plans the run, exactly as a real launch does, and then ends the test as skipped, with the
  reason `PLANNED`, before any tool starts.

A test that has a stale settings value or design field fails at its own plan.
What runs after the launch (checks of results, metrics and artifacts) is not run, and a test
that needs a real tool before its launch fails here and has an entry in `NEEDS_THE_FILES`
(`tests/test_optin_planning.py`).

Every check passes, the private `_require` and `_require_command` too, so the result does not
depend on the tools of the machine. With `--optin-outcomes <file>`, the plugin writes the outcome
of each test to that file, one JSON line each, so that a test that skips for another reason is
seen.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from . import tool_utils

#: A directory that does not exist: where a check that passes says that its tool or store is.
NOTHING = Path("/nonexistent")

#: The check functions of the opt-in layers of `tool_utils`, with what each returns when it passes.
OPT_IN_REQUIREMENTS: dict[str, Any] = {
    "require_vivado": None,
    "require_openxc7": NOTHING / "openxc7",
    "require_openroad": NOTHING / "openroad",
    "require_yosys_tcl": None,
    "require_asic_qor": None,
    "require_pdk_store": NOTHING / "pdk_store",
    "require_orfs_checkout": NOTHING / "orfs",
    "require_docker": None,
    "require_docker_image": None,
    "require_modelsim": None,
}

#: The reason of the skip that ends a test at its launch.
PLANNED = "PLANNED"

#: The layers that fetch from the network or need a host, and so stay skipped.
SKIPPED_LAYERS = ("XEDA_TESTS_EXTERNAL", "XEDA_TESTS_REMOTE")


def _pass_requirement(result: Any):
    def requirement(*args: Any, **kwargs: Any) -> Any:
        return result

    return requirement


def _opted_in(variable: str) -> bool:
    return not variable.startswith(SKIPPED_LAYERS)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--optin-outcomes", help="the file of the outcome of each test")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Write the outcome of a test: a phase that did not pass, or its call."""
    path = getattr(pytest_runtest_logreport, "path", None)
    if path is None or (report.passed and report.when != "call"):
        return
    reason = report.longrepr[2] if report.skipped and isinstance(report.longrepr, tuple) else ""
    line = {"node": report.nodeid, "when": report.when, "outcome": report.outcome}
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({**line, "reason": reason}) + "\n")


def pytest_configure(config: pytest.Config) -> None:
    """Make the checks pass and stop each launch at its plan. Importing this module for its
    names changes nothing; only a session that loads it as a plugin does."""
    pytest_runtest_logreport.path = config.getoption("optin_outcomes")  # type: ignore[attr-defined]
    checks = [
        name
        for name in dir(tool_utils)
        if name.lstrip("_").startswith("require") and callable(getattr(tool_utils, name))
    ]
    for name in checks:
        setattr(tool_utils, name, _pass_requirement(OPT_IN_REQUIREMENTS.get(name)))
    tool_utils._opted_in = _opted_in
    from xeda._internal.flow_runner import default_runner

    original = default_runner.DefaultRunner.launch_flow

    def launch_flow(self, flow_class, design, flow_settings=None, *args, **kwargs):
        if kwargs.get("depender") is not None:
            return original(self, flow_class, design, flow_settings, *args, **kwargs)
        if kwargs.get("plan") is None:
            self.resolve(flow_class, design, flow_settings, kwargs.get("all_flows_settings"))
        pytest.skip(PLANNED)

    default_runner.DefaultRunner.launch_flow = launch_flow  # type: ignore[method-assign]
