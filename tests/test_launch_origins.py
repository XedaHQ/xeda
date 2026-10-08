"""A design is planned and launched from the same settings, by whichever door the launch comes in.

A design's own `flows.<flow>` sections are its origin of a flow's settings. `run` and `plan` read
them from the design they load, and the remote runner from the design it ships. `run_flow`,
`launch_flow` and `resolve` are handed a built design, and must read them from it: otherwise a
setting that only a section gives -- the device, for a flow that cannot run without one -- is
missing at launch while the plan has it (`vivado_power`, whose device is written under
`flows.vivado_synth`, failed that way through `run_flow`).

Here the device is written in exactly one section: the requested flow's own, or only its
producer's, which reaches the requested flow along the declared edge. Each door must then resolve
the nodes, and identities, that `plan` names. The same holds for an input binding that the design
writes. Above the design in precedence come the sections a launch is handed, and above those its
settings; the design is never edited.
"""

import copy
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dse import dse_runner
from xeda.flow_runner.dse.dse_runner import Dse, Executioner
from xeda.flow_runner.trace import as_recorded

from .io_flows import _Place, _Taker
from .test_dse_run import _DeclaredOptimizer

PART = "LFE5U-25F-6BG256C"
OTHER_PART = "LFE5U-85F-6BG381C"

#: Where the design writes the device: in the requested flow's section, or in its producer's alone.
SECTIONS = {
    "requested": {"__place": {"fpga": PART}},
    "producer": {"__synth": {"fpga": PART}},
}
NODES = ["__synth", "__place"]


@pytest.fixture(autouse=True)
def start_in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _design(root: Path, where: str, sections=None) -> Design:
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": [], "top": "t"},
        flow=copy.deepcopy(SECTIONS[where] if sections is None else sections),
    )


def _planned(root: Path, design) -> list[tuple[str, str]]:
    """The nodes `plan` names, with their identities."""
    plan = DefaultRunner(root / "planned", display_results=False).plan(_Place, design)
    return [(node.name, node.flowrun_hash) for node in plan.nodes]


def _planned_with(root: Path, design, settings) -> list[tuple[str, str]]:
    """The nodes `plan` names when the command line gives `settings`."""
    plan = DefaultRunner(root / "planned_with", display_results=False).plan(
        _Place, design, flow_settings=settings
    )
    return [(node.name, node.flowrun_hash) for node in plan.nodes]


def _launched(runner: DefaultRunner) -> list[tuple[str, str]]:
    return [(flow.name, flow.flow_hash) for flow in runner.launched]


DOORS = {
    "run": lambda runner, design: runner.run(_Place, design),
    "run_flow": lambda runner, design: runner.run_flow(_Place, design, {}),
    "launch_flow": lambda runner, design: runner.launch_flow(_Place, design, {}),
}


@pytest.mark.parametrize("where", SECTIONS)
def test_the_device_is_planned_from_the_design_alone(tmp_path, where):
    """The premise of the rest: nothing but the design's own section gives the device."""
    design = _design(tmp_path, where)
    planned = _planned(tmp_path, design)
    assert [name for name, _ in planned] == NODES
    with pytest.raises(FlowSettingsException, match="needs `fpga`"):
        DefaultRunner(tmp_path / "bare", display_results=False).plan(
            _Place, _design(tmp_path, where, sections={})
        )


@pytest.mark.parametrize("where", SECTIONS)
@pytest.mark.parametrize("door", DOORS)
def test_a_launch_runs_the_nodes_planning_names(tmp_path, where, door):
    design = _design(tmp_path, where)
    before = copy.deepcopy(design.flow)
    planned = _planned(tmp_path, design)
    runner = DefaultRunner(tmp_path / door, display_results=False)

    flow = DOORS[door](runner, design)

    assert flow is not None and flow.succeeded
    assert _launched(runner) == planned
    assert flow.settings.fpga.part == PART
    assert design.flow == before, "composing a launch edits no section of the design"


@pytest.mark.parametrize("where", SECTIONS)
def test_resolving_a_built_design_gives_the_plan_planning_does(tmp_path, where):
    design = _design(tmp_path, where)
    planned = DefaultRunner(tmp_path / "planned", display_results=False).plan(_Place, design)
    resolved = DefaultRunner(tmp_path / "resolved", display_results=False).resolve(
        _Place, design, {}
    )

    assert [(n.name, n.flowrun_hash, n.settings_hash) for n in resolved.nodes] == [
        (n.name, n.flowrun_hash, n.settings_hash) for n in planned.nodes
    ]
    for name in NODES:
        assert as_recorded(resolved.node(name).settings) == as_recorded(planned.node(name).settings)


@pytest.mark.parametrize("where", SECTIONS)
def test_the_command_line_launches_the_nodes_planning_names(tmp_path, where):
    path = tmp_path / "design.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": [], "top": "t"},
                "flows": copy.deepcopy(SECTIONS[where]),
            }
        )
    )
    planned = _planned(tmp_path, path)

    result = CliRunner().invoke(cli, ["run", "__place", str(path), "--json"])

    document = json.loads(result.stdout)
    assert result.exit_code == 0 and document["success"], document
    launched = [
        (node["flow"], json.loads(Path(node["run_path"], "results.json").read_text())["flow_hash"])
        for node in document["nodes"]
    ]
    assert launched == planned


@pytest.mark.parametrize("where", SECTIONS)
def test_an_exploration_resolves_the_nodes_planning_names(tmp_path, monkeypatch, where):
    """`Dse.run_flow` resolves the plan its candidates launch, before it writes a log."""

    class Stop(Exception):
        pass

    def stop(*args, **kwargs):
        raise Stop

    design = _design(tmp_path, where)
    planned = _planned(tmp_path, design)
    runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=1)
    plans = []
    resolve = runner.resolve

    def resolved(*args, **kwargs):
        plans.append(resolve(*args, **kwargs))
        return plans[-1]

    monkeypatch.setattr(runner, "resolve", resolved)
    monkeypatch.setattr(dse_runner, "add_file_logger", stop)
    with pytest.raises(Stop):
        runner.run_flow(_Place, design, {})

    # the requested node carries the exploration's own adjustments (it is quiet, and bounded in
    # time), so its identity differs from the plan's; the producer is exactly the planned one
    (producer,) = (n for n in plans[-1].nodes if n.name == "__synth")
    assert (producer.name, producer.flowrun_hash) in planned
    assert plans[-1].node("__place").settings.fpga.part == PART


@pytest.mark.parametrize("where", SECTIONS)
def test_an_exploration_worker_launches_the_nodes_planning_names(tmp_path, where):
    """A worker is handed no request: it resolves each candidate from the design it was given."""
    design = _design(tmp_path, where)
    planned = _planned(tmp_path, design)
    runner = DefaultRunner(tmp_path / "worker", display_results=False)

    outcome, index = Executioner(runner, design, _Place)((7, {}))

    assert outcome is not None and index == 7
    assert _launched(runner) == planned


@pytest.mark.parametrize("door", DOORS)
def test_the_caller_s_settings_win_over_the_design_s_sections(tmp_path, door):
    """defaults < the design < what the caller gives: the design's device is overridden, and the
    overriding value reaches the producer along the declared edge."""
    design = _design(tmp_path, "requested")
    runner = DefaultRunner(tmp_path / door, display_results=False)
    launches = {
        "run": lambda: runner.run(_Place, design, flow_overrides={"fpga": OTHER_PART}),
        "run_flow": lambda: runner.run_flow(_Place, design, {"fpga": OTHER_PART}),
        "launch_flow": lambda: runner.launch_flow(_Place, design, {"fpga": OTHER_PART}),
    }

    flow = launches[door]()

    assert flow is not None and flow.succeeded
    assert flow.settings.fpga.part == OTHER_PART
    assert [f.settings.fpga.part for f in runner.launched] == [OTHER_PART, OTHER_PART]


@pytest.mark.parametrize("door", ["run_flow", "launch_flow"])
def test_sections_handed_to_a_launch_win_over_the_design_s(tmp_path, door):
    design = _design(tmp_path, "requested")
    runner = DefaultRunner(tmp_path / door, display_results=False)
    sections = {"__place": {"fpga": OTHER_PART}}

    flow = getattr(runner, door)(_Place, design, {}, all_flows_settings=sections)

    assert flow is not None and flow.settings.fpga.part == OTHER_PART


@pytest.mark.parametrize("door", ["run_flow", "launch_flow"])
def test_handing_a_launch_the_design_s_own_sections_changes_nothing(tmp_path, door):
    """Callers worked around the gap by passing `design.flow` as the sections: still the same
    launch."""
    design = _design(tmp_path, "producer")
    planned = _planned(tmp_path, design)
    runner = DefaultRunner(tmp_path / door, display_results=False)

    getattr(runner, door)(_Place, design, {}, all_flows_settings=design.flow)

    assert _launched(runner) == planned


def _bound_design(root: Path) -> Design:
    """A design that writes a binding, and the settings of the producer it names, in its own
    sections: `__taker` reads `__input_maker`'s file, not the default producer's."""
    (root / "in.txt").write_text("bound\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": [], "top": "t"},
        flow={
            "__taker": {"inputs": {"made": "__input_maker.made"}},
            "__input_maker": {"input_file": str(root / "in.txt")},
        },
    )


BOUND = ["__input_maker", "__taker"]


def test_resolving_a_design_that_writes_a_binding_follows_it(tmp_path):
    design = _bound_design(tmp_path)
    planned = DefaultRunner(tmp_path / "planned", display_results=False).plan(_Taker, design)
    resolved = DefaultRunner(tmp_path / "resolved", display_results=False).resolve(
        _Taker, design, {}
    )

    assert [n.name for n in planned.nodes] == BOUND
    assert [(n.name, n.flowrun_hash) for n in resolved.nodes] == [
        (n.name, n.flowrun_hash) for n in planned.nodes
    ]


@pytest.mark.parametrize("door", ["run", "run_flow", "launch_flow"])
def test_a_binding_the_design_writes_is_followed_by_every_door(tmp_path, door):
    design = _bound_design(tmp_path)
    planned = DefaultRunner(tmp_path / "planned", display_results=False).plan(_Taker, design)
    runner = DefaultRunner(tmp_path / door, display_results=False)
    launches = {
        "run": lambda: runner.run(_Taker, design),
        "run_flow": lambda: runner.run_flow(_Taker, design, {}),
        "launch_flow": lambda: runner.launch_flow(_Taker, design, {}),
    }

    flow = launches[door]()

    assert flow is not None and flow.results["read"] == "bound\n"
    assert _launched(runner) == [(n.name, n.flowrun_hash) for n in planned.nodes]


@pytest.mark.parametrize("door", ["run_flow", "launch_flow"])
def test_a_binding_the_caller_gives_wins_over_the_design_s(tmp_path, door):
    design = _bound_design(tmp_path)
    runner = DefaultRunner(tmp_path / door, display_results=False)

    flow = getattr(runner, door)(_Taker, design, {"inputs": {"made": "__maker.made"}})

    assert flow is not None and flow.results["read"] == "made\n"
    assert [f.name for f in runner.launched] == ["__maker", "__taker"]


@pytest.mark.parametrize("door", ["run_flow", "launch_flow"])
def test_settings_given_as_a_model_leave_the_design_s_sections_in_force(tmp_path, door):
    """A `Flow.Settings` instance contributes what it was given, not its defaults: the device
    still comes from the design's section."""
    design = _design(tmp_path, "producer")
    runner = DefaultRunner(tmp_path / door, display_results=False)

    flow = getattr(runner, door)(_Place, design, _Place.Settings(verbose=1))

    assert flow is not None and flow.succeeded
    assert flow.settings.verbose == 1 and flow.settings.fpga.part == PART
    assert _launched(runner) == _planned_with(tmp_path, design, {"verbose": 1})
