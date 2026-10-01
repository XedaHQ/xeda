"""A flow that hands its tool the design's sources itself reads the types it declares
(`Flow.reads_sources`), and only those: a source of another type is passed over -- a `.lpf` in a
design built by Vivado -- or, when it is a language the flow cannot read, refused by name at
launch. No source becomes a tool command by its type's name (Quartus's `XDC_FILE`,
`MEMORYFILE_FILE`: ST4), and no source crashes a template (ST3): the sweep runs every such flow's
scripts under the fake tools with one source of every type."""

import re
from pathlib import Path

import pytest

import xeda
from xeda import Design
from xeda.design import LANGUAGE_TYPES, SOURCE_SUFFIXES, SourceType
from xeda.flow import Flow, FlowSettingsException
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner

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
        parts = registered_flows[flow][1].reads_source_parts
        assert parts == (("rtl", "tb") if flow == "vivado_project" else ("rtl",)), flow


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
