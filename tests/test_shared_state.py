"""A launch leaves nothing of itself in the process that a later launch could meet.

An application that embeds xeda launches flows again and again in one process (a notebook, a
service, a design-space search). Whatever a launch writes into a table, a default, a class
attribute or the logging of the process stays for the next one, and no setting records it. This is
the oracle for that, in five parts, each with a deliberate offender that it must catch:

1. every registered flow is launched in one process, under the stand-in tools, with its
   own settings, with every option that can be flipped, with the options that write derived
   values, and again: no module variable, class attribute, function default or field default of
   `xeda.*` has changed (`state_snapshot.state`);
2. the finished flows share no mutable object with that state (`state_snapshot.shared_objects`):
   sharing is how a write into a flow's settings becomes a write into a table;
3. the process is as it was: working directory, environment, `sys.path`, logging levels and
   handlers, warning filters;
4. the settings mapping and the `Design` the caller gave are as they were;
5. no flow class holds a model instance (a `Tool`) as a class attribute: a tool finds its flow when
   it is made, so one made with the class never sees the flow it runs for.

The launches use `tests/test_isolation.py`'s world and stand-in tools, so what the sweep reaches is
what that one reaches (`UNREACHED` there lists the flows that stop short of `run()`).
"""

import copy
import enum
import logging
import os
import sys
import typing
import uuid
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from xeda import Design
from xeda.flow import SimFlow
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import FlowLauncher
from xeda.flows import VivadoAltSynth, VivadoSynth
from xeda.flows.vivado import vivado_alt_synth as alt
from xeda.run_root import ensure_run_root
from xeda.tool import Tool

from . import state_snapshot as ss
from .settings_samples import flow_classes, minimal_settings
from .test_isolation import (
    DESIGNS,
    EXTRA_SETTINGS,
    FAKED,
    FPGA_FAKED,
    PLAIN_TB_DESIGN,
    SQRT_DESIGN,
    _world,
)
from .tool_utils import use_fake_fpga_tools, use_fake_tools

ss.import_package()

FLOWS = [cls for cls, _ in flow_classes()]

#: Options that make a flow write what it derives from its settings (a waveform's format, the
#: mode of synthesis, a library merged), which the flipped options do not all reach.
HAND_VARIANTS: Dict[str, List[Dict[str, Any]]] = {
    "ghdl_sim": [{"vcd": "w.vcd"}, {"fst": "w.fst"}, {"wave": "w.ghw"}, {"wave": "w.vcd"}],
    "nvc": [{"wave": "w.vcd"}, {"wave": "w.fst"}],
    "vcs": [
        {"ucli_script": "notes.txt"},
        {"gui": True},
        {"sdf_file": "notes.txt", "sdf_instance": "uut"},
    ],
    "vivado_sim": [{"nthreads": 2}, {"sdf": {"max": "notes.txt", "root": "uut"}}],
    "vivado_synth": [{"bitstream": "outputs/x.bit"}, {"out_of_context": True}],
    "vivado_alt_synth": [{"out_of_context": True, "flatten_hierarchy": "full"}],
    "vivado_project": [{"out_of_context": True, "flatten_hierarchy": "full"}],
    "yosys_fpga": [{"abc_script": "notes.txt", "keep_hierarchy": ["sqrt"]}],
    "dc": [{"__language": "2019"}, {"__language": "1993"}],
    "ise_synth": [{"__language": "2019"}],
}
#: Settings that start a container or reach a machine: never flipped.
NOT_FLIPPED = ("docker", "remote", "ssh", "junest", "image")


def _flip_candidates(flow_class):
    """(name, value) for each boolean, literal and enumerated option, set to another value."""
    for name, info in flow_class.Settings.model_fields.items():
        if any(part in name for part in NOT_FLIPPED):
            continue
        annotation, default = info.annotation, info.default
        if annotation is bool or annotation == Optional[bool]:
            yield name, (not default) if isinstance(default, bool) else True
        elif typing.get_origin(annotation) is typing.Literal:
            options = [o for o in typing.get_args(annotation) if o != default and o is not None]
            if options:
                yield name, options[0]
        elif isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            options = [o for o in annotation if o != default]
            if options:
                yield name, options[0].value


def _flipped(flow_class, base: dict, world) -> dict:
    """`base`, with every option flipped that still validates beside the others."""
    settings = dict(base)
    for name, value in _flip_candidates(flow_class):
        if name in settings:
            continue
        trial = {**settings, name: value}
        try:
            flow_class.Settings.from_input(trial, design_root=world.work, runner_cwd=world.work)
        except Exception:  # noqa: BLE001 - a combination the flow refuses is left out
            continue
        settings = trial
    return settings


def _launch(flow_class, world, mp, settings: dict, launcher: dict):
    """One launch under the stand-in tools: the flow, or None when it raised, and what the launch
    changed in the caller's own objects."""
    use_fake_tools(mp)
    if flow_class.name in FPGA_FAKED:
        ensure_run_root(world.root)
        use_fake_fpga_tools(mp, world.root / ".fake-toolchain")
    if flow_class.name not in FAKED:
        mp.setattr("xeda.tool.run_process", lambda *args, **kwargs: "")
        mp.setattr("xeda.tool.Tool.version_gte", lambda self, *args: True)
    mp.chdir(world.work)
    rtl, tb = DESIGNS.get(flow_class.name, SQRT_DESIGN)
    if issubclass(flow_class, SimFlow) and flow_class.name in (
        "ghdl_sim",
        "nvc",
        "vivado_sim",
        "vcs",
    ):
        rtl, tb = PLAIN_TB_DESIGN  # a plain testbench, so that the stood-in simulator is reached
    settings = dict(settings)
    standard = settings.pop("__language", "2008")
    design = Design(
        name="sqrt",
        design_root=world.work,
        rtl=rtl,
        tb=tb,
        language={"vhdl": {"standard": standard}},
    )
    asked_settings, asked_design = copy.deepcopy(settings), design.model_dump_json()
    process_before = ss.process_state()
    try:
        flow = DefaultRunner(world.root, display_results=False, **launcher).launch_flow(
            flow_class, design, settings
        )
    except Exception:  # noqa: BLE001 - how it ends is not judged; what it leaves behind is
        flow = None
    # judged here as well as after the launches: the working directory and the environment the
    # stand-in tools set are put back when they are done, and would hide a change made meanwhile
    changed = [
        f"process: {line}" for line in ss.process_changes(process_before, ss.process_state())
    ]
    if settings != asked_settings:
        changed.append("the settings mapping the caller gave changed")
    if design.model_dump_json() != asked_design:
        changed.append("the Design the caller gave changed")
    return flow, changed


def check_flow(flow_class, tmp_path: Path, variants: Optional[list] = None) -> List[str]:
    """Launch `flow_class` as `variants` say, and say what it left behind in the process."""
    world = _world(tmp_path)
    base = {**minimal_settings(flow_class), **EXTRA_SETTINGS.get(flow_class.name, {})}
    if variants is None:
        variants = [
            ("its own settings", base, {}),
            (
                "every option flipped, with debug",
                _flipped(flow_class, base, world),
                {"debug": True},
            ),
            *[
                (f"options {extra}", {**base, **extra}, {})
                for extra in HAND_VARIANTS.get(flow_class.name, [])
            ],
            ("its own settings again, rebuilt", base, {"rebuild_all": True}),
        ]
    problems: List[str] = []
    state_before, process_before = ss.state(), ss.process_state()
    finished = []
    with pytest.MonkeyPatch.context() as mp:
        for label, settings, launcher in variants:
            flow, changed = _launch(flow_class, world, mp, settings, launcher)
            problems += [f"{label}: {line}" for line in changed]
            if flow is not None:
                finished.append((label, flow))
    problems += [f"state of the package: {line}" for line in ss.changes(state_before, ss.state())]
    problems += [
        f"process: {line}" for line in ss.process_changes(process_before, ss.process_state())
    ]
    shared = ss.shared_objects()
    for label, flow in finished:
        mine = ss.reachable(flow, "flow")
        problems += [
            f"{label}: {mine[oid][0]} ({mine[oid][1]}) is also {shared[oid][0]}"
            for oid in mine.keys() & shared.keys()
        ]
    return problems


# --- the sweep ---------------------------------------------------------------------------------


@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda cls: cls.name)
def test_launching_a_flow_leaves_nothing_in_the_process(flow_class, tmp_path) -> None:
    problems = check_flow(flow_class, tmp_path)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda cls: cls.name)
def test_no_flow_class_holds_a_model_instance(flow_class) -> None:
    """A tool made with the class has no flow: its `dockerized`, its run directory and what it
    learns of the program belong to no launch, and every launch of the process shares them."""
    assert not ss.models_held_by(flow_class)


def test_a_design_space_search_leaves_nothing_in_the_process(tmp_path) -> None:
    """The search takes variations and a log file for its length: the tables it starts from and
    the handlers of the process are as they were after it. (It runs no flow: the workers of a
    full search are other processes.)"""
    from .test_dse_run import _idle_search

    state_before, process_before = ss.state(), ss.process_state()
    with pytest.MonkeyPatch.context() as mp:
        _idle_search(tmp_path, mp)
    problems = [f"state: {line}" for line in ss.changes(state_before, ss.state())]
    problems += [
        f"process: {line}" for line in ss.process_changes(process_before, ss.process_state())
    ]
    assert not problems, "\n".join(problems)


# --- the oracle sees what it is meant to see ----------------------------------------------------
#
# Each offender is a deliberate wrong, made to a cheap flow under the stand-in tools; the oracle has
# to name it. The offenders work on copies of what they write, and put the process back.


def _victim(tmp_path, monkeypatch, offend) -> str:
    """Launch `vivado_alt_synth` with `offend(flow)` run in its `run()`; what the oracle says."""
    original = VivadoAltSynth.run

    def run(self):
        offend(self)
        return original(self)

    monkeypatch.setattr(VivadoAltSynth, "run", run)
    base = {**minimal_settings(VivadoAltSynth), **EXTRA_SETTINGS.get("vivado_alt_synth", {})}
    return "\n".join(check_flow(VivadoAltSynth, tmp_path, [("base", base, {})]))


def test_the_sweep_is_quiet_for_a_flow_that_changes_nothing(tmp_path, monkeypatch) -> None:
    assert _victim(tmp_path, monkeypatch, lambda flow: None) == ""


def test_the_sweep_sees_a_flow_that_writes_a_table_of_its_module(tmp_path, monkeypatch) -> None:
    table = copy.deepcopy(alt.strategies)  # the offender's, so that the real one stays whole
    monkeypatch.setattr(alt, "strategies", table)
    said = _victim(
        tmp_path,
        monkeypatch,
        lambda flow: table["synth"]["Timing"]["synth"].update(written_by_a_flow=True),
    )
    assert "strategies" in said and "written_by_a_flow" in said


def test_the_sweep_sees_a_flow_that_writes_a_table_of_its_class(tmp_path, monkeypatch) -> None:
    table = dict(VivadoAltSynth.results_description)
    monkeypatch.setattr(VivadoAltSynth, "results_description", table)
    said = _victim(
        tmp_path, monkeypatch, lambda flow: table.update(written_by_a_flow="a description")
    )
    assert "results_description" in said and "written_by_a_flow" in said


def test_the_sweep_sees_a_flow_that_writes_into_a_tool_its_class_holds(
    tmp_path, monkeypatch
) -> None:
    shared_tool = Tool("helper", version_flag=None)
    monkeypatch.setattr(VivadoAltSynth, "helper", shared_tool, raising=False)
    said = _victim(
        tmp_path, monkeypatch, lambda flow: setattr(shared_tool, "highlight_rules", {"a": "b"})
    )
    assert "helper" in said and "highlight_rules" in said
    assert ss.models_held_by(VivadoAltSynth) == ["VivadoAltSynth.helper"]


def test_the_sweep_sees_a_flow_that_writes_the_default_of_a_setting(tmp_path, monkeypatch) -> None:
    info = VivadoAltSynth.Settings.model_fields["suppress_msgs"]
    default = list(info.default)
    monkeypatch.setattr(info, "default", default)  # a copy: the real default stays whole
    said = _victim(tmp_path, monkeypatch, lambda flow: default.append("Synth 0-0000"))
    assert "suppress_msgs" in said and "Synth 0-0000" in said


def test_the_sweep_sees_a_flow_that_shares_a_table_with_its_settings(tmp_path, monkeypatch) -> None:
    table = copy.deepcopy(alt.strategies)
    monkeypatch.setattr(alt, "strategies", table)

    def share(flow):
        flow.settings.synth.steps["shared"] = table["synth"]["Timing"]["synth"]

    said = _victim(tmp_path, monkeypatch, share)
    assert "is also" in said and "strategies" in said


def test_the_sweep_sees_a_flow_that_shares_the_default_of_a_setting(tmp_path, monkeypatch) -> None:
    info = VivadoAltSynth.Settings.model_fields["suppress_msgs"]
    default = list(info.default)
    monkeypatch.setattr(info, "default", default)

    def share(flow):
        flow.settings.__dict__["suppress_msgs"] = default  # not a copy: a write shows in both

    said = _victim(tmp_path, monkeypatch, share)
    assert "is also" in said and "suppress_msgs" in said


def _loggers() -> dict:
    return {
        name: logger
        for name, logger in list(logging.root.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
    }


@pytest.fixture
def process_as_it_is():
    """What the process offenders change, put back after the test: the loggers that exist now as
    they are, and any a test makes with no configuration."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    named = {
        name: (logger.level, list(logger.handlers), logger.propagate, logger.disabled)
        for name, logger in _loggers().items()
    }
    filters = list(warnings.filters)
    path = list(sys.path)
    yield
    root.setLevel(level)
    root.handlers[:] = handlers
    for name, logger in _loggers().items():
        logger.level, logger.handlers[:], logger.propagate, logger.disabled = named.get(
            name, (logging.NOTSET, [], True, False)
        )
    warnings.filters[:] = filters
    sys.path[:] = path


def fresh_logger(kind: str) -> logging.Logger:
    """A logger no one has asked for yet: its name is new in the process each time."""
    return logging.getLogger(f"oracle.fresh.{kind}.{uuid.uuid4().hex[:8]}")


def _disabled(logger: logging.Logger) -> None:
    logger.disabled = True


def _not_propagating(logger: logging.Logger) -> None:
    logger.propagate = False


PROCESS_OFFENDERS = {
    "root logger level": (lambda mp: logging.getLogger().setLevel(logging.CRITICAL), "root logger"),
    "log handler": (
        lambda mp: logging.getLogger().addHandler(logging.NullHandler()),
        "root logger",
    ),
    "logger of xeda": (
        lambda mp: logging.getLogger("xeda.flow_runner.default_runner").setLevel(logging.CRITICAL),
        "xeda.flow_runner.default_runner",
    ),
    "new logger with a level": (
        lambda mp: fresh_logger("level").setLevel(logging.CRITICAL),
        "oracle.fresh.level",
    ),
    "new logger with a handler": (
        lambda mp: fresh_logger("handler").addHandler(logging.NullHandler()),
        "oracle.fresh.handler",
    ),
    "new logger that does not propagate": (
        lambda mp: _not_propagating(fresh_logger("propagate")),
        "oracle.fresh.propagate",
    ),
    "new logger that is disabled": (
        lambda mp: _disabled(fresh_logger("disabled")),
        "oracle.fresh.disabled",
    ),
    "sys.path": (lambda mp: sys.path.append("/nowhere/at/all"), "sys.path"),
    "warning filter": (lambda mp: warnings.simplefilter("ignore", DeprecationWarning), "warning"),
    "working directory": (lambda mp: os.chdir(Path.home()), "working directory"),
    "environment": (lambda mp: mp.setenv("XEDA_ORACLE_OFFENDER", "1"), "environment"),
}


@pytest.mark.parametrize("name", PROCESS_OFFENDERS)
def test_the_sweep_sees_a_launch_that_changes_the_process(
    name, tmp_path, monkeypatch, process_as_it_is
) -> None:
    offend, shown = PROCESS_OFFENDERS[name]
    original = FlowLauncher.launch_flow
    here = os.getcwd()

    def launch_flow(self, *args, **kwargs):
        flow = original(self, *args, **kwargs)
        offend(monkeypatch)  # after the launch's own working directory is back
        return flow

    monkeypatch.setattr(FlowLauncher, "launch_flow", launch_flow)
    base = {**minimal_settings(VivadoAltSynth), **EXTRA_SETTINGS.get("vivado_alt_synth", {})}
    try:
        said = "\n".join(check_flow(VivadoAltSynth, tmp_path, [("base", base, {})]))
    finally:
        os.chdir(here)
    assert shown in said


def test_the_sweep_lets_a_launch_make_a_logger_it_does_not_configure(
    tmp_path, monkeypatch, process_as_it_is
) -> None:
    """Importing a module during a launch makes its logger: no change of the process."""
    said = _victim(tmp_path, monkeypatch, lambda flow: fresh_logger("plain"))
    assert said == ""


def test_the_sweep_sees_a_launch_that_changes_what_the_caller_gave(tmp_path, monkeypatch) -> None:
    original = FlowLauncher.launch_flow

    def launch_flow(self, flow_class, design, settings, *args, **kwargs):
        flow = original(self, flow_class, design, settings, *args, **kwargs)
        settings["injected"] = True
        design.rtl.top = "somewhere_else"
        return flow

    monkeypatch.setattr(FlowLauncher, "launch_flow", launch_flow)
    base = {**minimal_settings(VivadoAltSynth), **EXTRA_SETTINGS.get("vivado_alt_synth", {})}
    said = "\n".join(check_flow(VivadoAltSynth, tmp_path, [("base", base, {})]))
    assert "settings mapping the caller gave changed" in said
    assert "Design the caller gave changed" in said


def test_the_launches_use_the_product_flows_only() -> None:
    assert FLOWS and all(not cls.__name__.startswith("_") for cls in FLOWS)
    assert {"vivado_synth", "quartus", "vcs"} <= {cls.name for cls in FLOWS}
    assert VivadoSynth in FLOWS
