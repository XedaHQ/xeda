"""A flow that hands its tool the design's sources itself reads the types it declares
(`Flow.reads_sources`), and only those: a source of another type is passed over -- a `.lpf` in a
design built by Vivado -- or, when it is a language the flow cannot read, refused by name at
launch. A design none of whose sources the flow reads is refused too. No source becomes a tool
command by its type's name (Quartus's `XDC_FILE`, `MEMORYFILE_FILE`), and no source crashes a
template: the sweep runs every such flow's scripts under the fake tools with one source of every
type."""

import re
from pathlib import Path
from typing import ClassVar

import pytest

import xeda
from xeda import Design
from xeda.board import WithFpgaBoardSettings
from xeda.design import LANGUAGE_TYPES, SOURCE_SUFFIXES, SourceType
from xeda.flow import Flow, FlowSettingsException, In
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr

from .settings_samples import flow_classes
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
}
REFUSED = [
    (flow, member, part)
    for flow, accepted in EXPECTED_READS.items()
    for member in sorted(LANGUAGE_TYPES, key=lambda m: m.name)
    if member.name not in accepted.split()
    for part in (("rtl", "tb") if flow == "vivado_project" else ("rtl",))
]
FLOWS_DIR = Path(xeda.__file__).parent / "flows"


def _suffix(member: SourceType) -> str:
    """A suffix `member` is inferred from, or `dat` for a member given by `type` only."""
    return next((s for s, (m, _v) in sorted(SOURCE_SUFFIXES.items()) if m is member), "dat")


def _settings(root: Path, flow: str) -> dict:
    """The least `flow` runs with here."""
    if flow == "dc":
        lib = root / "pdk" / "cells.db"
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_text("")
        return {"target_libraries": [str(lib)], "clock_period": 10.0}
    fpga = {
        "quartus": "10CL016YU256C6G",
        "ise_synth": "xc6slx9-2-tqg144",
        "diamond_synth": "LFE5U-25F-6BG256C",
    }.get(flow, "xc7a12tcsg325-1")
    return {"fpga": fpga, "clock_period": 10.0}


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
            "clock_port": "clk",
        },
    )
    return design, files


def test_every_contract_flow_declares_what_it_reads():
    for flow in FLOWS + ["yosys_fpga"]:
        reads = registered_flows[flow][1].reads_sources
        assert reads == frozenset(SourceType[name] for name in EXPECTED_READS[flow].split()), flow
        parts = registered_flows[flow][1].design_parts
        assert parts == (frozenset({"rtl", "tb"}) if flow == "vivado_project" else {"rtl"}), flow


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
    (root / "top.v").write_text("module top; endmodule\n")
    source = root / f"unsupported.{_suffix(member)}"
    source.write_text("a source\n")
    rtl = {"sources": ["top.v"], "top": "top"}
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
#: The flows that hand their tool the design's sources themselves, and so declare what they read.
READING_FLOWS = [cls for cls in PRODUCT_FLOWS if cls.reads_sources is not None]
#: The flows that choose their inputs in their own code, or declare them as inputs.
CHOOSING_FLOWS = [cls for cls in PRODUCT_FLOWS if cls.reads_sources is None]
READS_ONE_TYPE = [(cls, member) for cls in READING_FLOWS for member in sorted(cls.reads_sources)]


#: What the refusal of a default producer adds: the source that would have replaced the node.
SKIPPED_BY_A_NETLIST = "a JsonNetlist source would supply nextpnr's netlist and skip yosys_fpga"


def _edif_only(root: Path) -> Design:
    """A design whose only source is a netlist that no flow reads."""
    root.mkdir(exist_ok=True)
    (root / "top.edf").write_text("(edif top)\n")
    return Design(name="d", design_root=root, rtl={"sources": ["top.edf"], "top": "top"})


def test_the_flows_that_read_the_designs_sources_are_the_eight_with_a_contract():
    assert sorted(cls.name for cls in READING_FLOWS) == sorted(EXPECTED_READS)
    assert CHOOSING_FLOWS, "the flows that choose their inputs themselves are not covered"


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


@pytest.mark.parametrize("flow_class", CHOOSING_FLOWS, ids=lambda cls: cls.name)
def test_a_flow_that_chooses_its_inputs_itself_is_not_refused_by_that_rule(flow_class, tmp_path):
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
    by default: no one source replaces it, so the refusal suggests none."""
    monkeypatch.chdir(tmp_path)
    design = _edif_only(tmp_path / "design")
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(
            "vivado_power", design, flow_settings={"fpga": "xc7a12tcsg325-1", "clock_period": 10.0}
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
        name="d", design_root=root, rtl={"sources": names, "top": "top", "clock_port": "clk"}
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
        rtl={"sources": ["top.v", *headers], "top": "top", "clock_port": "clk"},
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
        rtl={"sources": [], "top": "top", "clock_port": "clk"},
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
            "clock_port": "clk",
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
