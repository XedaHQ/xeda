"""A path a user names reaches a TCL-scripted tool whole, however it is spelled.

A flow writes design sources and the constraint and script files a design or setting names into
a TCL script. Unquoted or merely double-quoted there, a path with a space was split, `$v` was read
as a variable, and `[x]` ran as a command -- Vivado's TCL even starts an external program whose
name begins with `x`; only a numeric bus index (`fifo[1].v`) got through, by an accident of
Vivado's `unknown`. The templates write such a path with `tcl_word` (a whole argument) or
`tcl_quote` (inside a message). PDK files and xeda's own run-directory paths are out of scope.
"""

import re
import shutil
from pathlib import Path
from typing import List

import pytest

from xeda import Design
from xeda.flow.flow import FlowSettingsError, registered_flows
from xeda.flows import dc
from xeda.flow_runner import DefaultRunner

from .tool_utils import fake_calls, use_fake_tools

FLOWS_DIR = Path(__file__).parent.parent / "src" / "xeda" / "flows"
TCLSH = shutil.which("tclsh")

# Template expressions that name a file a user named: a design source, or a constraint or script
# file from the design or the flow's settings.
USER_NAMED = re.compile(
    r"^(src|src\.file|file|xdc_file|constraint_file|ucf_file|sdc_file|sdc|make_tracks_tcl"
    r"|design\.(rtl|tb)\.sources"
    r"|settings\.(xcf_file|post_detailed_route_tcl|floorplan_def|footprint|sig_map_file"
    r"|footprint_tcl|io_constraints|macro_wrappers|macro_placement_file))\s*(\||$)"
)
EXPRESSION = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}")


def _tcl_templates():
    """Find Tcl templates that handle user supplied paths."""
    for template in sorted(FLOWS_DIR.glob("*/templates/*.tcl")):
        if template.parts[-3] != "yosys":  # yosys' own filters: tests/test_yosys_templates.py
            yield template


def test_every_user_named_path_in_a_tcl_template_is_quoted() -> None:
    """Every template, including those of flows no test can run here (OpenROAD, and flows whose
    templates do not render at all yet)."""
    unquoted = []
    for template in _tcl_templates():
        for n, line in enumerate(template.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for expression in EXPRESSION.findall(line):
                if USER_NAMED.match(expression) and not re.search(
                    r"tcl_(word|quote|list)", expression
                ):
                    unquoted.append(f"{template.relative_to(FLOWS_DIR)}:{n}: {expression}")
    assert not unquoted, "\n".join(unquoted)


def _odd(name: str) -> str:
    """`name` with a space, brackets and a dollar sign in its stem: `c.xdc` -> `c [x] $v.xdc`."""
    path = Path(name)
    return str(path.with_stem(f"{path.stem} [x] $v"))


def _design(root: Path) -> Design:
    """Create a design with paths requiring Tcl quoting."""
    rtl = [_odd(f"rtl/{n}") for n in ("pkg.vhd", "core.v", "top.vhd")]
    tb = [_odd("tb/tb.vhd")]
    for rel in rtl + tb:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("-- a source\n")
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": rtl, "top": "top", "clock_port": "clk"},
        tb={"sources": tb, "top": "tb"},
    )


def _file(root: Path, name: str) -> str:
    """Write an input file with a chosen path."""
    path = root / "constr" / _odd(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a user's file\n")
    return str(path)


# flow -> (its settings, the settings' user-named files it must hand a tool)
def _cases(root: Path) -> dict[str, tuple[dict, list[str]]]:
    """Provide path cases for Tcl quoting checks."""
    xdc, hook, ucf, xcf, sdc = (
        _file(root, n) for n in ("c.xdc", "h.tcl", "c.ucf", "c.xcf", "c.sdc")
    )
    sdf_max = _file(root, "timing.sdf")
    lib = root / "pdk" / "cells.db"  # a PDK file: out of scope, so an ordinary name
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text("")
    fpga = {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0}
    return {
        "vivado_synth": ({**fpga, "xdc_files": [xdc], "tcl_files": [hook]}, [xdc, hook]),
        "vivado_alt_synth": ({**fpga, "xdc_files": [xdc]}, [xdc]),
        "vivado_project": ({**fpga, "xdc_files": [xdc], "tcl_files": [hook]}, [xdc, hook]),
        "vivado_sim": ({}, []),
        "quartus": ({"fpga": "10CL016YU256C6G", "clock_period": 10.0}, []),
        "ise_synth": (
            {"fpga": "xc6slx9-2-tqg144", "clock_period": 10.0, "ucf_files": [ucf], "xcf_file": xcf},
            [ucf, xcf],
        ),
        "diamond_synth": ({"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0}, []),
        "dc": (
            {
                "sdc_files": [sdc],
                "hooks": {"post_link": hook},
                "target_libraries": [str(lib)],
                "clock_period": 10.0,
            },
            [sdc, hook],
        ),
        "modelsim": (
            {"sdf": {"max": sdf_max, "root": "tb/uut"}, "lib_paths": [("vendor_lib", None)]},
            [f"tb/uut={sdf_max}"],
        ),
    }


def _calls(
    flow: str, design: Design, settings: dict, tmp_path: Path, monkeypatch, elements: bool = False
) -> List[List[str]]:
    """Run `flow` with the fake tools, which run each script it hands them under tclsh: every
    tool command the scripts ran, as its arguments, in order (`tool_utils.fake_calls`)."""
    use_fake_tools(monkeypatch)
    run_dir = tmp_path / "run"
    try:
        DefaultRunner(run_dir).run_flow(registered_flows[flow][1], design, settings)
    except Exception:  # pylint: disable=broad-except
        pass  # a fake writes no reports for some flows to parse; its record is what counts
    calls = fake_calls(run_dir, elements)
    assert calls, f"{flow} ran no tool command"
    return calls


needs_tclsh = pytest.mark.skipif(not TCLSH, reason="tclsh is needed to run the TCL scripts")


@needs_tclsh
@pytest.mark.parametrize(
    "flow",
    [
        "vivado_synth",
        "vivado_alt_synth",
        "vivado_project",
        "vivado_sim",
        "quartus",
        "ise_synth",
        "diamond_synth",
        "dc",
        "modelsim",
    ],
)
def test_a_user_named_path_reaches_the_tool_whole(flow, tmp_path, monkeypatch) -> None:
    """Each flow's scripts, rendered as a run renders them and run under tclsh with the tool's
    commands recorded: every source, and every user-named file the flow reads, is one argument,
    exactly the path."""
    design = _design(tmp_path / "design")
    settings, files = _cases(tmp_path / "design")[flow]
    calls = _calls(flow, design, settings, tmp_path, monkeypatch, elements=True)
    arguments = [arg for call in calls for arg in call]
    simulates = flow in ("vivado_sim", "modelsim")
    sources = design.sim_sources if simulates else design.rtl.sources
    for path in [str(src.file) for src in sources] + files:
        assert path in arguments, f"{path!r} did not reach {flow} whole"


@needs_tclsh
def test_modelsim_keeps_systemverilog_and_lint_as_separate_options(tmp_path, monkeypatch) -> None:
    """A SystemVerilog source must give vlog separate `-sv` and `-lint` arguments."""
    root = tmp_path / "design"
    root.mkdir()
    source = root / "top.sv"
    source.write_text("module top; endmodule\n")
    design = Design(
        name="sv",
        design_root=root,
        rtl={"sources": ["top.sv"], "top": "top"},
        tb={"top": "top"},
    )
    calls = _calls("modelsim", design, {}, tmp_path, monkeypatch)
    assert ["vlog", str(source), "-sv", "-lint"] in calls


@needs_tclsh
def test_dc_sources_each_hook_at_its_stage(tmp_path, monkeypatch) -> None:
    """`hooks` were resolved and then never sourced: every stage's script was silently ignored."""
    design = _design(tmp_path / "design")
    settings, _ = _cases(tmp_path / "design")["dc"]
    hooks = {stage: _file(tmp_path / "design", f"{stage}.tcl") for stage in dc.HOOK_STAGES}
    calls = _calls("dc", design, {**settings, "hooks": hooks}, tmp_path, monkeypatch)

    def position(command: str) -> int:
        return next(i for i, call in enumerate(calls) if call[0] == command)

    def sourced(stage: str) -> int:
        return calls.index(["source", "-echo", hooks[stage]])

    elaborate, link, netlist = position("elaborate"), position("link"), position("write")
    assert sourced("pre_elab") < elaborate < sourced("post_elab") < link < sourced("post_link")
    assert sourced("finalize") > netlist


def test_dc_rejects_a_hook_at_an_unknown_stage() -> None:
    """Dc rejects a hook at an unknown stage."""
    with pytest.raises(FlowSettingsError, match="the stages are pre_elab, post_elab"):
        dc.Dc.Settings.from_input({"hooks": {"post_synth": "h.tcl"}})


@needs_tclsh
@pytest.mark.parametrize("gui", [False, True])
def test_vivado_project_opens_the_gui_only_when_asked(gui, tmp_path, monkeypatch) -> None:
    """The project flow ended in `start_gui`, which fails in every headless Vivado. `gui` runs the
    script in the GUI (`-mode gui`) and leaves the project open there; otherwise it is closed."""
    design = _design(tmp_path / "design")
    settings, _ = _cases(tmp_path / "design")["vivado_project"]
    calls = _calls("vivado_project", design, {**settings, "gui": gui}, tmp_path, monkeypatch)
    assert ["start_gui"] not in calls
    assert (["close_project"] in calls) != gui
    flow = registered_flows["vivado_project"][1](
        {**settings, "gui": gui}, design, tmp_path / "flow"
    )
    flow.init()
    args = flow.vivado.default_args
    assert args[args.index("-mode") + 1] == ("gui" if gui else "batch")


def test_vivado_alt_synth_rejects_tcl_files() -> None:
    """Non-project mode has no fileset for TCL hooks: the setting was accepted and ignored."""
    with pytest.raises(FlowSettingsError, match="`tcl_files` is a setting of vivado_synth"):
        registered_flows["vivado_alt_synth"][1].Settings.from_input(
            {"fpga": "xc7a12tcsg325-1", "tcl_files": ["h.tcl"]}
        )


@pytest.mark.parametrize("engine", ["lse", "synplify"])
@pytest.mark.parametrize("allowed", [True, False])
def test_diamond_allows_or_forbids_dsps_and_brams(allowed, engine, tmp_path, monkeypatch) -> None:
    """The template read `allow_dsps`/`allow_brams`, and `strategy` and `fpga_part`, which the
    flow's settings no longer had: it could not be rendered at all. Each resource is disallowed
    on its own: `allow_brams` also turned off LSE's DSPs."""
    use_fake_tools(monkeypatch)
    settings = {
        "fpga": "LFE5U-25F-6BG256C",
        "clock_period": 10.0,
        "synthesis_engine": engine,
        "allow_dsps": allowed,
        "allow_brams": allowed,
    }
    run_dir = tmp_path / "run"
    try:
        DefaultRunner(run_dir).run_flow(
            registered_flows["diamond_synth"][1], _design(tmp_path / "design"), settings
        )
    except Exception:  # pylint: disable=broad-except
        pass  # no reports without Diamond
    (script,) = run_dir.rglob("synth.tcl")
    text = script.read_text()
    assert "LFE5U-25F-6BG256C" in text
    assert "prj_strgy copy -from $strategy" in text
    bram_line = next((line for line in text.splitlines() if "lse_ebr_util=0" in line), None)
    dsp_line = next((line for line in text.splitlines() if "lse_dsp_util=0" in line), None)
    assert (bram_line is None) == allowed and (dsp_line is None) == allowed
    assert bram_line is None or "dsp" not in bram_line
    if engine == "synplify":
        (fdc,) = run_dir.rglob("constraints.fdc")
        assert ("syn_multstyle" in fdc.read_text()) != allowed
        assert ("syn_ramstyle" in fdc.read_text()) != allowed
