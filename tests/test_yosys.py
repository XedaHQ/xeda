import gzip
import json
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import get_args

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys, YosysFpga
from xeda.flows.yosys.yosys import preproc_libs

from .tool_utils import (
    _command_succeeds,
    _require,
    require_yosys,
    require_yosys_ghdl_plugin,
    yosys_json_attribute_holders,
)

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


def test_yosys_synth_py(tmp_path: Path) -> None:
    require_yosys_ghdl_plugin()
    # settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    # run_dir = "tests_run_dir"
    design_paths = [
        EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.yaml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium-dc.xeda.yaml",
        EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky_vhdl.xeda.yaml",
    ]
    run_dir = tmp_path / "xeda_run"
    for design in design_paths:
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run(YosysFpga, design, flow_overrides=dict(debug=True, verbose=True))
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert flow.succeeded
        assert isinstance(flow.settings, YosysFpga.Settings)
        assert flow.settings.fpga is not None
        if flow.settings.fpga.vendor == "xilinx":
            assert flow.results.LUT > 1
            assert flow.results.FF > 1
        assert results_json.exists()


NANGATE45_LIB = (
    Path(__file__).parent.parent
    / "src/xeda/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib.gz"
)

# Every path-valued Yosys setting, set to a location whose directory -- and in places whose file
# name -- contains a space. Relative outputs land in the run directory.
SPACED_OUTPUTS = {
    "rtl_json": "out dir/rtl.json",
    "rtl_verilog": "out dir/rtl.v",
    "rtl_graph": "out dir/rtl.dot",
    "netlist_verilog": "out dir/net list.v",
    "netlist_json": "out dir/net list.json",
    "netlist_graph": "out dir/netlist.dot",
    "write_blif": "out dir/net list.blif",
}
SPACED_INPUTS = (
    "liberty",
    "dff_liberty",
    "verilog_lib",
    "adder_map",
    "clockgate_map",
    "other_maps",
    "abc_script",
)
# Path settings the sweep does not set: the reports and the flow's other working locations
# (its log, the merged liberty) are names in the run directory, whose path has a space; yosys
# reads no `lib_paths` of its own -- the ghdl plugin's are `ghdl.lib_paths`, which
# `test_yosys_reads_vhdl_from_a_path_with_spaces` sets.
NOT_SWEPT = {
    "reports_dir",
    "outputs_dir",
    "checkpoints_dir",
    "log_file",
    "merge_libs_to",
    "lib_paths",
}


def _write(path: Path, text: str) -> Path:
    """Write a source file for Yosys tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _is_path_field(annotation) -> bool:
    """Identify settings fields containing paths."""
    return annotation is Path or any(_is_path_field(a) for a in get_args(annotation))


def test_the_spaced_path_sweep_covers_every_path_setting():
    """A new path setting has to join the sweep below, so the class stays covered."""
    path_fields = {
        name
        for name, field in Yosys.Settings.model_fields.items()
        if _is_path_field(field.annotation)
    }
    assert path_fields - NOT_SWEPT <= set(SPACED_OUTPUTS) | set(SPACED_INPUTS)


@cache
def _yosys_has_tcl() -> bool:
    """Check whether the installed Yosys supports Tcl."""
    return _command_succeeds(["yosys", "-q", "-c", "/dev/null"])


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
def test_yosys_synthesizes_with_spaces_in_every_path(script_format, tmp_path):
    """yosys splits a `.ys` line at whitespace. Most commands strip the double quotes that group
    a path, so the script quotes those; `read_verilog -I` and `show -prefix` take their argument
    verbatim, quotes included, so no quoting can pass them a path with a space. Emitting
    `-I{dir}` unquoted failed with "File `dir' not found". A TCL script hands yosys each word
    whole, so there quoting is all it takes."""
    require_yosys()
    if script_format == "tcl" and not _yosys_has_tcl():
        # TCL support is optional in yosys builds (oss-cad-suite has none)
        pytest.skip("this yosys has no TCL support")
    root = tmp_path / "my design"
    _write(root / "inc dir" / "defs.vh", "`define W 4\n")
    _write(
        root / "rtl dir" / "top.v",
        '`include "defs.vh"\n'
        "module top(input clk, input [`W-1:0] a, b, output reg [`W:0] q, output y);\n"
        "  always @(posedge clk) q <= a + b;\n"
        "  bb u_bb(.a(a[0]), .y(y));\n"
        "endmodule\n",
    )
    lib = root / "lib dir" / "cells lib.lib"
    lib.parent.mkdir(parents=True)
    with gzip.open(NANGATE45_LIB, "rb") as src, open(lib, "wb") as dst:
        shutil.copyfileobj(src, dst)
    black_box = _write(
        root / "lib dir" / "black box.v", "module bb(input a, output y); endmodule\n"
    )
    maps = root / "map dir"
    settings = {
        "liberty": [str(lib)],
        "dff_liberty": str(lib),
        "verilog_lib": [str(black_box)],
        "adder_map": str(_write(maps / "adder map.v", "module _unused_fa(); endmodule\n")),
        "clockgate_map": str(_write(maps / "clock gate.v", "module cg(); endmodule\n")),
        "other_maps": [str(_write(maps / "other map.v", "module _unused(); endmodule\n"))],
        # ABC runs the script with its own `source`, which splits a path at spaces whatever the
        # quoting: the one input that must live at a path without one
        "abc_script": str(_write(tmp_path / "abc" / "map.abc", "strash\nmap\n")),
        **SPACED_OUTPUTS,
        "script_format": script_format,
    }
    assert set(SPACED_INPUTS) <= set(settings)
    design = Design(
        name="spaced",
        design_root=root,
        rtl={"sources": ["inc dir/defs.vh", "rtl dir/top.v"], "top": "top"},
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, settings)
    assert flow is not None and flow.succeeded
    for output in SPACED_OUTPUTS.values():
        assert (flow.run_path / output).is_file(), output
    assert "DFF_X" in (flow.run_path / SPACED_OUTPUTS["netlist_verilog"]).read_text()


SRC_ATTRIBUTE_DESIGN = """\
module pipe(input clk, input [3:0] d, output reg [3:0] q);
  reg [3:0] mem [0:3];
  reg [1:0] addr = 0;
  always @(posedge clk) begin
    mem[addr] <= d;
    addr <= addr + 1;
    q <= mem[addr] ^ d;
  end
endmodule
module top(input clk, input [3:0] a, output [3:0] y);
  pipe u_pipe(.clk(clk), .d(a), .q(y));
endmodule
"""


def _synthesize_src_design(flow_cls, script_format, tmp_path, **settings):
    """Synthesize a small design -- a submodule, registers, a memory -- with real yosys."""
    require_yosys()
    if script_format == "tcl" and not _yosys_has_tcl():
        pytest.skip("this yosys has no TCL support")
    root = tmp_path / "src-attributes"
    _write(root / "top.v", SRC_ATTRIBUTE_DESIGN)
    design = Design(
        name="src-attributes", design_root=root, rtl={"sources": ["top.v"], "top": "top"}
    )
    flow = DefaultRunner(tmp_path / "run").run_flow(
        flow_cls, design, {"script_format": script_format, **settings}
    )
    assert flow is not None and flow.succeeded
    return flow


#: Settings under which `src` must reach no netlist: the default, which writes the JSON *and* the
#: Verilog netlist; JSON alone; and `netlist_attrs = false`, which only drops the Verilog
#: netlist's attributes.
STRIPPING = {
    "default": {},
    "json-only": {"netlist_verilog": None},
    "verilog-noattr": {"netlist_attrs": False},
}


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
@pytest.mark.parametrize("settings", list(STRIPPING.values()), ids=list(STRIPPING))
def test_yosys_writes_no_src_attribute_into_any_netlist(settings, script_format, tmp_path):
    """The JSON netlist is what nextpnr reads: `netlist_src_attrs = false` (the default) has to
    strip `src` before it is written, from every module, cell, memory and wire -- not only
    ahead of the Verilog netlist, and whatever `netlist_attrs` says."""
    flow = _synthesize_src_design(Yosys, script_format, tmp_path, **settings)
    assert yosys_json_attribute_holders(flow.run_path / "netlist.json", "src") == []
    if flow.settings.netlist_verilog:
        assert "src =" not in (flow.run_path / flow.settings.netlist_verilog).read_text()


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
def test_yosys_keeps_src_attributes_when_asked(script_format, tmp_path):
    flow = _synthesize_src_design(Yosys, script_format, tmp_path, netlist_src_attrs=True)
    holders = yosys_json_attribute_holders(flow.run_path / "netlist.json", "src")
    assert "modules/top" in holders and "modules/pipe" in holders
    assert "src =" in (flow.run_path / "netlist.v").read_text()


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
@pytest.mark.parametrize("keep_src", [False, True], ids=["strip-src", "keep-src"])
def test_yosys_fpga_json_netlist_follows_netlist_src_attrs(keep_src, script_format, tmp_path):
    """FPGA synthesis writes the target's library cells into the JSON netlist as boxes, each
    with the `src` of its yosys simulation model; a selection skips boxes unless it starts with
    `=`. Every one of them has to lose `src` as well."""
    flow = _synthesize_src_design(
        YosysFpga,
        script_format,
        tmp_path,
        fpga="iCE40HX1K-TQ144",
        flatten=False,
        netlist_src_attrs=keep_src,
    )
    netlist = flow.run_path / "netlist.json"
    boxes = yosys_json_attribute_holders(netlist, "blackbox")
    assert any(box.startswith("modules/SB_") for box in boxes), "no library box was written"
    holders = yosys_json_attribute_holders(netlist, "src")
    if keep_src:
        assert "modules/top" in holders
    else:
        assert holders == []


#: a bus on each port, and a module under the top that flattening absorbs
EDIF_DESIGN = """module core(input clk, input [3:0] d, output reg [3:0] q);
  always @(posedge clk) q <= d + 1;
endmodule
module top(input clk, input [3:0] sw, output [3:0] led);
  core u_core(.clk(clk), .d(sw), .q(led));
endmodule
"""


def _synthesize_for_vivado(tmp_path, **settings):
    """Synthesize `EDIF_DESIGN` for a Xilinx device with real yosys."""
    require_yosys()
    root = tmp_path / "edif"
    _write(root / "top.v", EDIF_DESIGN)
    design = Design(name="edif", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    flow = DefaultRunner(tmp_path / "run").run_flow(
        YosysFpga, design, {"fpga": {"part": "xc7a35tcpg236-1"}, **settings}
    )
    assert flow is not None and flow.succeeded
    return flow


def _modules_of_the_design(edif: str) -> int:
    """The cells the netlist defines in its own library, `DESIGN`: one for a flat design. The
    library cells it only declares, in `LIB`, come before it."""
    return edif[edif.index("(library DESIGN") : edif.index("\n  (design ")].count("\n    (cell ")


def test_yosys_fpga_writes_the_flat_edif_netlist_vivado_reads(tmp_path):
    """The netlist is one cell, flat, and every bus keeps its range: `led` is `led[3:0]`, which
    without `-pvector bra` is written `led` and read back by Vivado with its bits reversed."""
    flow = _synthesize_for_vivado(tmp_path)
    edif = flow.run_path / "netlist.edif"
    text = edif.read_text()
    assert flow.results["outputs"]["netlist_edif"]["path"] == str(edif)
    assert text.startswith("(edif top\n") and "(design top\n" in text
    assert _modules_of_the_design(text) == 1, "a hierarchical netlist is no one design to Vivado"
    assert '(rename sw "sw[3:0]")' in text and '(rename led "led[3:0]")' in text
    assert "u_core" not in text.split("(library DESIGN")[1]  # flattened into the top


def test_yosys_fpga_writes_no_edif_netlist_for_a_hierarchical_synthesis(tmp_path):
    flow = _synthesize_for_vivado(tmp_path, flatten=False)
    assert not (flow.run_path / "netlist.edif").exists()
    assert "netlist_edif" not in flow.results["outputs"] and "netlist" in flow.results["outputs"]


def test_a_hierarchical_yosys_netlist_would_define_two_cells(tmp_path):
    """Why a flat netlist is the only one: yosys writes each module of the design as a cell of
    the library `DESIGN`, and Vivado resolves the instance of one as an undefined black box."""
    require_yosys()
    _write(tmp_path / "top.v", EDIF_DESIGN)
    run = subprocess.run(
        [
            "yosys",
            "-q",
            "-p",
            "read_verilog top.v; synth_xilinx -top top; write_edif -pvector bra x.edif",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert run.returncode == 0, run.stderr
    assert _modules_of_the_design((tmp_path / "x.edif").read_text()) == 2


@pytest.mark.parametrize("flags", [[], ["-sv"], ["-noautowire"], ["-noautowire", "-sv"]])
def test_yosys_fpga_default_nettype_none_frontend_matrix(flags, tmp_path):
    """The legal deferred output reference works unless noautowire is explicitly requested."""
    require_yosys()
    source = TESTS_DIR / "resources" / "yosys" / "ps7.v"
    design = Design(
        name="deferred-primitive",
        design_root=source.parent,
        rtl={"sources": [source.name], "top": "ps7_axi_blinky"},
    )
    flow = DefaultRunner(tmp_path / "run").run_flow(
        YosysFpga,
        design,
        {"fpga": {"part": "xc7a35tcpg236-1"}, "read_verilog_flags": flags},
    )
    assert flow is not None
    assert flow.succeeded is ("-noautowire" not in flags)


def test_yosys_fpga_default_nettype_none_rejects_undeclared_wire(tmp_path):
    require_yosys()
    root = tmp_path / "undeclared-wire"
    _write(
        root / "top.v",
        "`default_nettype none\n"
        "module top(input I, output O); assign O = genuinely_undeclared; endmodule\n",
    )
    design = Design(
        name="undeclared-wire", design_root=root, rtl={"sources": ["top.v"], "top": "top"}
    )
    flow = DefaultRunner(tmp_path / "run").run_flow(
        YosysFpga, design, {"fpga": {"part": "xc7a35tcpg236-1"}}
    )
    assert flow is not None and not flow.succeeded


def test_yosys_fpga_xilinx_primitive_frontends(tmp_path):
    """All device library modules are known at hierarchy check, including the PS7 model."""
    require_yosys()
    root = tmp_path / "xilinx-primitives"
    _write(
        root / "top.v",
        "module top;\n"
        "  BUFG bufg();\n"
        "  IBUFDS ibufds();\n"
        "  PLLE2_ADV pll();\n"
        "  MMCME2_ADV mmcm();\n"
        "  PS7 ps7();\n"
        "endmodule\n",
    )
    design = Design(
        name="xilinx-primitives", design_root=root, rtl={"sources": ["top.v"], "top": "top"}
    )
    flow = DefaultRunner(tmp_path / "run").run_flow(
        YosysFpga, design, {"fpga": {"part": "xc7a35tcpg236-1"}}
    )
    assert flow is not None and flow.succeeded


@pytest.mark.parametrize("family", ["GW1N-9", "GW2A-18", "GW5A-25"])
def test_yosys_fpga_gowin_primitives_are_known_at_hierarchy_check(family, tmp_path):
    """A design instantiating a Gowin primitive reaches the pass: the device library is read
    before `hierarchy -check`, as `synth_gowin` itself reads it."""
    require_yosys()
    root = tmp_path / "gowin-primitives"
    _write(
        root / "top.v",
        "module top(input clk, input d, output q);\n"
        "  DFF ff(.D(d), .CLK(clk), .Q(q));\n"
        "endmodule\n",
    )
    design = Design(
        name="gowin-primitives", design_root=root, rtl={"sources": ["top.v"], "top": "top"}
    )
    flow = DefaultRunner(tmp_path / "run").run_flow(
        YosysFpga, design, {"fpga": {"vendor": "gowin", "family": "gowin", "device": family}}
    )
    assert flow is not None and flow.succeeded


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
@pytest.mark.parametrize("library", ["+/gowin/cells_sim.v", "+/xilinx/cells_sim.v"])
def test_yosys_writes_its_json_with_whitebox_library_cells_in_the_design(
    library, script_format, tmp_path
):
    """`verilog_lib` is read with `-lib`, which keeps the models yosys marks `lib_whitebox` as
    whiteboxes with their `always` blocks: `write_json` fails on those (`ERROR: Module ALU
    contains processes`). The RTL outputs describe the design's own modules; the netlist, written
    after synthesis, holds the library cells as blackboxes, as the FPGA passes leave them."""
    require_yosys()
    if script_format == "tcl" and not _yosys_has_tcl():
        pytest.skip("this yosys has no TCL support")
    root = tmp_path / "design"
    _write(
        root / "top.v",
        "module leaf(input a, output y); assign y = ~a; endmodule\n"
        "module top(input clk, input a, output reg q);\n"
        "  wire n;\n  leaf u(.a(a), .y(n));\n  always @(posedge clk) q <= n;\nendmodule\n",
    )
    design = Design(name="top", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    settings = {
        "script_format": script_format,
        "verilog_lib": [library],
        "flatten": False,
        "rtl_json": "rtl.json",
        "rtl_verilog": "rtl.v",
        "rtl_graph": "rtl.dot",
        "netlist_json": "netlist.json",
        "netlist_verilog": "netlist.v",
    }
    flow = DefaultRunner(tmp_path / "run").run_flow(Yosys, design, settings)
    assert flow is not None and flow.succeeded
    run = Path(flow.run_path)
    assert set(json.loads((run / "rtl.json").read_text())["modules"]) == {"leaf", "top"}
    rtl = (run / "rtl.v").read_text()
    assert set(re.findall(r"^module\s+(\S+?)\s*\(", rtl, re.MULTILINE)) == {"leaf", "top"}
    dot = (run / "rtl.dot").read_text()
    assert set(re.findall(r'^digraph "([^"]+)"', dot, re.MULTILINE)) == {"leaf", "top"}
    netlist = json.loads((run / "netlist.json").read_text())["modules"]
    assert "top" in netlist
    assert not [
        name
        for name, module in netlist.items()
        if "whitebox" in module.get("attributes", {}) and "blackbox" not in module["attributes"]
    ]


def test_yosys_fpga_xilinx_library_does_not_hide_unknown_modules(tmp_path):
    require_yosys()
    root = tmp_path / "unknown-xilinx-primitive"
    _write(root / "top.v", "module top; XEDA_UNKNOWN_PRIMITIVE missing(); endmodule\n")
    design = Design(
        name="unknown-xilinx-primitive", design_root=root, rtl={"sources": ["top.v"], "top": "top"}
    )
    flow = DefaultRunner(tmp_path / "run").run_flow(
        YosysFpga, design, {"fpga": {"part": "xc7a35tcpg236-1"}}
    )
    assert flow is not None and not flow.succeeded


def test_yosys_fpga_default_flags_drop_noautowire_only_for_fpga():
    assert YosysFpga.Settings().read_verilog_flags == ["-sv"]
    assert Yosys.Settings().read_verilog_flags == ["-noautowire", "-sv"]


def test_yosys_fpga_does_not_register_disabled_timing_report(tmp_path):
    flow = _synthesize_src_design(YosysFpga, "ys", tmp_path, fpga="iCE40HX1K-TQ144")
    assert flow.settings.sta is False
    assert flow.artifacts.timing_report is None


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
def test_yosys_reads_every_input_by_its_own_name(script_format, tmp_path):
    """yosys' own frontends -- `read_verilog`, `read_liberty`, `techmap -map` -- expand glob
    patterns in a file name, and fall back to the name itself only when nothing matches: handed
    `top[1].v`, they read `top1.v` whenever one existed. So every input here has brackets in its
    name, beside a decoy of garbage named as the pattern matches. The commands that take a name
    as it is (`dfflibmap`, `abc` and `stat` with `-liberty`) must get it unescaped, or they find
    no file at all."""
    require_yosys()
    if script_format == "tcl" and not _yosys_has_tcl():
        pytest.skip("this yosys has no TCL support")
    root = tmp_path / "d"

    def with_decoy(path: Path, text: str) -> Path:
        _write(path.with_name(path.name.replace("[1]", "1")), "garbage, no yosys input\n")
        return _write(path, text)

    lib = with_decoy(root / "lib" / "cells[1].lib", "")
    with gzip.open(NANGATE45_LIB, "rb") as src, open(lib, "wb") as dst:
        shutil.copyfileobj(src, dst)
    bb = "module bb(input a, output y); endmodule\n"
    settings = {
        "liberty": [str(lib)],
        "dff_liberty": str(lib),
        "verilog_lib": [str(with_decoy(root / "lib" / "bb[1].v", bb))],
        "adder_map": str(with_decoy(root / "map" / "fa[1].v", "module _unused_fa(); endmodule\n")),
        "clockgate_map": str(with_decoy(root / "map" / "cg[1].v", "module cg(); endmodule\n")),
        "other_maps": [str(with_decoy(root / "map" / "o[1].v", "module _unused(); endmodule\n"))],
        "abc_script": str(with_decoy(root / "map" / "abc[1].abc", "strash\nmap\n")),
        "netlist_verilog": "net[1].v",
        "script_format": script_format,
    }
    assert set(SPACED_INPUTS) <= set(settings)
    with_decoy(
        root / "rtl" / "top[1].v",
        "module top(input clk, input [3:0] a, b, output reg [4:0] q, output y);\n"
        "  always @(posedge clk) q <= a + b;\n"
        "  bb u_bb(.a(a[0]), .y(y));\n"
        "endmodule\n",
    )
    design = Design(name="d", design_root=root, rtl={"sources": ["rtl/top[1].v"], "top": "top"})
    flow = DefaultRunner(tmp_path / "run").run_flow(Yosys, design, settings)
    assert flow is not None and flow.succeeded
    assert "DFF_X" in (flow.run_path / "net[1].v").read_text()


def test_yosys_reads_vhdl_from_a_path_with_spaces(tmp_path):
    """The ghdl plugin takes its arguments verbatim, like `read_verilog -I`: the source files,
    and the directory of each `-P<dir>` library path."""
    require_yosys_ghdl_plugin()
    root = tmp_path / "my design"
    _write(
        root / "vhdl dir" / "inv.vhd",
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity inv is port(a: in std_logic; y: out std_logic); end;\n"
        "architecture rtl of inv is begin y <= not a; end;\n",
    )
    (root / "vhdl libs").mkdir()
    design = Design(
        name="inv", design_root=root, rtl={"sources": ["vhdl dir/inv.vhd"], "top": "inv"}
    )
    settings = {"ghdl": {"lib_paths": [(None, str(root / "vhdl libs"))]}}
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, settings)
    assert flow is not None and flow.succeeded


@cache
def _probe_yosys_slang() -> bool:
    """Check whether the installed Yosys has slang support."""
    return _command_succeeds(["yosys", "-p", "plugin -i slang"])


def test_yosys_reads_systemverilog_from_a_path_with_spaces(tmp_path):
    """So does the slang frontend, for sources and `-I` alike; and an `.svh` header's directory
    is an include directory just as a `.vh` header's is."""
    require_yosys()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    root = tmp_path / "my design"
    _write(root / "inc dir" / "defs.svh", "`define W 4\n")
    _write(
        root / "sv dir" / "top.sv",
        '`include "defs.svh"\n'
        "module top(input logic [`W-1:0] a, output logic [`W-1:0] y);\n"
        "  assign y = ~a;\n"
        "endmodule\n",
    )
    design = Design(
        name="svtop",
        design_root=root,
        rtl={"sources": ["inc dir/defs.svh", "sv dir/top.sv"], "top": "top"},
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded


def test_yosys_passes_vhdl_top_generics_to_ghdl_and_leaves_the_design_alone(tmp_path):
    """A VHDL top's generics reach yosys through GHDL (`-g<name>=<value>`), after which the
    elaborated top has no parameters left to `chparam`. `init` used to express the second half
    by emptying `design.rtl.parameters` -- the design every flow of the run shares, after its
    hash was taken: `settings.json` then recorded an `rtl_fingerprint`/`rtl_hash` that did not
    match the run's `design_hash`, and GHDL, reading the same emptied parameters, never
    received the generics at all."""
    require_yosys_ghdl_plugin()
    _write(
        tmp_path / "inv.vhd",
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity inv is generic(W: positive := 2);\n"
        "  port(a: in std_logic_vector(W-1 downto 0); y: out std_logic_vector(W-1 downto 0));\n"
        "end;\n"
        "architecture rtl of inv is begin y <= not a; end;\n",
    )
    design = Design(
        name="inv",
        design_root=tmp_path,
        rtl={"sources": ["inv.vhd"], "top": "inv", "parameters": {"W": 5}},
    )
    before = (design.model_dump(mode="json"), design.rtl_fingerprint, design.rtl_hash)
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded
    assert (design.model_dump(mode="json"), design.rtl_fingerprint, design.rtl_hash) == before
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["rtl_hash"] == design.rtl_hash
    assert recorded["rtl_fingerprint"]["parameters"] == {"W": 5}
    netlist = json.loads((flow.run_path / "netlist.json").read_text())
    assert len(netlist["modules"]["inv"]["ports"]["y"]["bits"]) == 5


def _liberty(cell: str) -> str:
    """Return Liberty source text for a cell used in Yosys tests."""
    return (
        "library (cells) {\n"
        '  time_unit : "1ns" ;\n'
        f"  cell ({cell}) {{\n"
        "    area : 1 ;\n"
        "  }\n"
        "}\n"
    )


def test_preprocessed_libraries_sharing_a_stem_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """Each library is pre-processed into its own file named after its stem, so `a/cells.lib`
    and `b/cells.lib` both became `cells-mod.lib`, the second replacing the first. Each file
    also received every library processed before it, which is what hid that: the survivor
    happened to hold both."""
    libs = [_write(tmp_path / d / "cells.lib", _liberty(f"{d.upper()}_X1")) for d in ("a", "b")]
    monkeypatch.chdir(tmp_path)
    preproc_libs(libs, tmp_path / "merged.lib", ["B_X1"], use_temp_folder=False)
    processed = sorted((tmp_path / "processed_libs").iterdir())
    assert len(processed) == 2
    cells = [set(re.findall(r"cell\s*\((\w+)\)", p.read_text())) for p in processed]
    assert sorted(cells, key=sorted) == [{"A_X1"}, {"B_X1"}]
    merged = (tmp_path / "merged.lib").read_text()
    assert set(re.findall(r"cell\s*\((\w+)\)", merged)) == {"A_X1", "B_X1"}
    assert re.search(r"cell \(B_X1\) \{\n\s*dont_use : true;", merged)


if __name__ == "__main__":
    test_yosys_synth_py()


def test_yosys_fpga_hands_ghdl_each_vhdl_file_once(tmp_path):
    """`read_files.ys` lists the VHDL files and `-e <top>` itself; `yosys_fpga` also asked
    `GhdlSynth.synth_args` for a one-shot elaboration, which carries every file again, so GHDL
    analyzed each twice (`ghdl ... sqrt.vhdl sqrt.vhdl -e sqrt`)."""
    require_yosys_ghdl_plugin()
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    flow = DefaultRunner(tmp_path).run_flow(YosysFpga, design, {"fpga": "LFE5U-25F-6BG381C"})
    assert flow is not None and flow.succeeded
    ghdl_line = next(
        line
        for line in (flow.run_path / "yosys_fpga_synth.ys").read_text().splitlines()
        if line.startswith("ghdl ")
    )
    assert ghdl_line.count("sqrt.vhdl") == 1, ghdl_line


def test_yosys_finds_a_header_listed_after_the_verilog_source(tmp_path):
    """Explicit headers supply include directories regardless of source-list position."""
    require_yosys()
    _write(tmp_path / "include" / "defs.vh", "`define WIDTH 4\n")
    _write(
        tmp_path / "rtl" / "top.v",
        '`include "defs.vh"\nmodule top(input [`WIDTH-1:0] a, output [`WIDTH-1:0] y); '
        "assign y = a; endmodule\n",
    )
    design = Design(
        name="headers",
        design_root=tmp_path,
        rtl={"sources": ["rtl/top.v", "include/defs.vh"], "top": "top"},
    )
    flow = DefaultRunner(tmp_path / "runs").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded
