"""The environment of the processes of a flow is evidence, and one snapshot per launch gives it.

A launch reads the environment of the caller once. Every process of every node, Xeda's own reads
for a flow included, sees that snapshot through the node's view: with `inherit_env` the whole
environment, without it only the essential variables, the variables that locate a tool or a
license, `env` and the flow's own variables. The private `env` of the local settings joins both
modes. The digest of the view is in the trace of each node, so a changed value makes the node
stale. No test starts a real tool: the flows here start Python or a recorded `docker`.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

import xeda.tool
from xeda import Design
from xeda.cocotb import Cocotb
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, MetricSpec
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.environment import (
    ESSENTIAL_VARIABLES,
    SESSION_VARIABLES,
    EnvironmentSnapshot,
    admitted,
    container_environment,
    environment_evidence,
    node_environment,
)
from xeda.flow_runner.trace import read_trace
from xeda.flows import Verilator
from xeda.local_settings import LocalSettings
from xeda.records import read_record
from xeda.tool import Docker, Tool

from .launch_utils import empty_project

pytestmark = pytest.mark.python_compat

OLD, NEW = "xeda-sentinel-old-7f3a91", "xeda-sentinel-new-c2d804"

#: What the probe flow's Python process writes: its whole environment.
DUMP = "import json, os; open('seen.json', 'w').write(json.dumps(dict(os.environ)))"


def _probe_flow(*license_variables: str) -> type[Flow]:
    """A flow that starts Python once and records the environment the process got."""

    class EnvProbe(Flow):
        """Starts Python, which writes its environment to `seen.json`."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        locator_variables = tuple(license_variables)

        def run(self) -> None:
            Tool(sys.executable).run("-c", DUMP, stdout=True)

    return EnvProbe


def _docker_flow() -> type[Flow]:
    """A flow that starts one program in a container."""

    class EnvDocker(Flow):
        """Starts `some-tool` in the image `img`."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def init(self) -> None:
            self.tool = Tool(executable="some-tool", docker=Docker(image="img"))

        def run(self) -> None:
            self.tool.run("arg", stdout=True, env={"OWN": "flow"})

    return EnvDocker


def _cocotb_flow(monkeypatch: pytest.MonkeyPatch) -> type[Flow]:
    """A flow that asks its cocotb support for the environment of a simulator."""

    def run_get_stdout(self, *args, **kwargs):
        if "--python-bin" in args:
            return sys.executable
        return "2.0.0" if "--version" in args else None

    monkeypatch.setattr(Cocotb, "run_get_stdout", run_get_stdout)

    class EnvCocotb(Flow):
        """Writes the environment that cocotb gives a simulator to `cocotb_env.json`."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            environ = Cocotb(sim_name="verilator").env(self.design)
            (self.run_path / "cocotb_env.json").write_text(json.dumps(environ))

    return EnvCocotb


def _design(tmp_path: Path, *, cocotb: bool = False) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "tb_top.py").write_text("")
    return Design(
        name="d",
        root=root,
        rtl={"sources": ["top.v"], "top": "top"},
        **({"tb": {"sources": ["tb_top.py"], "cocotb": True}} if cocotb else {}),
    )


def _runner(tmp_path: Path, run: str = "run") -> DefaultRunner:
    return DefaultRunner(tmp_path / run, display_results=False)


def _launch(
    tmp_path: Path,
    flow: type[Flow],
    settings: dict[str, Any] | None = None,
    *,
    local: LocalSettings | None = None,
    run: str = "run",
    cocotb: bool = False,
) -> Flow:
    result = _runner(tmp_path, run).run(
        flow,
        _design(tmp_path, cocotb=cocotb),
        xedaproject=empty_project(tmp_path),
        flow_settings=settings or {},
        local_settings=local,
    )
    assert result is not None and result.succeeded
    return result


def _plan(
    tmp_path: Path,
    flow: type[Flow],
    settings: dict[str, Any] | None = None,
    *,
    local: LocalSettings | None = None,
    run: str = "run",
):
    return _runner(tmp_path, run).plan_with_freshness(
        flow,
        _design(tmp_path),
        xedaproject=empty_project(tmp_path),
        flow_settings=settings or {},
        local_settings=local,
    )


def _seen(flow: Flow) -> dict[str, str]:
    return json.loads((flow.run_path / "seen.json").read_text())


# ------------------------------------------------------------------------------ freshness


def test_an_unchanged_environment_leaves_the_node_fresh(tmp_path):
    flow = _probe_flow()
    _launch(tmp_path, flow)
    _plan_, states = _plan(tmp_path, flow)
    assert states[flow.name].state == "fresh"


@pytest.mark.parametrize("name", ["NUM_TV", "BSC_OPTIONS", "PYTHONPATH"])
def test_a_changed_inherited_variable_makes_the_node_stale(tmp_path, monkeypatch, name):
    flow = _probe_flow()
    monkeypatch.setenv(name, OLD)
    _launch(tmp_path, flow)
    monkeypatch.setenv(name, NEW)
    _plan_, states = _plan(tmp_path, flow)
    node = states[flow.name]
    assert node.state == "stale" and "a value changed" in node.reason
    assert OLD not in node.reason and NEW not in node.reason


@pytest.mark.parametrize("name", ["PWD", "SSH_CONNECTION", "XEDA_LOG_LEVEL"])
def test_a_changed_session_variable_does_not(tmp_path, monkeypatch, name):
    flow = _probe_flow()
    monkeypatch.setenv(name, OLD)
    _launch(tmp_path, flow)
    monkeypatch.setenv(name, NEW)
    _plan_, states = _plan(tmp_path, flow)
    assert states[flow.name].state == "fresh"


def test_an_added_inherited_variable_is_named_in_the_stale_reason(tmp_path, monkeypatch):
    flow = _probe_flow()
    monkeypatch.delenv("XEDA_SENTINEL_ADDED", raising=False)
    _launch(tmp_path, flow)
    monkeypatch.setenv("XEDA_SENTINEL_ADDED", OLD)
    _plan_, states = _plan(tmp_path, flow)
    node = states[flow.name]
    assert node.state == "stale"
    assert "`XEDA_SENTINEL_ADDED` added" in node.reason and OLD not in node.reason


def test_a_removed_inherited_variable_is_named_in_the_stale_reason(tmp_path, monkeypatch):
    flow = _probe_flow()
    monkeypatch.setenv("XEDA_SENTINEL_REMOVED", OLD)
    _launch(tmp_path, flow)
    monkeypatch.delenv("XEDA_SENTINEL_REMOVED")
    _plan_, states = _plan(tmp_path, flow)
    node = states[flow.name]
    assert node.state == "stale"
    assert "`XEDA_SENTINEL_REMOVED` removed" in node.reason and OLD not in node.reason


def test_a_changed_private_variable_makes_the_node_stale(tmp_path):
    flow = _probe_flow()
    _launch(tmp_path, flow, local=LocalSettings(env={"XEDA_PRIVATE": OLD}))
    _plan_, states = _plan(tmp_path, flow, local=LocalSettings(env={"XEDA_PRIVATE": NEW}))
    node = states[flow.name]
    assert node.state == "stale" and "a value changed" in node.reason


def test_env_and_inherit_env_change_the_identity(tmp_path):
    flow = _probe_flow()
    hashes = {
        label: _plan(tmp_path, flow, settings)[0].node(flow.name).flowrun_hash
        for label, settings in {
            "plain": {},
            "env": {"env": {"A": "1"}},
            "other env": {"env": {"A": "2"}},
            "clean": {"inherit_env": False},
        }.items()
    }
    assert len(set(hashes.values())) == len(hashes)


def test_the_trace_and_the_record_hold_names_and_no_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_SENTINEL_NAME", OLD)
    flow = _probe_flow()
    ran = _launch(tmp_path, flow, local=LocalSettings(env={"XEDA_SENTINEL_PRIVATE": NEW}))
    trace, _ns = read_trace(ran.run_path)
    assert trace.environment_digest
    assert "XEDA_SENTINEL_NAME" in trace.environment_names
    assert "XEDA_SENTINEL_PRIVATE" in trace.environment_names
    assert trace.environment_names == sorted(trace.environment_names)
    assert not set(trace.environment_names) & SESSION_VARIABLES
    record = read_record(ran.run_path, run_root=ran.run_directory.run_root)
    assert {"XEDA_SENTINEL_NAME", "XEDA_SENTINEL_PRIVATE"} <= set(record.inherited_variables)
    for kept in (ran.run_path / "trace.json", ran.run_path / "results.json"):
        text = kept.read_text()
        assert OLD not in text and NEW not in text, kept.name


def test_the_record_names_the_inherited_variables_and_no_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_SENTINEL_NAME", OLD)
    ran = _launch(tmp_path, _probe_flow())
    assert "XEDA_SENTINEL_NAME" in ran.record.inherited_variables
    assert OLD not in json.dumps(ran.record.inherited_variables)


# --------------------------------------------------------------------- what a process sees


def test_the_default_mode_passes_the_environment_of_the_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_CALLER", OLD)
    ran = _launch(tmp_path, _probe_flow(), {"env": {"XEDA_FROM_ENV": "env"}})
    seen = _seen(ran)
    assert seen["XEDA_CALLER"] == OLD and seen["XEDA_FROM_ENV"] == "env"


def test_the_private_env_and_env_override_the_caller_in_that_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_A", "caller")
    monkeypatch.setenv("XEDA_B", "caller")
    monkeypatch.setenv("XEDA_C", "caller")
    local = LocalSettings(env={"XEDA_B": "private", "XEDA_C": "private"})
    ran = _launch(tmp_path, _probe_flow(), {"env": {"XEDA_C": "env"}}, local=local)
    seen = _seen(ran)
    assert (seen["XEDA_A"], seen["XEDA_B"], seen["XEDA_C"]) == ("caller", "private", "env")


@pytest.mark.parametrize("inherit_env", [False, True])
@pytest.mark.parametrize("source", ["private", "settings"])
def test_sibling_discovery_uses_the_effective_path(tmp_path, monkeypatch, inherit_env, source):
    parent, child = tmp_path / "parent", tmp_path / "child"
    for directory in (parent, child):
        directory.mkdir()
        program = directory / "sibling-tool"
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{parent}{os.pathsep}{os.environ['PATH']}")

    class SiblingProbe(Flow):
        """Checks the executable selected for sibling discovery."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            assert Tool("sibling-tool").executable_path() == (child / "sibling-tool").resolve()
            Tool("sibling-tool").run()

    settings = {"inherit_env": inherit_env}
    local = LocalSettings(env={"PATH": str(child)}) if source == "private" else None
    if source == "settings":
        settings["env"] = {"PATH": str(child)}
    ran = _launch(tmp_path, SiblingProbe, settings, local=local)
    recorded = read_trace(ran.run_path)[0].programs
    assert set(recorded) == {"sibling-tool"}
    assert recorded["sibling-tool"].path == str((child / "sibling-tool").resolve())
    _, states = _plan(tmp_path, SiblingProbe, settings, local=local)
    assert states[SiblingProbe.name].state == "fresh"


def test_the_clean_mode_passes_essentials_locators_and_env_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_CALLER", OLD)
    monkeypatch.setenv("XEDA_LICENSE_FILE", "27000@server")
    monkeypatch.setenv("LC_XEDA_SENTINEL", "C")
    flow = _probe_flow("XEDA_*_FILE")
    ran = _launch(
        tmp_path,
        flow,
        {"inherit_env": False, "env": {"XEDA_FROM_ENV": "env"}},
        local=LocalSettings(env={"XEDA_PRIVATE": "private"}),
    )
    seen = _seen(ran)
    assert "PATH" in seen and seen["XEDA_LICENSE_FILE"] == "27000@server"
    assert seen["LC_XEDA_SENTINEL"] == "C"
    assert seen["XEDA_FROM_ENV"] == "env" and seen["XEDA_PRIVATE"] == "private"
    assert "XEDA_CALLER" not in seen


def test_in_the_clean_mode_cocotb_reads_no_caller_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", OLD)
    flow = _cocotb_flow(monkeypatch)
    ran = _launch(tmp_path, flow, {"inherit_env": False}, cocotb=True)
    environ = json.loads((ran.run_path / "cocotb_env.json").read_text())
    assert OLD not in environ["PYTHONPATH"]


def test_xeda_reads_the_admitted_view(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", OLD)
    flow = _cocotb_flow(monkeypatch)
    clean = _launch(tmp_path, flow, {"inherit_env": False}, cocotb=True, run="clean")
    inherited = _launch(tmp_path, flow, cocotb=True, run="inherited")
    assert OLD not in json.loads((clean.run_path / "cocotb_env.json").read_text())["PYTHONPATH"]
    assert OLD in json.loads((inherited.run_path / "cocotb_env.json").read_text())["PYTHONPATH"]


def test_cocotb_reads_the_python_of_the_env_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("PYGPI_PYTHON_BIN", OLD)
    flow = _cocotb_flow(monkeypatch)
    ran = _launch(tmp_path, flow, {"env": {"PYGPI_PYTHON_BIN": sys.executable}}, cocotb=True)
    environ = json.loads((ran.run_path / "cocotb_env.json").read_text())
    assert environ["PYGPI_PYTHON_BIN"] == sys.executable


def test_a_caller_variable_and_a_private_variable_of_one_name_differ(tmp_path, monkeypatch):
    flow = _probe_flow()
    clean = {"inherit_env": False}
    monkeypatch.setenv("XEDA_FOO", OLD)
    from_caller = _launch(tmp_path, flow, clean, run="caller")
    monkeypatch.delenv("XEDA_FOO")
    from_private = _launch(
        tmp_path, flow, clean, local=LocalSettings(env={"XEDA_FOO": OLD}), run="private"
    )
    assert "XEDA_FOO" not in _seen(from_caller) and _seen(from_private)["XEDA_FOO"] == OLD
    digests = {
        read_trace(ran.run_path)[0].environment_digest for ran in (from_caller, from_private)
    }
    assert len(digests) == 2


# ----------------------------------------------------------------------------------- Docker


@pytest.fixture
def docker_calls(monkeypatch):
    """The `docker` commands that tools start: their arguments and keywords."""
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        xeda.tool,
        "run_process",
        lambda executable, args=None, **kwargs: calls.append(
            ([str(a) for a in args or []], kwargs)
        ),
    )
    return calls


def _container_file(ran: Flow) -> dict[str, str]:
    lines = (ran.run_path / ".some-tool_docker.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def _client_of(calls) -> dict[str, str]:
    (kwargs,) = [kwargs for args, kwargs in calls if "arg" in args]
    return kwargs["env"]


def test_a_docker_container_gets_the_private_env_then_env(tmp_path, monkeypatch, docker_calls):
    monkeypatch.setenv("XEDA_CALLER", OLD)
    local = LocalSettings(env={"XEDA_P": "private", "XEDA_Q": "private"})
    ran = _launch(tmp_path, _docker_flow(), {"docker": True, "env": {"XEDA_Q": "env"}}, local=local)
    inside = _container_file(ran)
    assert (inside["XEDA_P"], inside["XEDA_Q"], inside["OWN"]) == ("private", "env", "flow")
    assert "XEDA_CALLER" not in inside
    assert _client_of(docker_calls)["XEDA_CALLER"] == OLD


def test_the_clean_mode_keeps_the_docker_locators_for_the_client(
    tmp_path, monkeypatch, docker_calls
):
    monkeypatch.setenv("XEDA_CALLER", OLD)
    monkeypatch.setenv("DOCKER_HOST", "unix:///xeda-sentinel.sock")
    _launch(tmp_path, _docker_flow(), {"docker": True, "inherit_env": False})
    client = _client_of(docker_calls)
    assert client["DOCKER_HOST"] == "unix:///xeda-sentinel.sock" and "PATH" in client
    assert "XEDA_CALLER" not in client


def test_a_caller_variable_and_a_private_variable_of_one_name_differ_in_docker(
    tmp_path, monkeypatch, docker_calls
):
    flow = _docker_flow()
    settings = {"docker": True, "inherit_env": False}
    monkeypatch.setenv("XEDA_FOO", OLD)
    from_caller = _launch(tmp_path, flow, settings, run="caller")
    monkeypatch.delenv("XEDA_FOO")
    from_private = _launch(
        tmp_path, flow, settings, local=LocalSettings(env={"XEDA_FOO": OLD}), run="private"
    )
    assert "XEDA_FOO" not in _container_file(from_caller)
    assert _container_file(from_private)["XEDA_FOO"] == OLD


# ----------------------------------------------------------------------- the functions


def _snapshot(inherited: dict[str, str], private: dict[str, str] | None = None):
    return EnvironmentSnapshot(inherited, private or {})


def test_the_snapshot_is_one_copy_of_the_environment(monkeypatch):
    monkeypatch.setenv("XEDA_SENTINEL", OLD)
    snapshot = EnvironmentSnapshot.take({"XEDA_PRIVATE": "x"})
    monkeypatch.setenv("XEDA_SENTINEL", NEW)
    assert snapshot.inherited["XEDA_SENTINEL"] == OLD and snapshot.private["XEDA_PRIVATE"] == "x"
    with pytest.raises(TypeError):
        snapshot.inherited["XEDA_SENTINEL"] = NEW  # type: ignore[index]


def test_the_admitted_view_of_each_mode():
    snapshot = _snapshot(
        {"PATH": "/bin", "OTHER": "1", "LC_ALL": "C", "XEDA_X": "caller"}, {"XEDA_X": "p"}
    )
    flow = _probe_flow("OTHER")
    inherit = flow.Settings()
    clean = flow.Settings(inherit_env=False)
    assert dict(admitted(snapshot, inherit, flow)) == {
        "PATH": "/bin",
        "OTHER": "1",
        "LC_ALL": "C",
        "XEDA_X": "p",
    }
    assert dict(admitted(snapshot, clean, flow)) == {
        "PATH": "/bin",
        "OTHER": "1",
        "LC_ALL": "C",
        "XEDA_X": "p",
    }
    plain = _probe_flow()
    assert "OTHER" not in admitted(snapshot, plain.Settings(inherit_env=False), plain)
    assert "PATH" in ESSENTIAL_VARIABLES


def test_the_node_environment_and_the_container_environment():
    snapshot = _snapshot({"PATH": "/bin", "CALLER": "1"}, {"P": "private", "Q": "private"})
    flow = _probe_flow()
    settings = flow.Settings(env={"Q": "env", "R": "env"})
    assert node_environment(snapshot, settings, flow, {"R": "own"}) == {
        "PATH": "/bin",
        "CALLER": "1",
        "P": "private",
        "Q": "env",
        "R": "own",
    }
    assert container_environment(snapshot, settings, {"R": "own"}) == {
        "P": "private",
        "Q": "env",
        "R": "own",
    }


def test_the_evidence_leaves_out_the_session_variables_and_keeps_two_sections():
    flow = _probe_flow()
    settings = flow.Settings()
    base = {"A": "1", "PWD": "/one", "SSH_CONNECTION": "x"}
    digest, names = environment_evidence(_snapshot(base), settings, flow)
    other = {"A": "1", "PWD": "/two", "SSH_CONNECTION": "y"}
    assert environment_evidence(_snapshot(other), settings, flow)[0] == digest
    assert names == ["A"]
    moved = environment_evidence(_snapshot({}, {"A": "1"}), settings, flow)
    assert moved[0] != digest and moved[1] == ["A"]


# --------------------------------------------------------------------------- the settings


def test_env_takes_names_and_text():
    settings = Flow.Settings.model_validate({"env": {"NUM_TV": "8", "_a1": ""}})
    assert settings.env == {"NUM_TV": "8", "_a1": ""} and settings.inherit_env is True
    for name in ("1X", "A-B", "A=B", ""):
        with pytest.raises(FlowSettingsError, match="environment variable"):
            Flow.Settings.from_input({"env": {name: "1"}})
    with pytest.raises(FlowSettingsError, match='write "8"'):
        Flow.Settings.from_input({"env": {"NUM_TV": 8}})


def test_a_variable_that_cocotb_sets_is_refused_and_names_its_setting(tmp_path):
    design = _design(tmp_path, cocotb=True)
    refused = {
        "COCOTB_RANDOM_SEED": "cocotb.random_seed",
        "COCOTB_RESOLVE_X": "cocotb.resolve_x",
        "COCOTB_REDUCED_LOG_FMT": "cocotb.reduced_log_fmt",
        "PYTHONPATH": "",
        "PYTHONPYCACHEPREFIX": "",
        "COCOTB_ANYTHING": "",
    }
    for name, setting in refused.items():
        settings = Verilator.Settings.from_input({"env": {name: "1"}})
        with pytest.raises(FlowSettingsException, match=name) as error:
            Verilator.check_design_supported(design, settings)
        assert error.value.code == "invalid_value" and setting in str(error.value)
    Verilator.check_design_supported(
        design, Verilator.Settings.from_input({"env": {"NUM_TV": "1"}})
    )


# ------------------------------------------------------------------------------ the code


def test_no_flow_module_reads_os_environ():
    """Only `flow_runner/environment.py` of the flow code reads the process environment."""
    source = Path(xeda.tool.__file__).parent
    files = [
        *(source / "flows").rglob("*.py"),
        *(source / "flow").rglob("*.py"),
        source / "cocotb.py",
    ]
    found = []
    for file in files:
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "environb"):
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    found.append(f"{file.relative_to(source)}:{node.lineno}")
    assert not found


def test_the_session_variables_are_the_ones_the_generators_leave_out():
    from xeda.generation import SESSION_VARIABLES as GENERATOR_SESSION_VARIABLES

    assert SESSION_VARIABLES == GENERATOR_SESSION_VARIABLES
