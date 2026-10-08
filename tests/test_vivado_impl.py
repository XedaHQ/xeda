"""`vivado_impl`: Vivado's place and route of an EDIF netlist, and `yosys_fpga`'s EDIF output that
feeds it (`xeda run yosys_fpga+vivado_impl design.yaml`).

Planning needs no tool. Execution goes through the process fakes of `tests/fake_tools`: a yosys that
writes an EDIF netlist where its script says, and a Vivado whose Tcl runs under `tclsh` with its
commands recorded -- and whose `read_edif` and `link_design` find the top module of a netlist by
the name of its file, as Vivado does. The real tools run in `tests/test_vivado_real.py`.

Four hazards of handing a yosys netlist to Vivado: a bus written without its range comes out
reversed, and Vivado says nothing at all; a hierarchical netlist is not read as one design, and
Vivado stops with `[DRC INBB-3]`; the top of an EDIF netlist is found by the name of its file, and
Vivado stops with `[Project 1-68]`; and a Verilog netlist loses the contents of a block RAM that
has undefined bits, with one critical warning (`[Netlist 29-72]`) as the only trace. Each is
impossible or refused here, and the tests say how.
"""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import click
import pytest
import yaml

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowFatalError, FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoImpl, VivadoSynth, YosysFpga

from .test_chain_documentation import FLOWS_RST, _blocks, _stage
from .test_edif import netlist as edif_netlist
from .tool_utils import (
    FAKE_TOOLS_DIR,
    check_after_the_racy_window,
    fake_calls,
    fake_returns,
    launch_until_fresh,
    producers_of,
    require_yosys,
    use_fake_fpga_tools,
    use_fake_tools,
)

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)

PART = "xc7a35tcpg236-1"
BLINKY = (
    "module blinky(input clk, input rst, output [3:0] led);\n"
    "  reg [26:0] count = 0;\n"
    "  always @(posedge clk) count <= rst ? 0 : count + 1;\n"
    "  assign led = count[26:23];\n"
    "endmodule\n"
)
#: the same counter in a module of its own, which the HDL asks to keep: a hierarchy that no
#: setting shows
BLINKY_OVER_A_COUNTER = (
    "(* keep_hierarchy *)\n"
    "module counter(input clk, input rst, output [3:0] q);\n"
    "  reg [26:0] count = 0;\n"
    "  always @(posedge clk) count <= rst ? 0 : count + 1;\n"
    "  assign q = count[26:23];\n"
    "endmodule\n"
    "module blinky(input clk, input rst, output [3:0] led);\n"
    "  counter u_counter(.clk(clk), .rst(rst), .q(led));\n"
    "endmodule\n"
)
PINS = "set_property -dict {PACKAGE_PIN W5 IOSTANDARD LVCMOS33} [get_ports clk]\n"
TOP = "blinky"
#: the settings of a run: the device and a clock, given once, to the node that is requested
SETTINGS = {"fpga": PART, "clock": {"period": 10.0}}
#: an EDIF netlist as a design lists it, whatever its file is called
NETLIST = (
    "(edif other (edifVersion 2 0 0) (edifLevel 0) (keywordMap (keywordLevel 0))\n"
    "  (library DESIGN (edifLevel 0) (technology (numberDefinition))\n"
    "    (cell other (cellType GENERIC) (view VIEW_NETLIST (viewType NETLIST)\n"
    "      (interface) (contents))))\n"
    "  (design other (cellRef other (libraryRef DESIGN))))\n"
)


@pytest.fixture
def toolchain(tmp_path, monkeypatch) -> Path:
    """The fake FPGA tools first on `PATH`, with the fake Vivado behind them."""
    prefix = use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    return prefix


def _design(
    root: Path, sources=("blinky.v", "pins.xdc"), flows=None, top: str = TOP, attributes=None
) -> Design:
    """A design of one Verilog module and its pin constraints."""
    root.mkdir(exist_ok=True)
    (root / "blinky.v").write_text(BLINKY)
    (root / "pins.xdc").write_text(PINS)
    (root / "given.edf").write_text(NETLIST)
    rtl = {"sources": list(sources), "top": top, "clock": {"port": "clk"}}
    if attributes:
        rtl["attributes"] = attributes
    return Design(name="blinky", design_root=root, rtl=rtl, flow=flows or {})


def _edif_design(root: Path, **kwargs) -> Design:
    """The same design, with its netlist listed: an EDIF file named other than its top."""
    return _design(root, sources=({"file": "given.edf", "type": "Edif"}, "pins.xdc"), **kwargs)


def _runner(tmp_path: Path, **kwargs) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _plan(tmp_path: Path, request, design: Design, **kwargs):
    return _runner(tmp_path).plan(request, design, **kwargs)


# ------------------------------------------------------------------------------- the declarations


def test_vivado_impl_declares_one_edif_netlist_and_the_constraints_it_reads() -> None:
    inputs = declared_inputs(VivadoImpl)
    netlist, constraints = inputs["netlist"], inputs["constraints"]
    # only EDIF: a Verilog netlist loses the contents of a block RAM that has undefined bits
    assert netlist.types == (SourceType.Edif,)
    assert (netlist.cardinality, netlist.required) == ("one", True)
    assert (netlist.producer, netlist.output) == ("yosys_fpga", "netlist_edif")
    assert constraints.types == (SourceType.Xdc, SourceType.Sdc)
    assert (constraints.cardinality, constraints.required) == ("many", False)
    assert set(inputs) == {"netlist", "constraints"}
    (bitstream,) = declared_outputs(VivadoImpl).values()
    assert (bitstream.name, bitstream.types) == ("bitstream", (SourceType.Bitstream,))
    assert (bitstream.cardinality, bitstream.enabled_by) == ("optional", "bitstream")
    # it reads no RTL: the design's sources are the producer's concern
    assert VivadoImpl.reads_sources is None


def test_yosys_fpga_declares_its_edif_netlist_beside_the_json_one() -> None:
    outputs = declared_outputs(YosysFpga)
    assert outputs["netlist"].types == (SourceType.JsonNetlist,)
    edif = outputs["netlist_edif"]
    assert edif.types == (SourceType.Edif,)
    # no switch: a flat Xilinx synthesis always writes it, whoever asks, so one run serves all
    assert (edif.cardinality, edif.enabled_by) == ("optional", None)
    assert [o for o, d in outputs.items() if SourceType.Edif in d.types] == ["netlist_edif"]


# --------------------------------------------------------------------------------------- planning


def test_one_yosys_run_serves_nextpnr_and_vivado_impl(tmp_path) -> None:
    """The EDIF is written whenever the synthesis is flat, and asks for nothing: the synthesis
    is one configuration, one identity and one run directory, whichever toolchain follows."""
    design = _design(tmp_path / "d", flows={"yosys_fpga": SETTINGS})
    alone = _plan(tmp_path, "yosys_fpga", design).node("yosys_fpga")
    with_nextpnr = _plan(tmp_path, "yosys_fpga+nextpnr", design).node("yosys_fpga")
    with_vivado = _plan(tmp_path, "yosys_fpga+vivado_impl", design).node("yosys_fpga")
    assert alone.flowrun_hash == with_nextpnr.flowrun_hash == with_vivado.flowrun_hash
    assert alone.run_path == with_nextpnr.run_path == with_vivado.run_path
    assert alone.switched_on == with_nextpnr.switched_on == with_vivado.switched_on == ()
    assert with_vivado.settings.netlist_edif == Path("netlist.edif")


def test_vivado_impl_takes_the_edif_netlist_of_yosys_fpga_by_default(tmp_path) -> None:
    design = _design(tmp_path / "d", flows={"vivado_impl": SETTINGS})
    for request in ("vivado_impl", "yosys_fpga+vivado_impl"):
        plan = _plan(tmp_path, request, design)
        assert [node.name for node in plan.nodes] == ["yosys_fpga", "vivado_impl"]
        netlist = next(i for i in plan.node("vivado_impl").inputs if i.name == "netlist")
        assert (netlist.origin, netlist.producer, netlist.output) == (
            "producer",
            "yosys_fpga",
            "netlist_edif",
        )
        # the pin constraints are the design's, in source order
        constraints = next(i for i in plan.node("vivado_impl").inputs if i.name == "constraints")
        assert constraints.sources == (tmp_path / "d" / "pins.xdc",)
        # one clock and one device for both, from the one node that gave them
        for node in plan.nodes:
            assert node.settings.fpga.part == PART
            assert node.settings.main_clock.period == 10.0


def test_nextpnr_still_takes_the_json_netlist(tmp_path) -> None:
    """The EDIF output fits no input of nextpnr, and the JSON output none of vivado_impl: an
    unqualified chain is never ambiguous."""
    design = _design(tmp_path / "d", flows={"yosys_fpga": SETTINGS})
    plan = _plan(tmp_path, "yosys_fpga+nextpnr", design)
    netlist = next(i for i in plan.node("nextpnr").inputs if i.name == "netlist")
    assert netlist.output == "netlist"


def test_a_listed_edif_netlist_replaces_the_synthesis(tmp_path) -> None:
    design = _edif_design(tmp_path / "d", flows={"vivado_impl": SETTINGS})
    plan = _plan(tmp_path, "vivado_impl", design)
    assert [node.name for node in plan.nodes] == ["vivado_impl"]
    netlist = next(i for i in plan.node("vivado_impl").inputs if i.name == "netlist")
    assert (netlist.origin, netlist.sources) == ("source", (tmp_path / "d" / "given.edf",))
    # ... unless a chain asks for the synthesis
    chained = _plan(tmp_path, "yosys_fpga+vivado_impl", design)
    assert [node.name for node in chained.nodes] == ["yosys_fpga", "vivado_impl"]


def test_a_chain_to_the_loader_switches_the_bitstream_on_with_its_conventional_name(
    tmp_path,
) -> None:
    design = _design(tmp_path / "d", flows={"vivado_impl": SETTINGS})
    plan = _plan(tmp_path, "yosys_fpga+vivado_impl+openfpgaloader", design)
    node = plan.node("vivado_impl")
    assert node.switched_on == ("bitstream",)
    assert node.settings.bitstream == Path("outputs/blinky.bit")
    alone = _plan(tmp_path, "vivado_impl", design).node("vivado_impl")
    assert alone.settings.bitstream is None and alone.switched_on == ()


@pytest.mark.parametrize(
    "settings, cause",
    [
        pytest.param({"flatten": False}, "`flatten` is false", id="flatten_false"),
        pytest.param(
            {"synth_pass_only": True, "read_verilog_flags": [], "systemverilog": "default"},
            "`synth_pass_only` leaves `flatten` to the synthesis pass",
            id="synth_pass_only",
        ),
        pytest.param({"keep_hierarchy": ["blinky"]}, "`keep_hierarchy` keeps blinky", id="keep"),
        pytest.param(
            {"set_mod_attribute": {"keep_hierarchy": {"blinky": 1}}},
            "`set_mod_attribute` gives `keep_hierarchy` to blinky",
            id="mod_attribute",
        ),
        pytest.param(
            {"set_attribute": {"keep_hierarchy": {"u_core": 1}}},
            "`set_attribute` gives `keep_hierarchy` to u_core",
            id="attribute",
        ),
        pytest.param(
            {"black_box": ["blinky"]}, "`black_box` makes blinky a black box", id="black_box"
        ),
        pytest.param({"stop_after": "rtl"}, "`stop_after: rtl`", id="stop_after_rtl"),
        pytest.param({"fpga": "LFE5U-25F-6BG381C"}, "not a Xilinx device", id="not_xilinx"),
    ],
)
def test_no_edif_netlist_is_written_for_a_synthesis_vivado_cannot_read(
    tmp_path, settings, cause
) -> None:
    """A hierarchical netlist is not one design to Vivado (its modules are black boxes), and
    says so only at the end of a run: the synthesis writes no EDIF netlist then, `vivado_impl`
    is refused while planning, and the cause is named. The synthesis itself is not refused."""
    design = _design(tmp_path / "d", flows={"yosys_fpga": {**SETTINGS, **settings}})
    with pytest.raises(FlowSettingsException) as raised:
        _plan(tmp_path, "yosys_fpga+vivado_impl", design)
    message = str(raised.value)
    assert (
        "yosys_fpga.netlist_edif is required by a consumer: no EDIF netlist is written" in message
    )
    assert cause in message, message
    synthesis = _plan(tmp_path, "yosys_fpga", design).node("yosys_fpga")
    assert synthesis.settings.edif_problem() is not None  # planned, and it writes no EDIF


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"set_mod_attribute": {"keep_hierarchy": {}}}, id="no_module_kept"),
        pytest.param({"set_mod_attribute": {"keep": {"blinky": 1}}}, id="other_module_attribute"),
        pytest.param({"set_attribute": {"keep": {"u_core": 1}}}, id="other_attribute"),
        pytest.param({"keep_hierarchy": [], "black_box": []}, id="nothing_named"),
    ],
)
def test_what_keeps_no_module_out_of_the_netlist_leaves_the_edif_alone(tmp_path, settings) -> None:
    """The refusals above are judged by what a setting names, not by its presence: an empty
    mapping, and other attributes, keep the synthesis flat."""
    design = _design(tmp_path / "d", flows={"yosys_fpga": {**SETTINGS, **settings}})
    plan = _plan(tmp_path, "yosys_fpga+vivado_impl", design)
    assert plan.node("yosys_fpga").settings.edif_problem() is None


def test_the_edif_is_named_only_where_it_is_written(tmp_path) -> None:
    """`netlist_edif` is judged by value, as every setting is: a file name other than the default
    asks for an EDIF netlist, and a synthesis that writes none refuses it, naming why."""
    design = _design(tmp_path / "d", flows={"yosys_fpga": {**SETTINGS, "netlist_edif": "a.edif"}})
    assert _plan(tmp_path, "yosys_fpga", design).node("yosys_fpga").settings.netlist_edif == Path(
        "a.edif"
    )
    for settings in ({"flatten": False}, {"fpga": "iCE40HX1K-TQ144"}):
        flows = {"yosys_fpga": {**SETTINGS, "netlist_edif": "a.edif", **settings}}
        with pytest.raises(FlowSettingsException, match="netlist_edif names a file"):
            _plan(tmp_path, "yosys_fpga", _design(tmp_path / "d", flows=flows))
    with pytest.raises(Exception, match="always writes its EDIF netlist"):
        YosysFpga.Settings.from_input({"fpga": PART, "netlist_edif": ""})


def test_vivado_impl_implements_xilinx_devices_only(tmp_path) -> None:
    design = _edif_design(
        tmp_path / "d", flows={"vivado_impl": {**SETTINGS, "fpga": "iCE40HX1K-TQ144"}}
    )
    with pytest.raises(FlowSettingsException, match="implements Xilinx devices"):
        _plan(tmp_path, "vivado_impl", design)


@pytest.mark.parametrize("top", [None, "a/b", "a\\b"])
def test_vivado_impl_needs_a_top_that_can_name_a_file(tmp_path, top) -> None:
    """Vivado finds the top of an EDIF netlist by the name of its file, which the flow gives it."""
    design = _edif_design(tmp_path / "d", flows={"vivado_impl": SETTINGS}, top=top)
    with pytest.raises(FlowSettingsException, match=r"top module|no name a file can have"):
        _plan(tmp_path, "vivado_impl", design)


def test_vivado_impl_needs_a_device(tmp_path) -> None:
    design = _edif_design(tmp_path / "d")
    with pytest.raises(FlowSettingsException, match="needs `fpga`"):
        _plan(tmp_path, "vivado_impl", design)


# ------------------------------------------------------------------------------------ execution


def test_vivado_impl_implements_the_edif_netlist_yosys_fpga_writes(tmp_path, toolchain) -> None:
    runner = _runner(tmp_path)
    flow = runner.run_flow(VivadoImpl, _design(tmp_path / "d"), {**SETTINGS, "bitstream": "b.bit"})
    assert flow is not None and flow.succeeded
    (yosys,) = producers_of(runner, flow)
    edif = Path(yosys.results["outputs"]["netlist_edif"]["path"])
    assert edif.name == "netlist.edif" and flow.inputs.netlist == edif
    calls = fake_calls(flow.run_path)
    commands = [call[0] for call in calls]
    # the constraints, then the netlist, which is linked for the part and the top, then the steps
    assert commands.index("read_xdc") < commands.index("read_edif") < commands.index("link_design")
    assert commands.index("link_design") < commands.index("opt_design")
    for step in ("place_design", "route_design", "write_bitstream"):
        assert commands.index("opt_design") < commands.index(step)
    assert ["link_design", "-part", PART, "-top", TOP] in calls
    assert "synth_design" not in commands, "Vivado does no synthesis here"
    # the steps of the default strategy, as `docs/flows.rst` lists them
    steps = {"opt_design", "place_design", "phys_opt_design", "route_design", "write_bitstream"}
    assert [command for command in commands if command in steps] == [
        *("opt_design", "place_design", "opt_design", "phys_opt_design", "phys_opt_design"),
        *("route_design", "write_bitstream"),
    ]
    assert flow.results["lut"] and flow.results["ff"] and "wns" in flow.results
    assert flow.results["Fmax"] and "status" not in flow.results
    assert (flow.run_path / "b.bit").is_file()
    assert flow.results["outputs"]["bitstream"]["path"] == str(flow.run_path / "b.bit")


def test_yosys_writes_the_edif_with_bus_ranges_whose_file_vivado_reads(tmp_path, toolchain) -> None:
    """The synthesis writes `write_edif -pvector bra`, without which every bus is written as
    plain bits and read back reversed; the EDIF handed over is that file, byte for byte."""
    runner = _runner(tmp_path)
    flow = runner.run_flow(VivadoImpl, _design(tmp_path / "d"), SETTINGS)
    assert flow is not None and flow.succeeded
    (yosys,) = producers_of(runner, flow)
    script = (yosys.run_path / "yosys_fpga_synth.ys").read_text()
    assert script.count("write_edif -pvector bra netlist.edif\n") == 1
    produced = (yosys.run_path / "netlist.edif").read_text()
    assert "no ranges" not in produced  # the stand-in writes this when `bra` is missing
    assert (flow.run_path / f"{TOP}.edif").read_text() == produced


def test_the_netlist_is_staged_under_the_name_of_the_top_whatever_it_was_called(
    tmp_path, toolchain
) -> None:
    """Vivado finds the top of an EDIF netlist by the name of its file (`[Project 1-68] No files
    found to match top module`). A producer's file is called what its setting says, a listed
    netlist what its author called it: the script reads a copy named for the top."""
    runner = _runner(tmp_path)
    produced = runner.run_flow(
        VivadoImpl,
        _design(tmp_path / "d"),
        SETTINGS,
        all_flows_settings={"yosys_fpga": {"netlist_edif": "net/whatever.edif"}},
    )
    (yosys,) = producers_of(runner, produced)
    assert produced.inputs.netlist == yosys.run_path / "net" / "whatever.edif"
    listed = _runner(tmp_path).run_flow(VivadoImpl, _edif_design(tmp_path / "d"), SETTINGS)
    assert listed.inputs.netlist == tmp_path / "d" / "given.edf"
    for flow in (produced, listed):
        assert flow.succeeded
    # one run directory: the second run replaced the copy the first one made
    staged = listed.run_path / f"{TOP}.edif"
    assert staged.read_text() == (tmp_path / "d" / "given.edf").read_text() != ""
    for flow in (produced, listed):
        assert ["read_edif", f"{TOP}.edif"] in fake_calls(flow.run_path)
        assert ["link_design", "-part", PART, "-top", TOP] in fake_calls(flow.run_path)


@needs_tclsh
def test_the_fake_vivado_finds_the_top_by_the_name_of_the_file_as_vivado_does(tmp_path) -> None:
    """The stand-in has the teeth the hazard needs: a netlist not named for its top is not found."""
    (tmp_path / "other.edif").write_text(NETLIST)
    script = tmp_path / "script.tcl"
    for name, top, ok in (("other.edif", "other", True), ("other.edif", "blinky", False)):
        script.write_text(f"read_edif [list {name}]\nlink_design -part {PART} -top {top}\n")
        result = subprocess.run(
            [str(FAKE_TOOLS_DIR / "vivado"), "-mode", "batch", "-source", str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert (result.returncode == 0) is ok, result.stderr
        if not ok:
            assert "[Project 1-68] No files found to match top module 'blinky'" in result.stderr


def test_a_listed_edif_netlist_is_implemented_without_synthesis(tmp_path, toolchain) -> None:
    runner = _runner(tmp_path)
    flow = runner.run_flow(VivadoImpl, _edif_design(tmp_path / "d"), SETTINGS)
    assert flow is not None and flow.succeeded
    assert not producers_of(runner, flow) and [f.name for f in runner.launched] == ["vivado_impl"]
    assert flow.inputs.netlist == tmp_path / "d" / "given.edf"
    assert not (tmp_path / "run" / "blinky" / "yosys_fpga").exists()


def _stopped_before_vivado(run_path: Path, raised) -> str:
    """The message of the failure `raised` holds, after the checks all such failures share: it
    names the cause and the remedy, no Vivado command ran, and the run's failure document says
    the same."""
    message = str(raised.value)
    assert "not flat" in message and "flatten" in message, message
    assert fake_calls(run_path) == [], "Vivado was started"
    document = json.loads((run_path / "results.json").read_text())
    assert document["success"] is False
    assert document["error"] == {"type": "FlowFatalError", "message": message}
    return message


def test_a_listed_netlist_with_a_hierarchy_stops_the_flow_before_vivado_starts(
    tmp_path, toolchain
) -> None:
    """Vivado takes a module that the netlist defines and refers to as a library cell for a black
    box, and stops with `[Project 1-486]` and `[DRC INBB-3]`, and xeda with only its exit status.
    The flow reads the netlist first and says what is wrong and what to do."""
    design = _edif_design(tmp_path / "d")
    (tmp_path / "d" / "given.edf").write_text(edif_netlist(modules=("core",)))
    with pytest.raises(FlowFatalError) as raised:
        _runner(tmp_path).run_flow(VivadoImpl, design, SETTINGS)
    message = _stopped_before_vivado(tmp_path / "run" / "blinky" / "vivado_impl", raised)
    assert "given.edf" in message and "core" in message


@pytest.mark.parametrize("kept_by", ["hdl", "rtl_attributes"])
def test_a_hierarchy_the_settings_do_not_show_stops_the_chain_before_vivado_starts(
    tmp_path, monkeypatch, kept_by
) -> None:
    """Nothing in the settings shows a `(* keep_hierarchy *)` attribute of the HDL, or one the
    design gives in `rtl.attributes` (`init()` folds it into `set_attribute`, after the plan
    declared the EDIF netlist): yosys writes the EDIF netlist all the same (the synthesis itself
    succeeds, as it does for `nextpnr`'s netlist of a hierarchy), and the flow that reads it
    finds the hierarchy."""
    require_yosys()
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    source, attributes = BLINKY_OVER_A_COUNTER, None
    if kept_by == "rtl_attributes":  # a bare name selects a module in yosys; the instance is `c:`
        source = source.replace("(* keep_hierarchy *)\n", "")
        attributes = {"keep_hierarchy": {"c:u_counter": 1}}
    design = _design(tmp_path / "d", attributes=attributes)
    (tmp_path / "d" / "blinky.v").write_text(source)
    runner = _runner(tmp_path)
    with pytest.raises(FlowFatalError) as raised:
        runner.run_flow(VivadoImpl, design, SETTINGS)
    message = _stopped_before_vivado(tmp_path / "run" / "blinky" / "vivado_impl", raised)
    assert "counter" in message and "netlist.edif" in message
    yosys = next(flow for flow in runner.launched if flow.name == "yosys_fpga")
    assert yosys.succeeded and (yosys.run_path / "netlist.edif").is_file()


def test_a_synthesis_that_keeps_a_hierarchy_still_feeds_nextpnr(tmp_path) -> None:
    """The refusal is vivado_impl's: the synthesis is not failed for a hierarchy, and the JSON
    netlist of one is what `nextpnr` places."""
    flows = {"yosys_fpga": {**SETTINGS, "keep_hierarchy": ["blinky"]}}
    plan = _plan(tmp_path, "yosys_fpga+nextpnr", _design(tmp_path / "d", flows=flows))
    assert [node.name for node in plan.nodes] == ["yosys_fpga", "nextpnr"]
    assert plan.node("yosys_fpga").settings.edif_problem() is not None


@pytest.mark.parametrize("bitstream", [False, True], ids=["routed", "bitstream"])
def test_the_bitstream_is_written_and_recorded_only_when_asked(
    tmp_path, toolchain, bitstream
) -> None:
    settings = {**SETTINGS, **({"bitstream": "outputs/blinky.bit"} if bitstream else {})}
    flow = _runner(tmp_path).run_flow(VivadoImpl, _edif_design(tmp_path / "d"), settings)
    assert flow is not None and flow.succeeded
    commands = [call[0] for call in fake_calls(flow.run_path)]
    assert ("write_bitstream" in commands) is bitstream
    assert ("bitstream" in flow.results["outputs"]) is bitstream
    assert (flow.run_path / "outputs" / "blinky.bit").is_file() is bitstream
    if bitstream:
        assert flow.artifacts["bitstream"].name == "blinky.bit"


def test_a_bitstream_given_a_location_is_delivered_there(tmp_path, toolchain) -> None:
    target = tmp_path / "out" / "top.bit"
    flow = _runner(tmp_path).run_flow(
        VivadoImpl, _edif_design(tmp_path / "d"), {**SETTINGS, "bitstream": str(target)}
    )
    assert flow is not None and flow.succeeded
    written = flow.run_path / "outputs" / "blinky.bit"  # what the run writes, whatever the location
    assert written.is_file() and target.read_bytes() == written.read_bytes()


def test_a_bitstream_a_consumer_asks_for_is_written_where_the_loader_reads_it(
    tmp_path, toolchain
) -> None:
    from .test_openfpgaloader import assert_fake_loader

    assert_fake_loader()
    runner = _runner(tmp_path)
    loader = runner.run(
        "yosys_fpga+vivado_impl+openfpgaloader",
        str(_write_yaml(tmp_path, {"vivado_impl": SETTINGS, "openfpgaloader": {"verify": False}})),
    )
    assert loader is not None and loader.succeeded
    vivado = next(f for f in runner.launched if f.name == "vivado_impl")
    assert loader.inputs.bitstream == vivado.run_path / "outputs" / "blinky.bit"


def _write_yaml(tmp_path: Path, flows: dict) -> Path:
    """The design as a file, for `runner.run`."""
    root = tmp_path / "file"
    _design(root)
    path = root / "blinky.yaml"
    document = {
        "name": "blinky",
        "rtl": {"sources": ["blinky.v", "pins.xdc"], "top": TOP, "clock": {"port": "clk"}},
        "flows": flows,
    }
    path.write_text(yaml.safe_dump(document))
    return path


def test_constraints_are_read_clock_first_then_the_designs_then_the_settings(
    tmp_path, toolchain
) -> None:
    (tmp_path / "extra.xdc").write_text("# an extra constraint\n")
    flow = _runner(tmp_path).run_flow(
        VivadoImpl,
        _edif_design(tmp_path / "d"),
        {**SETTINGS, "xdc_files": [str(tmp_path / "extra.xdc")]},
    )
    assert flow is not None and flow.succeeded
    read = [call[1] for call in fake_calls(flow.run_path) if call[0] == "read_xdc"]
    assert read == ["clock.xdc", str(tmp_path / "d" / "pins.xdc"), str(tmp_path / "extra.xdc")]
    assert "create_clock -period 10.000 -name" in (flow.run_path / "clock.xdc").read_text()


def test_the_implementation_options_are_the_non_project_strategies(tmp_path, toolchain) -> None:
    flow = _runner(tmp_path).run_flow(
        VivadoImpl,
        _edif_design(tmp_path / "d"),
        {**SETTINGS, "impl": {"strategy": "Timing", "steps": {"route": {"directive": "Explore"}}}},
    )
    assert flow is not None and flow.succeeded
    calls = fake_calls(flow.run_path)
    assert ["place_design", "-directive", "ExtraPostPlacementOpt"] in calls
    assert ["route_design", "-directive", "Explore"] in calls  # a step's own options win
    with pytest.raises(Exception, match="Unknown strategy: Fastest"):
        VivadoImpl.Settings.from_input({"fpga": PART, "impl": {"strategy": "Fastest"}})


@needs_tclsh
@pytest.mark.parametrize("flow", ["vivado_impl", "vivado_alt_synth"])
def test_every_step_of_the_shared_implementation_script_writes_into_a_directory_it_made(
    flow, tmp_path, toolchain
) -> None:
    """`implementation.tcl` is the tail of two scripts, and each has to make the directory of every
    report and file the steps write (Vivado does not, and the stand-in fails as it does). All the
    optional steps are on: the power optimization after placement, the optimizations around it,
    the extra reports, and for `vivado_alt_synth` the checkpoints and the netlists."""
    impl = {
        "strategy": "Timing",
        "steps": {"power_opt": {"verbose": True}, "post_route_phys_opt": ["-placement_opt"]},
    }
    settings: dict = {**SETTINGS, "impl": impl, "extra_reports": True, "qor_suggestions": True}
    settings["bitstream"] = "outputs/blinky.bit"
    if flow == "vivado_impl":
        design, flow_class = _edif_design(tmp_path / "d"), VivadoImpl
    else:
        design = Design.from_file(Path(__file__).parent / "resources" / "design0" / "design0.toml")
        flow_class = VivadoAltSynth
        settings |= {"write_checkpoint": True, "write_netlist": True, "write_timing_netlist": True}
        settings["synth"] = {"steps": {"power_opt": {"verbose": True}}}
    run = _runner(tmp_path).run_flow(flow_class, design, settings)
    assert run is not None and run.succeeded
    commands = [call[0] for call in fake_calls(run.run_path)]
    for command in ("power_opt_design", "report_power_opt", "phys_opt_design", "write_bitstream"):
        assert command in commands
    reported = [call[2] for call in fake_calls(run.run_path) if call[0] == "report_power_opt"]
    assert any(path.endswith("post_place/post_place_power_optimization.rpt") for path in reported)


def test_a_second_launch_runs_nothing_and_an_edited_constraint_file_runs_vivado_again(
    tmp_path, toolchain, monkeypatch
) -> None:
    """A constraint file a setting names is an input: editing it runs Vivado again, and the
    synthesis, which does not read it, stays fresh."""
    extra = tmp_path / "extra.xdc"
    extra.write_text("# a constraint\n")
    settings = {**SETTINGS, "xdc_files": [str(extra)]}
    design = _design(tmp_path / "d")
    runner = _runner(tmp_path)
    first = runner.run_flow(VivadoImpl, design, settings)
    assert first is not None and first.succeeded
    fresh = launch_until_fresh(runner, lambda: runner.launch_flow(VivadoImpl, design, settings))
    assert fresh.reused and all(f.reused for f in runner.launched[-2:])
    check_after_the_racy_window(monkeypatch)
    extra.write_text("# another constraint\n")
    again = runner.launch_flow(VivadoImpl, design, settings)
    assert not again.reused and "extra.xdc" in (again.stale_reason or ""), again.stale_reason
    yosys, vivado = runner.launched[-2:]
    assert (yosys.name, yosys.reused, vivado.name) == ("yosys_fpga", True, "vivado_impl")


@needs_tclsh
def test_a_netlist_vivado_cannot_read_fails_the_script_with_its_own_message(
    tmp_path, toolchain, monkeypatch, capfd
) -> None:
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "read_edif")
    flow = _runner(tmp_path).run_flow(VivadoImpl, _edif_design(tmp_path / "d"), SETTINGS)
    assert flow is not None and not flow.succeeded
    assert "ERROR: read_edif failed" in click.unstyle(capfd.readouterr().out)
    calls = fake_calls(flow.run_path)
    assert "read_edif" in {call[0] for call in calls} and "place_design" not in {
        c[0] for c in calls
    }
    assert "errorExit" not in {call[0] for call in calls}, "errorExit ran as a tool command"


@needs_tclsh
@pytest.mark.parametrize("fail_timing", [True, False])
def test_timing_that_is_not_met_fails_the_flow_unless_it_is_allowed(
    tmp_path, toolchain, monkeypatch, fail_timing
) -> None:
    fake_returns(monkeypatch, {("get_property", "SLACK"): "-0.5"})
    flow = _runner(tmp_path).run_flow(
        VivadoImpl,
        _edif_design(tmp_path / "d"),
        {**SETTINGS, "fail_timing": fail_timing, "bitstream": "b.bit"},
    )
    assert flow is not None
    assert flow.succeeded is (not fail_timing)
    assert any(c[0] == "write_bitstream" for c in fake_calls(flow.run_path)) is (not fail_timing)


def test_the_results_are_those_of_vivado_synth_but_for_the_projects_status() -> None:
    kept = {key: text for key, text in VivadoSynth.results_description.items() if key != "status"}
    assert VivadoImpl.results_description == kept
    for key in ("Fmax", "wns", "whs", "lut", "ff", "dsp", "bram_RAMB36"):
        assert key in VivadoImpl.results_description


def test_a_run_leaves_no_checkpoint_or_netlist_and_asks_no_setting_for_one() -> None:
    """Until stage-typed checkpoint and netlist types exist, the bitstream is the only output."""
    settings = set(VivadoImpl.Settings.model_fields)
    assert not settings & {"write_checkpoint", "write_netlist", "write_timing_netlist"}
    assert not settings & {"synth", "flatten_hierarchy", "out_of_context", "tcl_files"}
    assert {"fpga", "clocks", "impl", "bitstream", "xdc_files", "fail_timing"} <= settings


# ------------------------------------------------------------------------- tricky names, no pins


EVIL = '[xeda_injected] $xeda_undefined "q" {b} ;c'


@needs_tclsh
def test_every_text_of_the_design_reaches_vivado_as_one_literal_word(tmp_path, toolchain) -> None:
    """A top, a netlist and constraint files whose names carry Tcl metacharacters: the script
    hands Vivado each whole -- the netlist as the copy named for the top -- and Tcl runs none."""
    top = f"top {EVIL}"
    root = tmp_path / "d"
    _design(root)
    for name, text in (("net [x] $v.edf", NETLIST), ("pins [x] $v.xdc", PINS)):
        (root / name).write_text(text)
    (tmp_path / "more [x] $v.xdc").write_text("# more\n")
    design = Design(
        name="blinky",
        design_root=root,
        rtl={
            "sources": [{"file": "net [x] $v.edf", "type": "Edif"}, "pins [x] $v.xdc"],
            "top": top,
            "clock": {"port": f"clk {EVIL}"},
        },
    )
    flow = _runner(tmp_path).run_flow(
        VivadoImpl, design, {**SETTINGS, "xdc_files": [str(tmp_path / "more [x] $v.xdc")]}
    )
    assert flow is not None and flow.succeeded
    # a Tcl list argument is recorded with its elements (`ELEM`) after its own text
    calls = fake_calls(flow.run_path, elements=True)
    assert not [call for call in calls if call[0] == "xeda_injected"], "Tcl ran it"
    (read_edif,) = [call for call in calls if call[0] == "read_edif"]
    assert read_edif[1:] == [f"{{{top}.edif}}", f"{top}.edif"]
    assert ["link_design", "-part", PART, "-top", top] in fake_calls(flow.run_path)
    read = [call[-1] for call in calls if call[0] == "read_xdc"]
    assert read[1:] == [str(root / "pins [x] $v.xdc"), str(tmp_path / "more [x] $v.xdc")]
    assert (flow.run_path / f"{top}.edif").read_text() == NETLIST


def test_a_design_without_pin_constraints_is_implemented_for_its_timing(
    tmp_path, toolchain
) -> None:
    """No bitstream is asked for, so no port has to be constrained: the flow reads the generated
    clock constraint alone and reports Vivado's timing and utilization."""
    design = _design(tmp_path / "d", sources=("blinky.v",))
    flow = _runner(tmp_path).run_flow(VivadoImpl, design, SETTINGS)
    assert flow is not None and flow.succeeded
    assert [c[1] for c in fake_calls(flow.run_path) if c[0] == "read_xdc"] == ["clock.xdc"]
    assert "wns" in flow.results and "Fmax" in flow.results
    assert "write_bitstream" not in {call[0] for call in fake_calls(flow.run_path)}


def test_a_verilog_netlist_is_no_input(tmp_path) -> None:
    """Vivado drops the contents of a block RAM that has undefined bits when it reads a Verilog
    netlist, and says nothing: a Verilog netlist among the sources feeds nothing, and the
    synthesis offers vivado_impl no Verilog netlist."""
    root = tmp_path / "d"
    _design(root)
    (root / "net.v").write_text("module blinky(); endmodule\n")
    design = _design(
        root, sources=("blinky.v", "pins.xdc", {"file": "net.v", "type": "VerilogNetlist"})
    )
    design.flow = {"vivado_impl": SETTINGS}
    plan = _plan(tmp_path, "vivado_impl", design)
    assert [node.name for node in plan.nodes] == ["yosys_fpga", "vivado_impl"]
    for node in plan.nodes:
        assert all(root / "net.v" not in node_input.sources for node_input in node.inputs)
    assert not [
        name
        for name, output in declared_outputs(YosysFpga).items()
        if SourceType.VerilogNetlist in output.types
    ]


# ----------------------------------------------------------------------------- the documentation


def test_the_commands_the_documentation_shows_plan_and_run(tmp_path, toolchain) -> None:
    """The three commands of the section on `vivado_impl`, on its fixture: the first runs the
    chain for reports, the second also for a bitstream where `$PWD` is, the third plans the chain
    on to the loader (which nothing here starts)."""
    from click.testing import CliRunner

    from xeda.cli import cli

    text = FLOWS_RST.read_text(encoding="utf-8")
    section = text[text.index(".. _vivado-impl:") : text.index("\nBluespec\n========\n")]
    unindented = section.replace("\n    ", "\n")
    commands = [
        shlex.split(line, comments=True)[2:]
        for language, body in _blocks(FLOWS_RST)
        if language == "bash" and body in unindented
        for line in body.splitlines()
        if line.startswith("xeda run ")
    ]
    assert [words[0] for words in commands] == ["yosys_fpga+vivado_impl"] * 2 + [
        "yosys_fpga+vivado_impl+openfpgaloader"
    ]
    design = _stage(tmp_path, "vivado_demo.yaml") / "vivado_demo.yaml"
    for words in commands:
        words = [
            str(design) if w == "blinky.yaml" else w.replace("$PWD", str(tmp_path)) for w in words
        ]
        result = CliRunner().invoke(
            cli, ["run", *words, "--run-root", str(tmp_path / "xeda_run"), "--json"]
        )
        assert result.exit_code == 0, (words, result.output)
    assert (tmp_path / "blinky.bit").is_file()
