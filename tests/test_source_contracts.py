"""A flow that hands its tool the design's sources reads the types it declares
(`Flow.reads_sources`), and only those: a source of another type is passed over -- a `.lpf` in a
design built by Vivado -- or, when it is a language the flow cannot read, refused by name at
launch. A design none of whose sources the flow reads is refused too. No source becomes a tool
command by its type's name (Quartus's `XDC_FILE`, `MEMORYFILE_FILE`), and no source crashes a
template: the sweep runs every such flow's scripts under the fake tools with one source of every
type.

Every flow is held to it by a scan of its code and of the templates it renders: a flow that reads
the design's sources declares what it reads, and selects it with `Flow.sources_read()`, so the
declaration is what the tool gets. A flow that declares nothing reads only its declared inputs."""

import ast
import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import ClassVar

import pytest

import xeda
from xeda import Design
from xeda.board import WithFpgaBoardSettings
from xeda.design import LANGUAGE_TYPES, SOURCE_SUFFIXES, SourceType
from xeda.flow import Flow, FlowSettingsException, In
from xeda.flow.flow import registered_flows
from xeda.flow.io import declared_inputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr

from .settings_samples import flow_classes, minimal_settings
from .test_design_parts import _mro, reachable_templates
from .test_tcl_paths import needs_tclsh
from .tool_utils import fake_calls, use_fake_tools

MAIN_SCRIPTS = {
    "vivado_synth.tcl",
    "vivado_alt_synth.tcl",
    "vivado_project.tcl",
    "create_project.tcl",
    "ise_synth.tcl",
    "synth.tcl",
    "dc_script.tcl",
}


def _calls(flow, design, settings, tmp_path, monkeypatch, elements=True):
    """Run the actual flow scripts and require they reach the end, even without reports."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    original = Flow.copy_from_template

    def marked(self, template_name, *args, **kwargs):
        path = original(self, template_name, *args, **kwargs)
        if template_name in MAIN_SCRIPTS:
            text = Path(path).read_text()
            sentinel = "\nxeda_source_contract_complete\n"
            # DC ends normally with exit; early exits must not reach the sentinel.
            if text.rstrip().endswith("\nexit"):
                text = text.rstrip()[:-4] + sentinel + "exit\n"
            else:
                text += sentinel
            Path(path).write_text(text)
        return path

    monkeypatch.setattr(Flow, "copy_from_template", marked)
    run_dir = tmp_path / "run"
    error = None
    try:
        DefaultRunner(run_dir, display_results=False).run_flow(
            registered_flows[flow][1], design, settings
        )
    except Exception as exc:  # Some fake tools do not produce parseable reports.
        error = exc
    calls = fake_calls(run_dir, elements)
    assert ["xeda_source_contract_complete"] in calls, f"{flow} did not finish its script: {error}"
    return calls


#: The flows whose scripts turn the design's sources into tool commands.
FLOWS = [
    "vivado_synth",
    "vivado_alt_synth",
    "vivado_project",
    "quartus",
    "ise_synth",
    "diamond_synth",
    "dc",
]


EXPECTED_READS = {
    "vivado_synth": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader MemoryFile Xdc Sdc Tcl",
    "vivado_alt_synth": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader Xdc Sdc",
    "vivado_project": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader MemoryFile Xdc Sdc Tcl",
    "quartus": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader Sdc",
    "ise_synth": "Verilog VerilogHeader Vhdl Ucf",
    "diamond_synth": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader MemoryFile Sdc Lpf",
    "dc": "Verilog SystemVerilog Vhdl Sdc Tcl",
    "yosys_fpga": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader",
    "yosys": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader",
    "yosys_sim": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader Cpp",
    "verilator": "Verilog SystemVerilog VerilogHeader SVHeader Cpp",
    "ghdl_sim": "Vhdl",
    "ghdl_synth": "Vhdl",
    "nvc": "Vhdl",
    "modelsim": "Verilog SystemVerilog Vhdl",
    "vcs": "Verilog SystemVerilog Vhdl VerilogHeader SVHeader",
    "vivado_sim": "Verilog SystemVerilog Vhdl",
    "vivado_postsynth_sim": "Verilog SystemVerilog Vhdl",
    "bsc": "Bluespec Verilog SystemVerilog",
    "bsc_sim": "Bluespec Verilog SystemVerilog C Cpp ObjectFile",
}
#: The flows that read none of the design's sources: each reads only its declared inputs.
READS_NO_SOURCE = [
    "fpga_pack",
    "nextpnr",
    "openfpgaloader",
    "openroad",
    "vivado_impl",
    "vivado_power",
]
REFUSED = [
    (flow, member, part)
    for flow, accepted in EXPECTED_READS.items()
    for member in sorted(LANGUAGE_TYPES, key=lambda m: m.name)
    if member.name not in accepted.split()
    for part in sorted(registered_flows[flow][1].design_parts)
]
FLOWS_DIR = Path(xeda.__file__).parent / "flows"


def _suffix(member: SourceType) -> str:
    """A suffix `member` is inferred from, or `dat` for a member given by `type` only."""
    return next((s for s, (m, _v) in sorted(SOURCE_SUFFIXES.items()) if m is member), "dat")


def _settings(root: Path, flow: str) -> dict:
    """The least `flow` runs with here."""
    if flow not in FLOWS:
        return minimal_settings(registered_flows[flow][1])
    if flow == "dc":
        lib = root / "pdk" / "cells.db"
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_text("")
        return {"target_libraries": [str(lib)], "clock": {"period": 10.0}}
    fpga = {
        "quartus": "10CL016YU256C6G",
        "ise_synth": "xc6slx9-2-tqg144",
        "diamond_synth": "LFE5U-25F-6BG256C",
    }.get(flow, "xc7a12tcsg325-1")
    return {"fpga": fpga, "clock": {"period": 10.0}}


def _every_type(
    root: Path, flow_class, spelling="inferred"
) -> tuple[Design, dict[SourceType, Path]]:
    """A design with one source of every type `flow_class` does not refuse."""
    files: dict[SourceType, Path] = {}
    for member in SourceType:
        if member in LANGUAGE_TYPES and member not in flow_class.reads_sources:
            continue
        suffix = {
            "inferred": _suffix(member),
            "misleading": "v",
            "unknown": "dat",
            "uppercase": _suffix(member).upper(),
        }[spelling]
        folder = root / f"s_{member.name.lower()} [x] $v"
        folder.mkdir()
        path = folder / f"source.{suffix}"
        path.write_text("# a source\n")
        files[member] = path
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": [{"file": str(p), "type": m.name} for m, p in files.items()],
            "top": "top",
            "clock": "clk",
        },
    )
    return design, files


@pytest.mark.parametrize("flow", sorted(EXPECTED_READS))
def test_every_contract_flow_declares_what_it_reads(flow):
    reads = registered_flows[flow][1].reads_sources
    assert reads == frozenset(SourceType[name] for name in EXPECTED_READS[flow].split()), flow


@needs_tclsh
@pytest.mark.parametrize("flow", FLOWS)
@pytest.mark.parametrize("spelling", ["inferred", "misleading", "unknown", "uppercase"])
def test_a_flow_hands_its_tool_only_the_sources_it_reads(flow, spelling, tmp_path, monkeypatch):
    flow_class = registered_flows[flow][1]
    root = tmp_path / "design"
    root.mkdir()
    design, files = _every_type(root, flow_class, spelling)
    calls = _calls(flow, design, _settings(root, flow), tmp_path, monkeypatch)
    arguments = [argument for call in calls for argument in call]
    handed = sorted(
        member.name
        for member, path in files.items()
        if member not in flow_class.reads_sources
        and any(str(path) in argument for argument in arguments)
    )
    assert not handed, f"{flow} handed its tool sources it does not read: {handed}"
    for member in flow_class.reads_sources:
        path = files[member]
        assert _read_call(flow, member, path, calls), (flow, member, path, calls)


@pytest.mark.parametrize(("flow", "member", "part"), REFUSED)
def test_a_language_the_flow_cannot_read_is_refused_at_launch(
    flow, member, part, tmp_path, monkeypatch
):
    flow_class = registered_flows[flow][1]
    root = tmp_path / "design"
    root.mkdir()
    # a source the flow reads, so the unsupported language is what it refuses
    read = min(LANGUAGE_TYPES & flow_class.reads_sources, key=lambda m: m.name)
    (root / f"top.{_suffix(read)}").write_text("a source the flow reads\n")
    source = root / f"unsupported.{_suffix(member)}"
    source.write_text("a source\n")
    rtl = {"sources": [f"top.{_suffix(read)}"], "top": "top"}
    tb = {"sources": [], "top": "tb"}
    (rtl if part == "rtl" else tb)["sources"].append({"file": str(source), "type": member.name})
    design = Design(name="d", design_root=root, rtl=rtl, tb=tb)
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(
        FlowSettingsException, match=f"cannot read the design's {member.name}"
    ) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            flow_class, design, _settings(root, flow)
        )
    assert str(source) in str(raised.value)
    assert not list((tmp_path / "xeda_run").rglob("settings.json")), "nothing was set up"


# ---------------------------------------------------------- a flow that reads none of the sources

PRODUCT_FLOWS = [cls for cls, _name in flow_classes() if cls.__module__.startswith("xeda.")]
#: The flows that hand their tool the design's sources, and so declare what they read.
READING_FLOWS = [cls for cls in PRODUCT_FLOWS if cls.reads_sources is not None]
#: The flows that read none of the design's sources, only their declared inputs.
INPUT_ONLY_FLOWS = [cls for cls in PRODUCT_FLOWS if cls.reads_sources is None]
READS_ONE_TYPE = [(cls, member) for cls in READING_FLOWS for member in sorted(cls.reads_sources)]


#: What the refusal of a default producer adds: the source that would have replaced the node.
SKIPPED_BY_A_NETLIST = "a JsonNetlist source would supply nextpnr's netlist and skip yosys_fpga"


def _edif_only(root: Path) -> Design:
    """A design whose only source is a netlist that no flow reads."""
    root.mkdir(exist_ok=True)
    (root / "top.edf").write_text("(edif top)\n")
    return Design(name="d", design_root=root, rtl={"sources": ["top.edf"], "top": "top"})


def test_every_flow_either_declares_what_it_reads_or_reads_only_its_inputs():
    assert sorted(cls.name for cls in READING_FLOWS) == sorted(EXPECTED_READS)
    assert sorted(cls.name for cls in INPUT_ONLY_FLOWS) == READS_NO_SOURCE
    for cls in INPUT_ONLY_FLOWS:
        assert declared_inputs(cls), f"{cls.name} reads no source and declares no input"


@pytest.mark.parametrize("flow_class", READING_FLOWS, ids=lambda cls: cls.name)
def test_a_flow_that_reads_sources_refuses_a_design_with_none_it_reads(flow_class, tmp_path):
    design = _edif_only(tmp_path)
    with pytest.raises(FlowSettingsException) as raised:
        flow_class.check_design_supported(design)
    message = str(raised.value)
    assert message.startswith(f"{flow_class.name} reads none of the design's sources"), message
    assert f"{tmp_path / 'top.edf'} (Edif)" in message
    for member in flow_class.reads_sources:
        assert member.name in message


@pytest.mark.parametrize("flow_class", INPUT_ONLY_FLOWS, ids=lambda cls: cls.name)
def test_a_flow_that_reads_only_its_inputs_is_not_refused_by_that_rule(flow_class, tmp_path):
    flow_class.check_design_supported(_edif_only(tmp_path))


def test_the_refusal_names_what_the_design_lists_and_what_the_flow_reads(tmp_path):
    design = _edif_only(tmp_path)
    with pytest.raises(FlowSettingsException) as raised:
        registered_flows["yosys_fpga"][1].check_design_supported(design)
    assert str(raised.value) == (
        "yosys_fpga reads none of the design's sources: "
        f"rtl.sources has {tmp_path / 'top.edf'} (Edif); "
        "yosys_fpga reads SVHeader, SystemVerilog, Verilog, VerilogHeader, Vhdl"
    )
    empty = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "top"})
    with pytest.raises(FlowSettingsException) as raised:
        registered_flows["vivado_project"][1].check_design_supported(empty)
    # `vivado_project` reads the testbench too, so the message names both parts
    assert str(raised.value).startswith(
        "vivado_project reads none of the design's sources: "
        "rtl.sources has none, tb.sources has none; vivado_project reads "
    )


@pytest.mark.parametrize(
    ("flow_class", "member"), READS_ONE_TYPE, ids=lambda x: getattr(x, "name", x)
)
def test_a_design_with_one_source_of_a_type_the_flow_reads_is_not_refused(
    flow_class, member, tmp_path
):
    """The rule is that the flow reads none: one source it reads, whichever type, is enough. A
    source that does not exist yet (`{path: ...}`, which a generator writes) counts too."""
    path = tmp_path / f"source.{_suffix(member)}"
    path.write_text("# a source\n")
    for entry in (
        {"file": str(path), "type": member.name},
        {"path": str(path), "type": member.name},
    ):
        rtl = {"sources": [entry], "top": "top"}
        flow_class.check_design_supported(Design(name="d", design_root=tmp_path, rtl=rtl))
    if "tb" in flow_class.design_parts:
        design = Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": [], "top": "top"},
            tb={"sources": [{"file": str(path), "type": member.name}], "top": "tb"},
        )
        flow_class.check_design_supported(design)


@pytest.mark.parametrize("flow", ["vivado_synth", "yosys_fpga", "nextpnr"])
def test_a_design_with_nothing_the_flow_reads_is_refused_at_launch(flow, tmp_path, monkeypatch):
    """`nextpnr` plans a `yosys_fpga` synthesis of a design that has no source for it: that
    node is the one refused."""
    root = tmp_path / "design"
    design = _edif_only(root)
    settings = (
        {"fpga": {"part": "LFE5U-25F-6BG381C"}} if flow == "nextpnr" else _settings(root, flow)
    )
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FlowSettingsException, match="reads none of the design's sources") as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            registered_flows[flow][1], design, settings
        )
    assert str(raised.value).startswith("yosys_fpga" if flow == "nextpnr" else flow)
    assert str(root / "top.edf") in str(raised.value)
    # only `nextpnr` takes a source in place of the producer it plans by default
    assert (SKIPPED_BY_A_NETLIST in str(raised.value)) is (flow == "nextpnr")
    assert not list((tmp_path / "xeda_run").rglob("settings.json")), "nothing was set up"


@pytest.mark.parametrize(
    ("request_text", "hinted"),
    [
        ("yosys_fpga", False),
        # `nextpnr` reaches `yosys_fpga` by default, so a netlist source would replace it ...
        ("nextpnr", True),
        # ... and a chain binds the producer: no source replaces it, so none is suggested
        ("yosys_fpga+nextpnr", False),
    ],
)
def test_a_plan_refuses_a_design_with_nothing_the_flow_reads(
    request_text, hinted, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    design = _edif_only(tmp_path / "design")
    with pytest.raises(
        FlowSettingsException, match="yosys_fpga reads none of the design's sources"
    ) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(
            request_text, design, flow_settings={"fpga": {"part": "LFE5U-25F-6BG381C"}}
        )
    assert (SKIPPED_BY_A_NETLIST in str(raised.value)) is hinted
    if hinted:
        assert str(raised.value).endswith(f"; {SKIPPED_BY_A_NETLIST}")
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


@pytest.fixture
def private_registry():
    """A flow that a test defines does not leak into the sweeps of other tests."""
    saved = dict(registered_flows)
    yield
    registered_flows.clear()
    registered_flows.update(saved)


def _reads_nextpnr_and_the_netlist():
    """A test flow with two inputs: `nextpnr`'s configuration, which it takes from the default
    producer, and a netlist that has no default and that a saved binding takes from `yosys_fpga`."""

    class _ReadsNextpnrAndTheNetlist(Flow):
        """Read a configuration from nextpnr and a netlist from yosys_fpga."""

        results_description: ClassVar[dict[str, str]] = {}
        required_settings = Nextpnr.required_settings

        class Settings(WithFpgaBoardSettings, Flow.Settings):
            """No setting of its own."""

        class Inputs(Flow.Inputs):
            config: Path = In(
                SourceType.EcpConfig,
                producer="nextpnr",
                output="config",
                description="The configuration nextpnr writes.",
            )
            netlist: Path = In(SourceType.JsonNetlist, description="A synthesized netlist.")

        def run(self):
            self.results["read"] = self.inputs.netlist.read_text()

    return _ReadsNextpnrAndTheNetlist


def _own_refusal(flow: str, design: Design) -> str:
    """What the flow says about the design when it is launched alone: no producer, no note."""
    with pytest.raises(FlowSettingsException) as refused:
        registered_flows[flow][1].check_design_supported(design)
    return str(refused.value)


def test_a_producer_that_another_input_binds_is_not_said_to_leave_the_plan(
    tmp_path, monkeypatch, private_registry
):
    """`nextpnr` reaches `yosys_fpga` by default, but another flow's input is bound to it by a
    saved binding: a netlist source would replace the first edge only, so the plan would keep
    `yosys_fpga` and refuse again. The refusal suggests nothing."""
    monkeypatch.chdir(tmp_path)
    taker = _reads_nextpnr_and_the_netlist()
    design = _edif_only(tmp_path / "design")
    design.flow[taker.name] = {"inputs": {"netlist": "yosys_fpga.netlist"}}
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(
            taker, design, flow_settings={"fpga": {"part": "LFE5U-25F-6BG381C"}}
        )
    assert str(raised.value) == _own_refusal("yosys_fpga", design)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


def test_a_producer_that_several_inputs_reach_by_default_is_not_said_to_leave_with_one_source(
    tmp_path, monkeypatch
):
    """`vivado_power` and `vivado_postsynth_sim` take their inputs from `vivado_synth`, every one
    by default: no one source replaces it, so the refusal suggests none. The design has a
    testbench, which `vivado_postsynth_sim` reads, so the synthesis is what is refused."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.edf").write_text("(edif top)\n")
    (root / "tb.v").write_text("module tb; endmodule\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.edf"], "top": "top"},
        tb={"sources": ["tb.v"], "top": "tb"},
    )
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(
            "vivado_power",
            design,
            flow_settings={"fpga": "xc7a12tcsg325-1", "clock": {"period": 10.0}},
        )
    assert str(raised.value) == _own_refusal("vivado_synth", design)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


@pytest.mark.parametrize(
    ("flow", "source", "plan"),
    [
        # a typed netlist replaces the synthesis that would have no source to read
        ("nextpnr", {"file": "top.json", "type": "JsonNetlist"}, ["nextpnr"]),
        ("openfpgaloader", {"file": "top.bit", "type": "Bitstream"}, ["openfpgaloader"]),
    ],
)
def test_a_source_of_a_later_stage_stands_in_for_the_flow_that_would_read_none(
    flow, source, plan, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / source["file"]).write_text("{}\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [source], "top": "top"})
    planned = DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(
        flow, design, flow_settings={"fpga": {"part": "LFE5U-25F-6BG381C"}}
    )
    assert [node.name for node in planned.nodes] == plan


@needs_tclsh
def test_quartus_assigns_each_source_it_reads_by_the_assignment_quartus_has(tmp_path, monkeypatch):
    root = tmp_path / "design"
    root.mkdir()
    names = ["top.v", "defs.vh", "pins.xdc", "rom.mem", "hook.tcl"]
    for name in names:
        (root / name).write_text("# a source\n")
    design = Design(
        name="d", design_root=root, rtl={"sources": names, "top": "top", "clock": "clk"}
    )
    calls = _calls("quartus", design, _settings(root, "quartus"), tmp_path, monkeypatch)
    assert ["set_global_assignment", "-name", "VERILOG_FILE", str(root / "top.v")] in calls
    assert ["set_global_assignment", "-name", "SEARCH_PATH", str(root)] in calls
    assigned = {call[2] for call in calls if call[:2] == ["set_global_assignment", "-name"]}
    assert not assigned & {"XDC_FILE", "MEMORYFILE_FILE", "TCL_FILE", "VERILOGHEADER_FILE"}


@needs_tclsh
@pytest.mark.parametrize("header_suffix", ["vh", "dat"])
def test_ise_preserves_each_header_search_directory_as_a_list_element(
    tmp_path, monkeypatch, header_suffix
):
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text('`include "defs.' + header_suffix + '"\nmodule top; endmodule\n')
    include_dirs = [root / "shared headers", root / "board headers"]
    headers = []
    for directory in include_dirs:
        directory.mkdir()
        header = directory / f"defs.{header_suffix}"
        header.write_text("`define WIDTH 8\n")
        headers.append({"file": str(header), "type": "VerilogHeader"})
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.v", *headers], "top": "top", "clock": "clk"},
    )
    calls = _calls("ise_synth", design, _settings(root, "ise_synth"), tmp_path, monkeypatch)
    (setting,) = [
        call for call in calls if call[:3] == ["project", "set", "Verilog Include Directories"]
    ]
    # The Tcl recorder expands list-valued arguments using Tcl itself. A single joined string
    # splits a directory containing spaces; each actual search path must be a complete element.
    for directory in [root, *include_dirs]:
        assert str(directory) in setting[3:], setting


#: A template that iterates the design's sources, or pipes them into a filter, unfiltered.
RAW_SOURCES = re.compile(
    r"for\s+\w+\s+in\s+design\.(rtl|tb)\.sources\b|design\.(rtl|tb)\.sources\s*\|"
)


def test_no_template_hands_a_tool_the_design_s_sources_unfiltered():
    found = [
        f"{template.relative_to(FLOWS_DIR)}:{n}"
        for template in sorted(FLOWS_DIR.glob("*/templates/*"))
        if template.is_file()
        for n, line in enumerate(template.read_text(errors="ignore").splitlines(), 1)
        if RAW_SOURCES.search(line)
    ]
    assert not found, "use sources_read() (Flow.reads_sources):\n" + "\n".join(found)


def _read_call(flow, member, path, calls):
    """Check the command and its whole path, including list-valued Tcl arguments."""
    name = member.name

    def handed(prefix, value=path):
        return any(c[: len(prefix)] == prefix and str(value) in c[len(prefix) :] for c in calls)

    if flow in ("vivado_synth", "vivado_alt_synth"):
        if name == "Verilog":
            return handed(["read_verilog"]) and not handed(["read_verilog", "-sv"])
        if name == "SystemVerilog":
            return handed(["read_verilog", "-sv"])
        if name == "Vhdl":
            return handed(["read_vhdl"])
        if name in ("Xdc", "Sdc"):
            if flow == "vivado_synth":
                return handed(["add_files", "-fileset", "constrs_1", "-norecurse"])
            return handed(["read_xdc"])
        if name == "Tcl":
            return handed(["source", "-verbose"])
        if name in ("VerilogHeader", "SVHeader") and flow == "vivado_alt_synth":
            return handed(["set_property", "include_dirs"], path.parent)
        return handed(["add_files", "-fileset", "sources_1", "-norecurse"])
    if flow == "vivado_project":
        fileset = "constrs_1" if name in ("Xdc", "Sdc") else "sources_1"
        fmt = {
            "Verilog": "Verilog",
            "SystemVerilog": "SystemVerilog",
            "Vhdl": "VHDL",
            "VerilogHeader": "Verilog Header",
            "SVHeader": "Verilog Header",
            "MemoryFile": "Memory File",
            "Xdc": "XDC",
            "Sdc": "SDC",
            "Tcl": "TCL",
        }[name]
        return handed(["add_files", "-fileset", fileset, "-norecurse"]) and any(
            first[0] == "get_files"
            and str(path) in first[1:]
            and second[:3] == ["set_property", "FILE_TYPE", fmt]
            for first, second in zip(calls, calls[1:])
        )
    if flow == "quartus":
        assignment = {
            "Verilog": "VERILOG_FILE",
            "SystemVerilog": "SYSTEMVERILOG_FILE",
            "Vhdl": "VHDL_FILE",
            "Sdc": "SDC_FILE",
            "VerilogHeader": "SEARCH_PATH",
            "SVHeader": "SEARCH_PATH",
        }[name]
        value = path.parent if assignment == "SEARCH_PATH" else path
        return handed(["set_global_assignment", "-name", assignment], value)
    if flow == "diamond_synth":
        fmt = {"Vhdl": "VHDL", "Verilog": "Verilog", "SystemVerilog": "SystemVerilog"}.get(name)
        return handed(["prj_src", "add"] + (["-format", fmt] if fmt else []))
    if flow == "ise_synth":
        fmt = {"Verilog": ".v", "Vhdl": ".vhd", "VerilogHeader": ".vh", "Ucf": ".ucf"}[name]
        return any(
            c[:2] == ["xfile", "add"]
            and c[-1] == "-copy"
            and (
                str(path) in c
                or (Path(c[2]).suffix == fmt and Path(c[2]).read_bytes() == path.read_bytes())
            )
            for c in calls
        )
    if flow == "dc":
        if name in ("Sdc", "Tcl"):
            return handed(["source", "-echo"])
        fmt = {"Verilog": "verilog", "SystemVerilog": "sverilog", "Vhdl": "vhdl"}[name]
        return handed(["analyze", "-format", fmt])
    raise AssertionError(flow)


@needs_tclsh
def test_vivado_project_filters_testbench_sources_in_design_order(tmp_path, monkeypatch):
    root = tmp_path / "design"
    root.mkdir()
    cls = registered_flows["vivado_project"][1]
    design, files = _every_type(root, cls)
    entries = [{"file": str(p), "type": m.name} for m, p in reversed(list(files.items()))]
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": [], "top": "top", "clock": "clk"},
        tb={"sources": entries, "top": "tb"},
    )
    calls = _calls(
        "vivado_project",
        design,
        _settings(root, "vivado_project"),
        tmp_path,
        monkeypatch,
        elements=True,
    )
    expected = [str(p) for m, p in reversed(list(files.items())) if m in cls.reads_sources]
    sim = [c for c in calls if c[:4] == ["add_files", "-fileset", "sim_1", "-norecurse"]]
    assert len(sim) == 1
    all_paths = {str(p) for p in files.values()}
    assert [arg for arg in sim[0][4:] if arg in all_paths] == expected
    for member in (SourceType.SystemVerilog, SourceType.Vhdl):
        fmt = "SystemVerilog" if member is SourceType.SystemVerilog else "VHDL"
        assert any(
            first[0] == "get_files"
            and str(files[member]) in first[1:]
            and second[:3] == ["set_property", "FILE_TYPE", fmt]
            for first, second in zip(calls, calls[1:])
        )


@needs_tclsh
def test_the_script_completion_oracle_catches_an_early_read_failure(tmp_path, monkeypatch):
    root = tmp_path / "design"
    root.mkdir()
    design, _ = _every_type(root, registered_flows["vivado_synth"][1])
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "read_verilog")
    with pytest.raises(AssertionError, match="did not finish its script"):
        _calls("vivado_synth", design, _settings(root, "vivado_synth"), tmp_path, monkeypatch)


@needs_tclsh
@pytest.mark.parametrize("flow", FLOWS + ["yosys_fpga"])
@pytest.mark.parametrize(
    "member,suffix", [(SourceType.SystemVerilog, "v"), (SourceType.Vhdl, "dat")]
)
def test_explicit_language_reaches_the_tool_command(flow, member, suffix, tmp_path, monkeypatch):
    cls = registered_flows[flow][1]
    if member not in cls.reads_sources:
        pytest.skip("ISE does not support SystemVerilog")
    root = tmp_path / "design"
    root.mkdir()
    path = root / f"legacy.{suffix}"
    path.write_text("a language source\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": [{"file": str(path), "type": member.name}],
            "top": "top",
            "clock": "clk",
        },
    )
    if flow == "yosys_fpga":
        settings = {**_settings(root, flow), "systemverilog": "default"}
        instance = cls(cls.Settings.from_input(settings), design, tmp_path / "render")
        instance.init()
        template = instance.jinja_env.get_template("read_files.ys")
        script = template.render(
            settings=instance.settings, design=design, defines=[], ghdl_args=[], parameters={}
        )
        prefix = "read_verilog -sv" if member is SourceType.SystemVerilog else "ghdl "
        assert any(line.startswith(prefix) and str(path) in line for line in script.splitlines())
    else:
        calls = _calls(flow, design, _settings(root, flow), tmp_path, monkeypatch)
        if flow == "ise_synth":
            added = [Path(c[2]) for c in calls if c[:2] == ["xfile", "add"] and c[-1] == "-copy"]
            assert any(p.suffix == ".vhd" and p.read_bytes() == path.read_bytes() for p in added)
        else:
            assert _read_call(flow, member, path, calls), (flow, member, calls)


# ------------------------------------------------- what a flow's code and templates read, scanned

#: The one selection: what a flow reads of the design's sources is what it declares.
SELECTION = "sources_read"
#: Calls and attributes that read the design's sources around the declaration.
DIRECT_CALLS = frozenset({"sources_of_type", "sim_sources_of_type", "header_dirs"})
DIRECT_ATTRIBUTES = frozenset({"sources", "sim_sources"})
#: Methods that judge a design instead of choosing what a tool reads: the contract's own checks
#: (`check_design_supported` and what it asks) and the selection itself. A read in one of them
#: hands nothing to a tool.
JUDGING_METHODS = frozenset(
    {"check_design_supported", "runs_without_testbench_top", "has_cpp_driver", SELECTION}
)
#: The types `Design.header_dirs` reads: the include search path of a flow that declares both.
HEADER_TYPES = frozenset({SourceType.VerilogHeader, SourceType.SVHeader})
#: Reads around the declaration, as `<module:qualified name, or template name>: <what>`, each
#: with why it hands the tool nothing the flow does not declare. A new one fails
#: `test_a_flow_selects_what_it_reads_only_through_sources_read` until it is reviewed here, and
#: an entry whose read is gone fails `test_every_reviewed_direct_read_is_still_there`.
REVIEWED_DIRECT_READS: dict[str, str] = {
    "xeda.flows.vivado.vivado_synth:constraint_files: sources": "the Xdc and Sdc sources, "
    "which every flow that calls it (`vivado_synth`, `vivado_alt_synth`, `vivado_project`) declares",
    "xeda.flows.bsc:BscSim._tb_top: sources": "whether the design has a testbench at all, which "
    "decides the module simulated",
    "xeda.flows.bsc:BscSim.run: sources": "which of the Bluespec sources `sources_read` selected "
    "are the testbench's, to find the package of its top",
    "xeda.flows.yosys.cxx_rtl:YosysSim.run: sources": "whether the testbench is VHDL, which "
    "decides the unit GHDL elaborates",
    "xeda.flows.ghdl:GhdlSynth.synth_args: sources_of_type": "the VHDL sources of a one-shot "
    "elaboration, a branch no caller takes: every one passes `one_shot_elab=False`",
    "xeda.cocotb:Cocotb.env: sources": "whether a cocotb testbench lists any source",
    "xeda.cocotb:Cocotb.env: sources_of_type": "the `Cocotb` (Python) sources, whose last names "
    "the test module cocotb imports: no simulator reads them",
    "xeda.flows.modelsim.sim_evidence:parse_modelsim_evidence: sim_sources": "the VHDL sources, "
    "to tell a VHDL `std.env.stop` in the log from a Verilog `$stop`",
    "xeda.flows.vivado.sim_evidence:parse_xsim_evidence: sim_sources": "the VHDL sources, to "
    "tell a VHDL `std.env.stop` in the log from a Verilog `$stop`",
}

DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

TEMPLATE_SELECTION = re.compile(r"\bsources_read\s*\(")
TEMPLATE_DIRECT = re.compile(
    r"\b(sources_of_type|sim_sources_of_type|sim_sources|header_dirs)\b|\.sources\b"
)


def python_source_reads(
    source: str, skip_classes=(), skip_functions=(), module_level: bool = True
) -> list[tuple[str, int, str]]:
    """Where the Python in `source` reads the design's sources, as `(qualified name, line,
    what)`, `what` being `SELECTION` or the name of a direct read. Reads inside a
    `JUDGING_METHODS` method are not counted; top-level classes in `skip_classes` (other flows'
    classes in the same module) and top-level functions in `skip_functions` (helpers the flow
    does not reach) are not visited, and neither are the module's other top-level statements
    unless `module_level`."""
    tree = ast.parse(source)
    found: list[tuple[str, int, str]] = []
    skip = set(skip_classes)
    skip_defs = set(skip_functions)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_Module(self, node: ast.Module) -> None:
            for statement in node.body:
                if module_level or isinstance(statement, DEFINITIONS):
                    self.visit(statement)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node in tree.body and node.name in skip:
                return
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node) -> None:
            if node.name in JUDGING_METHODS or (node in tree.body and node.name in skip_defs):
                return
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def _hit(self, node: ast.AST, what: str) -> None:
            found.append((".".join(self.scope) or "<module>", node.lineno, what))

        def visit_Attribute(self, node: ast.Attribute) -> None:
            # a call or not: `design.sources_of_type(...)`, and `read = design.sources_of_type`
            if node.attr == SELECTION or node.attr in DIRECT_CALLS | DIRECT_ATTRIBUTES:
                self._hit(node, node.attr)
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            # a function of these names called or passed by itself
            if node.id == SELECTION or node.id in DIRECT_CALLS:
                self._hit(node, node.id)

    Visitor().visit(tree)
    return found


def template_source_reads(text: str) -> list[tuple[int, str]]:
    """Where a template reads the design's sources, as `(line, what)`."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if TEMPLATE_SELECTION.search(line):
            found.append((number, SELECTION))
        for match in TEMPLATE_DIRECT.finditer(line):
            found.append((number, match.group(0).lstrip(".")))
    return found


def _package(module_name: str) -> str:
    """The package that a relative import in `module_name` is relative to."""
    module = sys.modules.get(module_name)
    if module is not None:
        return module.__package__ or ""
    return module_name.rpartition(".")[0]


def _loaded_source(module_name: str) -> str | None:
    """The source of an imported module of the package, or None for any other."""
    module = sys.modules.get(module_name)
    if module is None or not module_name.startswith("xeda."):
        return None
    try:
        return inspect.getsource(module)
    except (OSError, TypeError):
        return None


class _Module:
    """One parsed module: its top-level functions and what it imports."""

    def __init__(self, name: str, source: str, load) -> None:
        self.name = name
        self.tree = ast.parse(source)
        self.functions = {
            node.name: node
            for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        #: a name the module imports -> the module and the name it imports it as
        self.names: dict[str, tuple[str, str]] = {}
        #: a name the module gives to another module of the package: `from . import helpers`
        self.modules: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                base = self._absolute(node)
                for alias in node.names if base else ():
                    local = alias.asname or alias.name
                    if load(f"{base}.{alias.name}") is not None:
                        self.modules[local] = f"{base}.{alias.name}"
                    else:
                        self.names[local] = (base, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and load(alias.name) is not None:
                        self.modules[alias.asname] = alias.name

    def _absolute(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        try:
            return importlib.util.resolve_name(
                "." * node.level + (node.module or ""), _package(self.name)
            )
        except (ImportError, ValueError):
            return ""


def _uses(node: ast.AST):
    """What the code of `node` refers to, as `(qualifier, name)`: `(None, "f")` for `f`,
    `("m", "f")` for `m.f`. A `JUDGING_METHODS` method hands no tool anything, so what only it
    calls is not reached."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if current.name in JUDGING_METHODS:
                continue
        if isinstance(current, ast.Name):
            yield None, current.id
        elif isinstance(current, ast.Attribute) and isinstance(current.value, ast.Name):
            yield current.value.id, current.attr
        stack.extend(ast.iter_child_nodes(current))


def flow_source_reads(
    sources: dict[str, str], classes: dict[str, set[str]], load=_loaded_source
) -> list[tuple[str, int, str]]:
    """Every read of the design's sources by the Python of one flow, as `(module:qualified
    name, line, what)`. `sources` maps the modules of the flow's classes to their text and
    `classes` to the names of those classes in them. The flow's code is its classes, the
    module-level statements of their modules (which run when the module is imported), and the
    module-level functions that code reaches: by their name, through the module's own functions
    and through the modules it imports them from (`load` gives an imported module's text), and
    through the functions those call. Another flow's class in a flow's module, and a helper that
    no code of the flow reaches (`VivadoSynth` calls `constraint_files`, `VivadoImpl` in the
    same module does not), are no part of it."""
    parsed: dict[str, _Module | None] = {}

    def module(name: str) -> _Module | None:
        if name not in parsed:
            text = sources[name] if name in sources else load(name)
            parsed[name] = _Module(name, text, load) if text is not None else None
        return parsed[name]

    reached: dict[str, set[str]] = {}
    pending: list[tuple[str, ast.AST]] = []

    def reach(owner: str, node: ast.AST) -> None:
        """Mark what the code of `node`, in module `owner`, calls, and queue it."""
        here = module(owner)
        assert here is not None
        for qualifier, name in _uses(node):
            target: tuple[str, str] | None = None
            if qualifier is None and name in here.functions:
                target = (owner, name)
            elif qualifier is None and name in here.names:
                target = here.names[name]
            elif qualifier in here.modules:
                target = (here.modules[qualifier], name)
            there = module(target[0]) if target else None
            if there is None or target is None or target[1] not in there.functions:
                continue
            if target[1] not in reached.setdefault(target[0], set()):
                reached[target[0]].add(target[1])
                pending.append((target[0], there.functions[target[1]]))

    for name in sources:
        here = module(name)
        assert here is not None
        for statement in here.tree.body:
            if isinstance(statement, ast.ClassDef):
                if statement.name in classes.get(name, ()):
                    reach(name, statement)
            elif not isinstance(statement, DEFINITIONS):
                reach(name, statement)
    while pending:
        owner, node = pending.pop()
        reach(owner, node)

    found = []
    for name in sorted({*sources, *reached}):
        here = module(name)
        assert here is not None
        text = sources[name] if name in sources else load(name)
        assert text is not None
        own = classes.get(name, set())
        found += [
            (f"{name}:{where}", line, what)
            for where, line, what in python_source_reads(
                text,
                skip_classes={
                    n.name
                    for n in here.tree.body
                    if isinstance(n, ast.ClassDef) and n.name not in own
                },
                skip_functions=set(here.functions) - reached.get(name, set()),
                module_level=name in sources,
            )
        ]
    return found


def source_reads(cls: type[Flow]) -> list[tuple[str, int, str]]:
    """Every read of the design's sources by `cls`'s code -- the classes of its MRO, the code
    of their modules outside any class, and the helpers that code reaches
    (`flow_source_reads`) -- and by the templates it can render, as `(where, line, what)`:
    `where` is `module:qualified name`, or `template <name>`. `Flow` itself, which holds the
    contract (the selection, its template global and the check), is no flow's code."""
    names_by_module: dict[str, set[str]] = {}
    for klass in _mro(cls):
        if klass is not Flow:
            names_by_module.setdefault(klass.__module__, set()).add(klass.__name__)
    sources = {name: inspect.getsource(sys.modules[name]) for name in names_by_module}
    found = flow_source_reads(sources, names_by_module)
    for name, text in sorted(reachable_templates(cls).items()):
        found += [(f"template {name}", line, what) for line, what in template_source_reads(text)]
    return found


def test_the_scan_sees_each_way_of_reading_the_design_s_sources():
    """The teeth of the scan: a scan that finds nothing proves nothing."""
    reads = {
        "rtl.sources": ("def run(self): return self.design.rtl.sources", "sources"),
        "tb.sources": ("def run(self): return [s for s in self.design.tb.sources]", "sources"),
        "a part by name": ("def run(d, p): return getattr(d, p).sources", "sources"),
        "sim_sources": ("def run(self): return self.design.sim_sources", "sim_sources"),
        "sources_of_type": ("def run(d): return d.sources_of_type('*')", "sources_of_type"),
        "sim_sources_of_type": ("def f(d): return d.sim_sources_of_type(1)", "sim_sources_of_type"),
        "header_dirs": ("def run(d): return d.header_dirs(tb=True)", "header_dirs"),
        "the selection": ("def run(self): return self.sources_read(tb=True)", SELECTION),
        "a method taken": (
            "def run(d): read = d.sources_of_type; return read('*')",
            "sources_of_type",
        ),
        "a function passed": ("def run(f): return map(header_dirs, f)", "header_dirs"),
    }
    for what, (source, expected) in reads.items():
        assert [hit[2] for hit in python_source_reads(source)] == [expected], what
    nested = "class F:\n    def run(self):\n        return self.design.rtl.sources\n"
    assert python_source_reads(nested) == [("F.run", 3, "sources")]
    clean = {
        "a judging method": "class F:\n    def check_design_supported(cls, d):\n"
        "        return d.tb.sources\n",
        "a skipped class": "class Other:\n    def run(self): return self.design.rtl.sources\n",
        "no read": "def run(self): return self.design.rtl.top",
    }
    for what, source in clean.items():
        assert not python_source_reads(source, skip_classes=["Other"]), what

    assert template_source_reads("{% for s in sources_read(rtl=true, tb=true) %}") == [
        (1, SELECTION)
    ]
    for line, expected in [
        ("{% for s in design.sim_sources %}", "sim_sources"),
        ("{% for s in design.rtl.sources %}", "sources"),
        ("{% set v = design.sources_of_type('Vhdl', rtl=true) %}", "sources_of_type"),
    ]:
        assert template_source_reads(line) == [(1, expected)], line
    assert not template_source_reads("{{ design.rtl.top }} {{ settings.sdc_files }}")


HELPERS = """
def reads(design):
    return design.rtl.sources


def reads_through(design):
    return reads(design)


def reads_unused(design):
    return design.tb.sources


class Calling:
    def run(self):
        return reads(self.design)


class Chained:
    def run(self):
        return reads_through(self.design)


class Idle:
    def run(self):
        return self.design.rtl.top


class Judging:
    @classmethod
    def check_design_supported(cls, design):
        return reads(design)
"""


def test_the_scan_counts_a_module_helper_for_the_flows_that_reach_it_only():
    """A helper in a flow's module is that flow's code only if the flow's code reaches it, so
    one flow in a module does not answer for another's helper, and a flow that does call it is
    still held to the review."""

    def reads_of(name: str) -> list[tuple[str, int, str]]:
        return flow_source_reads({"m": HELPERS}, {"m": {name}}, load=lambda _: None)

    assert reads_of("Calling") == [("m:reads", 3, "sources")]
    assert reads_of("Chained") == [("m:reads", 3, "sources")], "through another helper"
    assert reads_of("Idle") == []
    assert reads_of("Judging") == [], "a judging method hands a tool nothing"
    # a module-level statement runs on import, so what it calls is reached
    statement = HELPERS + "TABLE = {'read': reads}\n"
    assert flow_source_reads({"m": statement}, {"m": {"Idle"}}) == [("m:reads", 3, "sources")]


def test_the_scan_follows_a_helper_through_the_module_it_is_imported_from():
    helpers = "def reads(design):\n    return design.rtl.sources\n\n\ndef idle(design):\n    return design.tb.sources\n"
    modules = {"pkg.helpers": helpers, "pkg.sub.other": "def reads(design):\n    return 1\n"}
    for importing, calling in {
        "from ..helpers import reads": "reads(self.design)",
        "from ..helpers import reads as read": "read(self.design)",
        "from .. import helpers": "helpers.reads(self.design)",
        "import pkg.helpers as helpers": "helpers.reads(self.design)",
    }.items():
        flow = f"{importing}\n\n\nclass Calling:\n    def run(self):\n        return {calling}\n"
        found = flow_source_reads(
            {"pkg.sub.flow": flow}, {"pkg.sub.flow": {"Calling"}}, modules.get
        )
        assert found == [("pkg.helpers:reads", 2, "sources")], importing
    # the same name in a module the flow does not import from is not its helper
    flow = "from .other import reads\n\n\nclass Calling:\n    def run(self):\n        return reads(1)\n"
    assert not flow_source_reads({"pkg.sub.flow": flow}, {"pkg.sub.flow": {"Calling"}}, modules.get)


def test_a_helper_of_a_shared_module_is_the_code_of_the_flows_that_call_it():
    """`VivadoSynth`, `VivadoAltSynth` and `VivadoProject` call `constraint_files`, which reads
    the Xdc and Sdc sources; `VivadoImpl` shares a module and a base class with the first, calls
    it not, and takes its constraints through a declared input."""
    helper = "xeda.flows.vivado.vivado_synth:constraint_files"
    for name, calls in {
        "vivado_synth": True,
        "vivado_alt_synth": True,
        "vivado_project": True,
        "vivado_impl": False,
    }.items():
        places = {where for where, _, _ in source_reads(registered_flows[name][1])}
        assert (helper in places) is calls, name


def test_a_flow_that_gains_a_call_of_that_helper_is_found_again():
    """The converse, on the real code: `VivadoImpl` with a class that calls `constraint_files`
    reads the design's sources, and so would need to declare what it reads."""
    cls = registered_flows["vivado_impl"][1]
    names: dict[str, set[str]] = {}
    for klass in _mro(cls):
        if klass is not Flow:
            names.setdefault(klass.__module__, set()).add(klass.__name__)
    sources = {name: inspect.getsource(sys.modules[name]) for name in names}
    assert not flow_source_reads(sources, names)
    module = cls.__module__
    sources[module] += (
        "\n\nfrom .vivado_synth import constraint_files\n\n\n"
        "class Probe:\n    def run(self):\n        return constraint_files(self, self.settings)\n"
    )
    names[module].add("Probe")
    found = flow_source_reads(sources, names)
    assert [(where, what) for where, _, what in found] == [
        ("xeda.flows.vivado.vivado_synth:constraint_files", "sources")
    ]


def test_the_scan_reaches_a_flow_s_templates_and_its_bases_code():
    """`dc` selects in its template only, `vivado_project` in its code and its template; the
    contract's own selection and checks, in `Flow`, count for no flow."""
    dc = source_reads(registered_flows["dc"][1])
    assert {where for where, _, _ in dc} == {"template dc_script.tcl"}
    project = {where for where, _, _ in source_reads(registered_flows["vivado_project"][1])}
    assert "template vivado_project.tcl" in project
    assert any(where.startswith("xeda.flows.vivado.vivado_project:") for where in project)
    assert not any(where.startswith("xeda.flow.flow:") for where in project)


@pytest.mark.parametrize(("cls", "name"), flow_classes(), ids=[n for _, n in flow_classes()])
def test_a_flow_that_reads_the_design_s_sources_declares_what_it_reads(cls, name):
    """A flow that reads the design's sources, in code or in a template, declares the types it
    reads, and a flow that declares them selects with `sources_read()`: a declaration that
    selects nothing would refuse designs for a flow that reads none of their sources."""
    if not cls.__module__.startswith("xeda."):
        pytest.skip("a flow outside the package")
    reads = source_reads(cls)
    listing = "\n".join(f"{where}:{line}: {what}" for where, line, what in reads)
    if cls.reads_sources is None:
        assert not reads, f"{name} reads the design's sources and declares no reads_sources:\n" + (
            listing
        )
    else:
        assert any(
            what == SELECTION for _, _, what in reads
        ), f"{name} declares reads_sources and never selects with sources_read():\n{listing}"


@pytest.mark.parametrize(("cls", "name"), flow_classes(), ids=[n for _, n in flow_classes()])
def test_a_flow_selects_what_it_reads_only_through_sources_read(cls, name):
    """Whatever a flow hands its tool of the design's sources goes through `sources_read()`, so
    its declaration is what the tool gets: no other read of the design's sources, unless it is
    reviewed in `REVIEWED_DIRECT_READS` as choosing nothing a tool reads."""
    if not cls.__module__.startswith("xeda."):
        pytest.skip("a flow outside the package")
    headers = cls.reads_sources is not None and HEADER_TYPES <= cls.reads_sources
    direct = [
        f"{where}:{line}: {what}"
        for where, line, what in source_reads(cls)
        if what != SELECTION and f"{where}: {what}" not in REVIEWED_DIRECT_READS
        # the include search path of the headers the flow declares
        and not (what == "header_dirs" and headers)
    ]
    assert not direct, (
        f"{name} reads the design's sources around its declaration; select with "
        "sources_read() (Flow.reads_sources):\n" + "\n".join(direct)
    )


#: The code a flow calls besides its own classes, whose reads of the design's sources the
#: sweep below holds to the same review: every module of the flows and of their base classes,
#: and cocotb's.
def _holds_code(name: str) -> bool:
    """False for a module with no source text: the empty `templates` packages that only name a
    directory of data, and which any test that walks the packages imports."""
    path = getattr(sys.modules[name], "__file__", None)
    return bool(path) and bool(Path(path).read_text(encoding="utf-8").strip())


HELPER_MODULES = sorted(
    {
        name
        for name in sys.modules
        if (name.startswith(("xeda.flows.", "xeda.flow.")) or name == "xeda.cocotb")
        and _holds_code(name)
    }
)


def helper_reads(module_name: str) -> list[tuple[str, int, str]]:
    """Every read of the design's sources by the code of `module_name`, every class included."""
    source = inspect.getsource(sys.modules[module_name])
    return [
        (f"{module_name}:{where}", line, what) for where, line, what in python_source_reads(source)
    ]


def test_the_helpers_cover_the_flows_packages_and_cocotb():
    assert "xeda.cocotb" in HELPER_MODULES
    assert "xeda.flows.vivado.sim_evidence" in HELPER_MODULES
    assert "xeda.flow.sim" in HELPER_MODULES


@pytest.mark.parametrize("module_name", HELPER_MODULES)
def test_no_helper_reads_the_design_s_sources_around_a_declaration(module_name):
    """What a flow's helpers read of the design's sources -- the evidence readers a simulator
    calls, cocotb's support, another flow's static method -- is held to the review as the
    flows' own code is: a read other than `sources_read()` is reviewed, or it is a finding."""
    # a flow's own code is judged with its declaration, by the tests above
    judged = {
        f"{where}: {what}" for cls, _ in flow_classes() for where, _, what in source_reads(cls)
    }
    direct = [
        f"{where}:{line}: {what}"
        for where, line, what in helper_reads(module_name)
        if what != SELECTION
        and f"{where}: {what}" not in REVIEWED_DIRECT_READS
        and f"{where}: {what}" not in judged
    ]
    assert not direct, (
        "a read of the design's sources around a flow's declaration; select with "
        "sources_read() or review it in REVIEWED_DIRECT_READS:\n" + "\n".join(direct)
    )


def test_every_reviewed_direct_read_is_still_there():
    places = {
        f"{where}: {what}" for cls, _ in flow_classes() for where, _, what in source_reads(cls)
    }
    places |= {
        f"{where}: {what}" for name in HELPER_MODULES for where, _, what in helper_reads(name)
    }
    gone = sorted(set(REVIEWED_DIRECT_READS) - places)
    assert not gone, f"reviewed reads that are gone, delete their entries: {gone}"


# ------------------------------------------------------------- the examples and the flows they name

EXAMPLES = Path(__file__).parent.parent / "examples"
EXAMPLE_FILES = sorted(
    path
    for path in EXAMPLES.rglob("*")
    if path.suffix in (".yaml", ".yml", ".toml", ".json") and "xeda_run" not in path.parts
)


def _example_requests(path: Path):
    """Each design `path` holds, with every flow its sections or its project's name and the
    project's section for that flow."""
    from xeda.xedaproject import XedaProject

    if path.stem == "xedaproject":
        project = XedaProject.from_file(path)
        designs = [project.get_design(i) for i in range(len(project.designs))]
        shared = project.flows or {}
    else:
        designs, shared = [Design.from_file(path)], {}
    for design in designs:
        assert design is not None
        for flow in sorted(set(design.flow or {}) | set(shared)):
            yield design, flow, shared.get(flow, {})


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: str(p.relative_to(EXAMPLES)))
def test_every_example_plans_for_every_flow_it_names(path, tmp_path, monkeypatch):
    """No example is refused by a flow it is meant for: each plans, its producers included, for
    every flow a section of the design or of its project names."""
    monkeypatch.chdir(path.parent)
    requests = list(_example_requests(path))
    for design, flow, settings in requests:
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        runner.plan(flow, design, flow_settings=settings)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


def test_the_examples_name_flows_of_every_kind():
    """The sweep above covers simulators, synthesis and a chain: an oracle over few flows
    proves little."""
    named = {flow for path in EXAMPLE_FILES for _, flow, _ in _example_requests(path)}
    assert {"verilator", "ghdl_sim", "yosys_sim", "bsc_sim", "openroad", "nextpnr"} <= named
