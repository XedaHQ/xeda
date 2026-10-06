"""The outputs `vivado_synth` writes in project mode, and the flows that read them.

A project run writes its outputs from the `TCL.POST` hooks it attaches to Vivado's steps, which
Vivado's own runs source after the step: the synthesis checkpoint after `synth_design`; the
routed checkpoint, the netlists, the SDF corners and the exported constraints after
`route_design`; the bitstream, which Vivado's `write_bitstream` step writes, is put at the
requested path after that step. The fake Vivado records the project script -- the hooks it
attaches, the step each run is launched to -- and runs the runs' steps and hooks too
(`TCL_MODEL`), but records what every hook ran in one file. So these tests source each attached
hook again as its run would, in step order, each in a directory of its own, and check that every
output the flow registers is written by the hook of its step, at the registered path.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from xeda import Design
from xeda.digest import content_digest
from xeda.flow import FPGA, FlowFatalError
from xeda.flow.io import declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoSynth
from xeda.flows.vivado import vivado_synth as vs
from xeda.flows.vivado.vivado_postsynthsim import VivadoPostsynthSim
from xeda.flows.vivado.vivado_power import VivadoPower
from xeda.flows.vivado.vivado_sim import VivadoSim

from .tool_utils import FAKE_TOOLS_DIR, fake_calls, use_fake_tools

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
PART = "xc7a12tcsg325-1"

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)

#: The steps xeda hooks, in the order Vivado runs them, with the run each belongs to.
HOOKED_STEPS = [
    ("synth_1", "SYNTH_DESIGN"),
    ("impl_1", "PLACE_DESIGN"),
    ("impl_1", "PHYS_OPT_DESIGN"),
    ("impl_1", "ROUTE_DESIGN"),
    ("impl_1", "WRITE_BITSTREAM"),
]
IMPL_STEPS = [step for run, step in HOOKED_STEPS if run == "impl_1"]

LABELS = (
    vs.CHECKPOINT_SYNTH,
    vs.CHECKPOINT_ROUTE,
    vs.NETLIST,
    vs.NETLIST_TIMING,
    vs.SDF_MIN,
    vs.SDF_MAX,
    vs.XDC_EXPORTED,
    vs.BITSTREAM,
)


def _synth(tmp_path: Path, monkeypatch, design: Optional[Design] = None, **settings) -> VivadoSynth:
    """Run `vivado_synth` on the fake Vivado."""
    use_fake_tools(monkeypatch)
    flow = DefaultRunner(tmp_path / "run").run_flow(
        VivadoSynth,
        design or Design.from_file(SQRT),
        {"fpga": PART, "clock_period": 5.5, **settings},
    )
    assert isinstance(flow, VivadoSynth) and flow.succeeded
    return flow


def _sqrt_with_an_hdl_testbench(root: Path) -> Design:
    """`sqrt` with a plain VHDL testbench: its own is a cocotb one, which the Vivado simulator --
    and so `vivado_postsynth_sim` and `vivado_power` -- cannot run."""
    root.mkdir(parents=True)
    (root / "tb_sqrt.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity tb_sqrt is end;\n"
        "architecture sim of tb_sqrt is\n"
        "  signal clk, rst, in_valid, out_ready: std_logic := '0';\n"
        "  signal in_data: std_logic_vector(31 downto 0) := (others => '0');\n"
        "begin\n"
        "  clk <= not clk after 5 ns;\n"
        "  uut: entity work.sqrt generic map(G_IN_WIDTH => 32)\n"
        "    port map(clk => clk, rst => rst, in_data => in_data, in_valid => in_valid,\n"
        "             out_ready => out_ready);\n"
        "end;\n"
    )
    return Design(
        name="sqrt",
        design_root=root,
        rtl={
            "sources": [str(SQRT.parent / "sqrt.vhdl")],
            "top": "sqrt",
            "clock": {"port": "clk"},
            "parameters": {"G_IN_WIDTH": 32},
        },
        tb={"sources": ["tb_sqrt.vhd"], "top": "tb_sqrt", "uut": "uut"},
    )


def _registered(flow: VivadoSynth) -> Dict[str, Path]:
    """The project outputs `flow` registered, as recorded."""
    return {label: Path(flow.artifacts[label]) for label in LABELS if label in flow.artifacts}


def _attached_hooks(calls: List[List[str]]) -> Dict[str, Tuple[str, Path]]:
    """`{step: (run, hook)}`: the `TCL.POST` hook the project script attached to each step."""
    hooks = {}
    for previous, call in zip(calls, calls[1:]):
        if call[:2] == ["set_property", "-name"] and call[2].endswith(".TCL.POST"):
            # `set_property -name {STEPS.<STEP>.TCL.POST} -value {<hook>} -objects [get_runs <run>]`:
            # `get_runs` is evaluated, and recorded, first
            assert previous[0] == "get_runs", previous
            step = call[2].split(".")[1]
            hooks[step] = (previous[1], Path(call[call.index("-value") + 1]))
    return hooks


def _launched_to(calls: List[List[str]], run: str) -> Optional[str]:
    """The `-to_step` the project script launched `run` to (None: all its steps)."""
    (launch,) = [call for call in calls if call[:2] == ["launch_runs", run]]
    return launch[launch.index("-to_step") + 1] if "-to_step" in launch else None


def _source_hooks(flow: VivadoSynth, work: Path) -> Dict[str, List[List[str]]]:
    """Source each hook the project script attached, as Vivado's runs would: synth_1's, then
    impl_1's in step order up to the step impl_1 was launched to. Each is sourced in a directory
    of its own -- the run directory Vivado sources it in -- where the fake records its commands,
    which are returned by step. Vivado's `write_bitstream` step leaves `<top>.bit` and `.bin` in
    its run directory before its hook runs; so does this."""
    calls = fake_calls(flow.run_path)
    hooks = _attached_hooks(calls)
    to_step = (_launched_to(calls, "impl_1") or IMPL_STEPS[-1]).upper()
    top = flow.design.rtl.top
    recorded: Dict[str, List[List[str]]] = {}
    for run, step in HOOKED_STEPS:
        if run == "impl_1" and IMPL_STEPS.index(step) > IMPL_STEPS.index(to_step):
            break
        if step not in hooks:
            continue
        run_dir = work / step.lower()
        run_dir.mkdir(parents=True)
        if step == "WRITE_BITSTREAM":
            (run_dir / f"{top}.bit").write_text("bit")
            (run_dir / f"{top}.bin").write_text("bin")
        assert _source_hook(hooks[step][1], run_dir, top) == 0, step
        recorded[step] = fake_calls(run_dir)
    return recorded


def _source_hook(hook: Path, run_dir: Path, top: str, slack: float = 0.0) -> int:
    """Source `hook` on the fake Vivado in `run_dir`, answering what the hooks ask Vivado for:
    no messages, no timing paths, the design's top and the given worst slack. Its exit status."""
    driver = run_dir / "driver.tcl"
    driver.write_text(
        "proc get_msg_config {args} {return 0}\n"
        "proc get_timing_paths {args} {return {}}\n"
        "proc get_property {name args} {\n"
        f"  switch -- $name {{ TOP {{return {{{top}}}}} SLACK {{return {slack}}} }}\n"
        "  return 0\n"
        "}\n"
        f"__source {{{hook}}}\n"
    )
    vivado = FAKE_TOOLS_DIR / "vivado"
    return subprocess.run([str(vivado), "-source", str(driver)], cwd=run_dir).returncode


def _writes(recorded: Dict[str, List[List[str]]]) -> Dict[str, List[List[str]]]:
    return {
        step: [c for c in calls if c[0].startswith("write_")] for step, calls in recorded.items()
    }


def test_the_output_labels_are_the_ones_every_vivado_flow_uses() -> None:
    assert LABELS == (
        "checkpoint_synth",
        "checkpoint_route",
        "netlist",
        "netlist_timing",
        "sdf_min",
        "sdf_max",
        "xdc_exported",
        "bitstream",
    )


@needs_tclsh
@pytest.mark.parametrize("write_checkpoint", [False, True], ids=["no_checkpoints", "checkpoints"])
def test_each_registered_output_is_written_by_its_steps_hook(
    tmp_path, monkeypatch, write_checkpoint
) -> None:
    flow = _synth(
        tmp_path,
        monkeypatch,
        write_netlist=True,
        write_timing_netlist=True,
        write_checkpoint=write_checkpoint,
        bitstream="outputs/sqrt.bit",
    )
    # label: the step whose hook writes it, with which command, where (in the run directory)
    route = "outputs/route_design"
    expected = {
        vs.NETLIST: ("ROUTE_DESIGN", "write_verilog", f"{route}/funcsim.v"),
        vs.NETLIST_TIMING: ("ROUTE_DESIGN", "write_verilog", f"{route}/timesim.v"),
        vs.SDF_MIN: ("ROUTE_DESIGN", "write_sdf", f"{route}/timesim.min.sdf"),
        vs.SDF_MAX: ("ROUTE_DESIGN", "write_sdf", f"{route}/timesim.max.sdf"),
        vs.XDC_EXPORTED: ("ROUTE_DESIGN", "write_xdc", f"{route}/impl.xdc"),
        # written by Vivado's own step, put in place by the hook that runs after it
        vs.BITSTREAM: ("WRITE_BITSTREAM", None, "outputs/sqrt.bit"),
    }
    if write_checkpoint:
        expected[vs.CHECKPOINT_SYNTH] = (
            "SYNTH_DESIGN",
            "write_checkpoint",
            "outputs/synth_design/post_synth.dcp",
        )
        expected[vs.CHECKPOINT_ROUTE] = (
            "ROUTE_DESIGN",
            "write_checkpoint",
            f"{route}/post_route.dcp",
        )
    # registered relative to the run directory
    assert _registered(flow) == {label: Path(path) for label, (_, _, path) in expected.items()}

    calls = fake_calls(flow.run_path)
    assert {step: run for step, (run, _) in _attached_hooks(calls).items()} == dict(
        (step, run) for run, step in HOOKED_STEPS
    )
    assert _launched_to(calls, "impl_1") == "write_bitstream"

    writes = _writes(_source_hooks(flow, tmp_path / "runs"))
    for label, (step, command, path) in expected.items():
        target = flow.run_path / path
        if command is None:
            assert target.read_text() == "bit", label
        else:
            assert [c for c in writes[step] if c[0] == command and Path(c[-1]) == target], label
    written = sorted(Path(c[-1]) for calls in writes.values() for c in calls)
    assert written == sorted(flow.run_path / p for _, command, p in expected.values() if command)
    corners = [
        (c[c.index("-process_corner") + 1], Path(c[-1]))
        for c in writes["ROUTE_DESIGN"]
        if c[0] == "write_sdf"
    ]
    assert corners == [
        ("fast", flow.run_path / expected[vs.SDF_MIN][2]),
        ("slow", flow.run_path / expected[vs.SDF_MAX][2]),
    ]


def test_without_a_bitstream_the_implementation_run_stops_after_routing(
    tmp_path, monkeypatch
) -> None:
    flow = _synth(tmp_path, monkeypatch, write_netlist=True)
    calls = fake_calls(flow.run_path)
    assert _launched_to(calls, "impl_1") == "route_design"
    assert "WRITE_BITSTREAM" not in _attached_hooks(calls)
    assert vs.BITSTREAM not in flow.artifacts


@needs_tclsh
def test_the_write_bitstream_step_keeps_its_own_settings(tmp_path, monkeypatch) -> None:
    """`impl.steps.WRITE_BITSTREAM` reaches Vivado's step, which the implementation run now goes
    through: its arguments (`BIN_FILE`: a .bin beside the .bit), and a user's own `TCL.POST`,
    which the generated hook sources before putting the bitstream in place."""
    user_hook = tmp_path / "user post.tcl"
    user_hook.write_text("puts user\n")
    flow = _synth(
        tmp_path,
        monkeypatch,
        bitstream="outputs/sqrt.bit",
        impl={
            "steps": {
                "WRITE_BITSTREAM": {"ARGS": {"BIN_FILE": True}, "TCL": {"POST": str(user_hook)}}
            }
        },
    )
    calls = fake_calls(flow.run_path)
    assert [
        c
        for c in calls
        if c[:3] == ["set_property", "-name", "STEPS.WRITE_BITSTREAM.ARGS.BIN_FILE"]
    ]
    run, hook = _attached_hooks(calls)["WRITE_BITSTREAM"]
    assert run == "impl_1" and hook.parent == flow.run_path

    recorded = _source_hooks(flow, tmp_path / "runs")
    assert ["source", str(user_hook)] in recorded["WRITE_BITSTREAM"]
    assert (flow.run_path / "outputs" / "sqrt.bit").read_text() == "bit"
    assert (flow.run_path / "outputs" / "sqrt.bin").read_text() == "bin"


@needs_tclsh
def test_a_bitstream_given_as_a_location_is_registered_in_the_run_directory_and_delivered(
    tmp_path, monkeypatch
) -> None:
    """A bitstream named by a location is written under the conventional name in the run
    directory, registered there, and copied to the location once the launch succeeded. A later
    run whose `write_bitstream` step fails neither reports a bitstream as its own nor touches
    the earlier delivery."""
    bitstream = tmp_path / "bits" / "sqrt.bit"
    flow = _synth(tmp_path, monkeypatch, bitstream=str(bitstream))
    assert _registered(flow) == {vs.BITSTREAM: Path("outputs/sqrt.bit")}
    written = flow.run_path / "outputs" / "sqrt.bit"
    assert written.is_file()
    assert bitstream.read_bytes() == written.read_bytes()
    delivered, content = bitstream.stat(), bitstream.read_bytes()

    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "write_bitstream")
    failed = DefaultRunner(tmp_path / "run", rebuild_all=True).run_flow(
        VivadoSynth,
        Design.from_file(SQRT),
        {"fpga": PART, "clock_period": 5.5, "bitstream": str(bitstream)},
    )
    assert failed is not None and not failed.succeeded
    assert bitstream.stat().st_ino == delivered.st_ino, "the earlier delivery is where it was"
    assert bitstream.read_bytes() == content
    assert vs.BITSTREAM not in failed.results.artifacts


def test_hooks_render_only_their_own_steps_writes_and_leave_active_step_to_vivado(
    tmp_path, monkeypatch
) -> None:
    """`ACTIVE_STEP` is Vivado's: its runs set it before each step and unset it after."""
    flow = _synth(
        tmp_path,
        monkeypatch,
        write_netlist=True,
        write_timing_netlist=True,
        write_checkpoint=True,
        bitstream="outputs/sqrt.bit",
        qor_suggestions=True,
    )
    route_only = ("write_verilog", "write_sdf", "write_xdc", "report_route_status", "write_qor")
    for step, (_, hook) in _attached_hooks(fake_calls(flow.run_path)).items():
        text = hook.read_text()
        assert "ACTIVE_STEP" not in text, step
        if step != "ROUTE_DESIGN":
            assert not [command for command in route_only if command in text], step
        if step != "SYNTH_DESIGN" and step != "ROUTE_DESIGN":
            assert "write_checkpoint" not in text, step


@needs_tclsh
@pytest.mark.parametrize("fail_timing", [True, False], ids=["fail_timing", "no_fail_timing"])
def test_a_timing_failure_fails_the_route_step_only_with_fail_timing(
    tmp_path, monkeypatch, fail_timing
) -> None:
    """The route hook runs before Vivado's `write_bitstream` step: failing it on a negative slack
    stops the run there. Without `fail_timing`, the run goes on to write the bitstream."""
    flow = _synth(tmp_path, monkeypatch, bitstream="outputs/sqrt.bit", fail_timing=fail_timing)
    _, hook = _attached_hooks(fake_calls(flow.run_path))["ROUTE_DESIGN"]
    run_dir = tmp_path / "route_design"
    run_dir.mkdir()
    status = _source_hook(hook, run_dir, flow.design.rtl.top, slack=-0.5)
    assert (status != 0) == fail_timing
    assert ("Failed to meet timing" in (run_dir / "fake_vivado.calls").read_text()) == fail_timing


@needs_tclsh
def test_qor_suggestions_are_written_under_the_run_directorys_reports(
    tmp_path, monkeypatch
) -> None:
    flow = _synth(tmp_path, monkeypatch, qor_suggestions=True)
    route = _source_hooks(flow, tmp_path / "runs")["ROUTE_DESIGN"]
    (call,) = [c for c in route if c[0] == "write_qor_suggestions"]
    reports = flow.run_path / "reports" / "route_design"
    assert Path(call[call.index("-strategy_dir") + 1]) == reports / "strategy_suggestions"
    assert Path(call[-1]) == reports / "qor_suggestions.rqs"


def test_a_missing_dependency_output_is_a_fatal_error_naming_it(tmp_path, monkeypatch) -> None:
    synth = _synth(tmp_path, monkeypatch)  # no `write_netlist`: no netlist registered
    with pytest.raises(FlowFatalError, match=r"vivado_synth.*`netlist`"):
        vs.artifact_path(synth, vs.NETLIST)


def _write_registered(flow: VivadoSynth) -> None:
    """Create the files `flow` registered, as the real Vivado would have written them."""
    for path in _registered(flow).values():
        path = flow.run_path / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


@pytest.mark.parametrize("timing_sim", [False, True], ids=["functional", "timing"])
def test_postsynth_sim_simulates_what_its_synthesis_registered(
    tmp_path, monkeypatch, timing_sim
) -> None:
    design = _sqrt_with_an_hdl_testbench(tmp_path / "design")
    synth = _synth(tmp_path, monkeypatch, design, write_netlist=True, write_timing_netlist=True)
    _write_registered(synth)
    settings = VivadoPostsynthSim.Settings(
        synth=VivadoSynth.Settings(fpga=FPGA(part=PART)), timing_sim=timing_sim
    )  # type: ignore
    sim = VivadoPostsynthSim(settings, design, tmp_path / "sim")  # type: ignore
    sim.completed_dependencies.append(synth)
    sim.init()
    observed = {}

    def capture_run(self):
        observed["sources"] = [src.file for src in self.design.rtl.sources]
        observed["sdf"] = list(self.settings.sdf.delay_items())
        observed["libs"] = [name for name, _ in self.settings.lib_paths]

    monkeypatch.setattr(VivadoSim, "run", capture_run)
    sim.run()
    netlist = vs.NETLIST_TIMING if timing_sim else vs.NETLIST
    assert observed["sources"] == [synth.run_path / synth.artifacts[netlist]]
    sdf = [("max", synth.run_path / synth.artifacts[vs.SDF_MAX])]
    assert observed["sdf"] == (sdf if timing_sim else [])
    # the functional netlist instantiates UNISIM primitives, the timing one SIMPRIM primitives
    assert observed["libs"] == (["simprims_ver"] if timing_sim else ["unisims_ver", "simprims_ver"])


@needs_tclsh
def test_power_reads_the_checkpoint_and_activity_its_dependencies_registered(
    tmp_path, monkeypatch
) -> None:
    design = _sqrt_with_an_hdl_testbench(tmp_path / "design")
    synth = _synth(
        tmp_path,
        monkeypatch,
        design,
        write_netlist=True,
        write_timing_netlist=True,
        write_checkpoint=True,
    )
    _write_registered(synth)
    post_settings = VivadoPostsynthSim.Settings(synth=VivadoSynth.Settings(fpga=FPGA(part=PART)))  # type: ignore
    power = VivadoPower(VivadoPower.Settings(postsynthsim=post_settings), design, tmp_path / "power")  # type: ignore
    power.init()
    ((_, post_settings, _),) = power.dependencies
    assert post_settings.synth.write_checkpoint
    # the simulation, on the fake Vivado, records the activity file it writes (`saif`)
    post = VivadoPostsynthSim(post_settings, design, tmp_path / "post")
    post.completed_dependencies.append(synth)
    post.init()
    power.completed_dependencies.append(post)
    for flow in (post, power):
        flow.run_path.mkdir()
        monkeypatch.chdir(flow.run_path)  # where the runner runs a flow's tools
        flow.run()
        if flow is post:
            # Complete the activity verdict as the launcher does before handing it to power.
            post.results.success = post.check_results()
            assert post.succeeded
    assert ["open_saif", str(power.settings.saif)] in fake_calls(post.run_path)
    calls = fake_calls(power.run_path)
    assert ["open_checkpoint", str(synth.run_path / synth.artifacts[vs.CHECKPOINT_ROUTE])] in calls
    assert Path(post.artifacts["saif"]) == Path(power.settings.saif)
    assert ["read_saif", "-verbose", str(post.run_path / power.settings.saif)] in calls


# --- the declared outputs (PC Task 2, R-PC-a) ---------------------------------------------------

#: Which declared output each switch turns on, on each flow (`41-plan-pc.md` 3.1a, read per
#: flow: `vivado_alt_synth` writes one SDF corner and declares no `sdf_min`).
SWITCHED_OUTPUTS = {
    VivadoSynth: {
        "write_netlist": {"netlist"},
        "write_timing_netlist": {"netlist_timing", "sdf", "sdf_min"},
        "write_checkpoint": {"checkpoint_synth", "checkpoint_route"},
        "bitstream": {"bitstream"},
    },
    VivadoAltSynth: {
        "write_netlist": {"netlist"},
        "write_timing_netlist": {"netlist_timing", "sdf"},
        "write_checkpoint": {"checkpoint_synth", "checkpoint_route"},
        "bitstream": {"bitstream"},
    },
}
#: A value that turns each switch on (`bitstream` is a path, the others are flags).
SWITCH_ON = {
    "write_netlist": True,
    "write_timing_netlist": True,
    "write_checkpoint": True,
    "bitstream": "outputs/sqrt.bit",
}
FLOW_IDS = {VivadoSynth: "vivado_synth", VivadoAltSynth: "vivado_alt_synth"}


def _run(flow_class, tmp_path: Path, monkeypatch, **settings):
    use_fake_tools(monkeypatch)
    flow = DefaultRunner(tmp_path / "run").run_flow(
        flow_class, Design.from_file(SQRT), {"fpga": PART, "clock_period": 5.5, **settings}
    )
    assert isinstance(flow, flow_class) and flow.succeeded
    return flow


def _recorded_outputs(flow) -> dict:
    """The `outputs` of the run's `results.json`, as written to disk."""
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["outputs"] == flow.results["outputs"]
    return saved["outputs"]


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
def test_each_flow_declares_the_outputs_the_plan_assigns_it(flow_class) -> None:
    declared = {
        name: declaration.enabled_by for name, declaration in declared_outputs(flow_class).items()
    }
    expected = {
        output: switch
        for switch, outputs in SWITCHED_OUTPUTS[flow_class].items()
        for output in outputs
    }
    assert declared == expected
    # one SDF corner for the alternative flow: `sdf_min` is `vivado_synth`'s alone (PCD18)
    assert ("sdf_min" in declared) == (flow_class is VivadoSynth)


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
@pytest.mark.parametrize("switch", SWITCH_ON)
def test_each_switch_enables_exactly_the_outputs_the_plan_assigns_it(
    flow_class, switch, tmp_path, monkeypatch
) -> None:
    """R-PC-a's table: `enabled_by` setting -> outputs, on both flows. The record is the run's
    own: path inside the run directory, digest of the file as written."""
    flow = _run(flow_class, tmp_path, monkeypatch, **{switch: SWITCH_ON[switch]})
    recorded = _recorded_outputs(flow)
    assert set(recorded) == SWITCHED_OUTPUTS[flow_class][switch]
    for name, entry in recorded.items():
        path = Path(entry["path"])
        assert path.is_file() and path.is_relative_to(flow.run_path.resolve()), name
        assert entry["sha"] == content_digest(path), name


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
def test_write_netlist_alone_writes_the_functional_netlist_and_constraints_and_no_sdf(
    flow_class, tmp_path, monkeypatch
) -> None:
    flow = _run(flow_class, tmp_path, monkeypatch, write_netlist=True)
    written = [call for call in fake_calls(flow.run_path) if call[0].startswith("write_")]
    modes = [c[c.index("-mode") + 1] for c in written if c[0] == "write_verilog"]
    assert modes == ["funcsim"]
    assert [c[0] for c in written if c[0] in ("write_sdf", "write_xdc")] == ["write_xdc"]
    assert {vs.NETLIST, vs.XDC_EXPORTED} <= set(flow.artifacts)
    assert not {vs.NETLIST_TIMING, vs.SDF_MIN, vs.SDF_MAX, vs.SDF} & set(flow.artifacts)


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
def test_write_timing_netlist_alone_writes_the_timing_netlist_and_sdf_and_no_functional_one(
    flow_class, tmp_path, monkeypatch
) -> None:
    flow = _run(flow_class, tmp_path, monkeypatch, write_timing_netlist=True)
    written = [call for call in fake_calls(flow.run_path) if call[0].startswith("write_")]
    modes = [c[c.index("-mode") + 1] for c in written if c[0] == "write_verilog"]
    assert modes == ["timesim"]
    corners = [c[c.index("-process_corner") + 1] for c in written if c[0] == "write_sdf"]
    assert corners == (["fast", "slow"] if flow_class is VivadoSynth else ["slow"])
    assert not [c for c in written if c[0] == "write_xdc"]
    assert vs.NETLIST not in flow.artifacts and vs.XDC_EXPORTED not in flow.artifacts
    assert vs.NETLIST_TIMING in flow.artifacts


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
def test_both_netlist_switches_together_record_both_halves(
    flow_class, tmp_path, monkeypatch
) -> None:
    flow = _run(flow_class, tmp_path, monkeypatch, write_netlist=True, write_timing_netlist=True)
    switches = SWITCHED_OUTPUTS[flow_class]
    assert (
        set(_recorded_outputs(flow)) == switches["write_netlist"] | switches["write_timing_netlist"]
    )
    # the plan's exit table counts the exported constraints too: a plain artifact, not an output
    assert vs.XDC_EXPORTED in flow.artifacts
    assert len(_recorded_outputs(flow)) + 1 == (5 if flow_class is VivadoSynth else 4)


def test_an_output_whose_switch_is_off_is_not_recorded(tmp_path, monkeypatch) -> None:
    flow = _run(VivadoSynth, tmp_path, monkeypatch)
    assert _recorded_outputs(flow) == {}


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
def test_a_consumer_can_switch_the_bitstream_on(flow_class) -> None:
    """`bitstream` has no default, so a consumer's demand needs `enable_output` to name one."""
    settings = flow_class.Settings(fpga=FPGA(part=PART))  # type: ignore
    assert settings.bitstream is None
    flow_class.enable_output(settings, "bitstream")
    assert settings.bitstream is not None
    assert settings.write_netlist is False and settings.write_timing_netlist is False


@pytest.mark.parametrize("flow_class", SWITCHED_OUTPUTS, ids=FLOW_IDS.get)
def test_a_bitstream_located_outside_the_run_directory_is_recorded_inside_it_and_delivered(
    flow_class, tmp_path, monkeypatch
) -> None:
    """`bitstream` is a deliverable: given a location, the run writes the conventional name in its
    run directory, the declared output is that file (never the location, which would fail the
    recording as outside the run directory), and the launch delivers a copy to the location."""
    target = tmp_path / "elsewhere" / "x.bit"
    flow = _run(flow_class, tmp_path, monkeypatch, bitstream=str(target))
    recorded = _recorded_outputs(flow)
    assert set(recorded) == {"bitstream"}
    path = Path(recorded["bitstream"]["path"])
    assert path.is_relative_to(flow.run_path.resolve()) and path != target.resolve()
    assert target.read_bytes() == path.read_bytes()
