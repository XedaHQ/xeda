import ast
import inspect
import json
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import get_args

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow.settings_group import SettingsGroup
from xeda.flow_runner import DefaultRunner
from xeda.edif import modules_used_as_library_cells
from xeda.flows import Yosys, YosysFpga
from xeda.flows.yosys.common import YosysBase, YosysSynthesisBase

from .tiny_platform import TINY, TINY_RESIDUE, tiny_store
from .tool_utils import (
    _command_succeeds,
    _require,
    require_yosys,
    require_yosys_ghdl_plugin,
    require_yosys_tcl,
    yosys_json_attribute_holders,
)
from .tool_utils import HANG_GUARD

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
            assert flow.results.lut > 1
            assert flow.results.ff > 1
        assert results_json.exists()


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
    "sequential.dff_liberty",
    "verilog_lib",
    "arithmetic.adder_map_file",
    "sequential.clockgate_map_file",
    "sequential.latch_map_file",
    "techmap_files",
    "memory.libmap",
    "memory.techmap",
    "abc_script",
    "platform_file",
    "pdk_root",
)
# Path settings the sweep does not set: the reports and the flow's other working locations
# (its log) are names in the run directory, whose path has a space; yosys reads no `lib_paths` of
# its own -- the ghdl plugin's are `ghdl.lib_paths`, which
# `test_yosys_reads_vhdl_from_a_path_with_spaces` sets.
NOT_SWEPT = {
    # refused together with `sequential.dff_liberty`, which the sweep sets;
    # `tests/test_yosys_asic_sequential.py` reads it from a path with a space
    "sequential.dff_map",
    "reports_dir",
    "outputs_dir",
    "checkpoints_dir",
    "log_file",
    "lib_paths",
}


#: A memory library that yosys reads and that no design memory matches.
MEMORY_LIBRARY = (
    "ram distributed $__UNUSED_RAM {\n  abits 4;\n  width 8;\n  cost 3;\n"
    '  port arsw "RW" {\n    clock posedge;\n  }\n}\n'
)


def _write(path: Path, text: str) -> Path:
    """Write a source file for Yosys tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _is_path_field(annotation) -> bool:
    """Identify settings fields containing paths."""
    return annotation is Path or any(_is_path_field(a) for a in get_args(annotation))


def _path_settings() -> set[str]:
    """The names of the path settings of `Yosys`, a leaf of a group by its dotted name."""
    names = set()
    for name, field in Yosys.Settings.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, SettingsGroup):
            names |= {
                f"{name}.{leaf}"
                for leaf, info in annotation.model_fields.items()
                if _is_path_field(info.annotation)
            }
        elif _is_path_field(annotation):
            names.add(name)
    return names


def _nested(settings: dict) -> dict:
    """Return `settings` with the dotted keys written as nested tables."""
    nested: dict = {}
    for key, value in settings.items():
        table = nested
        *groups, leaf = key.split(".")
        for group in groups:
            table = table.setdefault(group, {})
        table[leaf] = value
    return nested


def test_the_spaced_path_sweep_covers_every_path_setting():
    """A new path setting has to join the sweep below, so the class stays covered."""
    assert _path_settings() - NOT_SWEPT <= set(SPACED_OUTPUTS) | set(SPACED_INPUTS)


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
    shutil.copyfile(TINY / "tiny.lib", lib)
    store = tiny_store(root, "pdk dir")
    platform_file = shutil.copy(TINY_RESIDUE, store / "my platform.yaml")
    black_box = _write(
        root / "lib dir" / "black box.v", "module bb(input a, output y); endmodule\n"
    )
    maps = root / "map dir"
    settings = {
        "liberty": [str(lib)],
        "sequential.dff_liberty": str(lib),
        "platform_file": str(platform_file),
        "pdk_root": str(store),
        "verilog_lib": [str(black_box)],
        "arithmetic.adder_map_file": str(
            _write(maps / "adder map.v", "module _unused_fa(); endmodule\n")
        ),
        "sequential.clockgate_map_file": str(
            _write(maps / "clock gate.v", "module cg(); endmodule\n")
        ),
        "sequential.latch_map_file": str(
            _write(maps / "latch map.v", "module _unused(); endmodule\n")
        ),
        "techmap_files": [str(_write(maps / "other map.v", "module _unused2(); endmodule\n"))],
        "memory.libmap": [str(_write(maps / "mem lib.txt", MEMORY_LIBRARY))],
        "memory.techmap": str(_write(maps / "mem map.v", "module _unused3(); endmodule\n")),
        # ABC runs the script with its own `source`, which splits a path at spaces whatever the
        # quoting: the one input that must live at a path without one
        "abc_script": str(_write(tmp_path / "abc" / "map.abc", "strash\nmap\n")),
        **SPACED_OUTPUTS,
    }
    assert set(SPACED_INPUTS) <= set(settings)
    settings = _nested(settings)
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
    flow = DefaultRunner(tmp_path / "run").run_flow(flow_cls, design, settings)
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


@pytest.mark.parametrize("script_format", ["ys"])  # `yosys_fpga` writes no Tcl script
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


def _synthesize_for_vivado(tmp_path, source=EDIF_DESIGN, **settings):
    """Synthesize a design of `top` over `core` (`EDIF_DESIGN`) for a Xilinx device with real
    yosys."""
    require_yosys()
    root = tmp_path / "edif"
    _write(root / "top.v", source)
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


#: `core` with the attribute in the HDL: the hierarchy no setting shows
EDIF_DESIGN_KEEPING_CORE = EDIF_DESIGN.replace(
    "module core", "(* keep_hierarchy *)\nmodule core", 1
)


@pytest.mark.parametrize(
    "settings, source, shown_by_the_settings",
    [
        pytest.param({"keep_hierarchy": ["core"]}, EDIF_DESIGN, True, id="keep_hierarchy"),
        pytest.param(
            {"set_mod_attribute": {"keep_hierarchy": {"core": 1}}},
            EDIF_DESIGN,
            True,
            id="set_mod_attribute",
        ),
        pytest.param(  # a bare name selects a module in yosys: the instance is `c:u_core`
            {"set_attribute": {"keep_hierarchy": {"c:u_core": 1}}},
            EDIF_DESIGN,
            True,
            id="set_attribute",
        ),
        pytest.param(
            {"set_attribute": {"keep_hierarchy": 1}}, EDIF_DESIGN, True, id="set_attribute_all"
        ),
        pytest.param({"black_box": ["core"]}, EDIF_DESIGN, True, id="black_box"),
        pytest.param({}, EDIF_DESIGN_KEEPING_CORE, False, id="hdl_attribute"),
    ],
)
def test_every_way_to_keep_core_out_of_the_flat_netlist_is_found(
    tmp_path, settings, source, shown_by_the_settings
):
    """The synthesis itself succeeds with a hierarchy (`yosys_fpga+nextpnr` takes the JSON netlist
    of one), and Vivado cannot read one. A route its settings show writes no EDIF netlist, so a
    consumer is refused while planning. The one only the HDL shows, an attribute, is found in the
    EDIF netlist that is written. The flat design is the control: no route, no finding."""
    flow = _synthesize_for_vivado(tmp_path, source, **settings)
    modules = json.loads((flow.run_path / "netlist.json").read_text())["modules"]
    assert "core" in modules, "the route did not keep core out of the flat netlist"
    edif = flow.run_path / "netlist.edif"
    assert flow.settings.edif_problem() is not None if shown_by_the_settings else True
    if shown_by_the_settings:
        assert not edif.exists() and "netlist_edif" not in flow.results["outputs"]
    else:
        assert flow.settings.edif_problem() is None
        assert modules_used_as_library_cells(edif.read_text()) == ["core"]


def test_a_flat_edif_netlist_uses_no_module_as_a_library_cell(tmp_path):
    flow = _synthesize_for_vivado(tmp_path)
    assert "core" not in json.loads((flow.run_path / "netlist.json").read_text())["modules"]
    assert modules_used_as_library_cells((flow.run_path / "netlist.edif").read_text()) == []


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
        timeout=HANG_GUARD,
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
    shutil.copyfile(TINY / "tiny.lib", lib)
    store = tiny_store(root, "pdk[1]")
    platform_file = shutil.copy(TINY_RESIDUE, store / "tiny[1].yaml")
    bb = "module bb(input a, output y); endmodule\n"
    settings = {
        "liberty": [str(lib)],
        "sequential.dff_liberty": str(lib),
        "platform_file": str(platform_file),
        "pdk_root": str(store),
        "verilog_lib": [str(with_decoy(root / "lib" / "bb[1].v", bb))],
        "arithmetic.adder_map_file": str(
            with_decoy(root / "map" / "fa[1].v", "module _unused_fa(); endmodule\n")
        ),
        "sequential.clockgate_map_file": str(
            with_decoy(root / "map" / "cg[1].v", "module cg(); endmodule\n")
        ),
        "sequential.latch_map_file": str(
            with_decoy(root / "map" / "latch[1].v", "module _unused(); endmodule\n")
        ),
        "techmap_files": [
            str(with_decoy(root / "map" / "o[1].v", "module _unused2(); endmodule\n"))
        ],
        "memory.libmap": [str(with_decoy(root / "map" / "mem[1].txt", MEMORY_LIBRARY))],
        "memory.techmap": str(
            with_decoy(root / "map" / "mem[1].v", "module _unused3(); endmodule\n")
        ),
        "abc_script": str(with_decoy(root / "map" / "abc[1].abc", "strash\nmap\n")),
        "netlist_verilog": "net[1].v",
    }
    assert set(SPACED_INPUTS) <= set(settings)
    settings = _nested(settings)
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


def test_yosys_elaborates_a_package_that_another_systemverilog_file_declares(tmp_path):
    """slang elaborates the sources of one `read_slang` as one compilation. A design whose
    package is in a file of its own, as most multi-file SystemVerilog designs are, elaborates
    only when every SystemVerilog source goes to one `read_slang`."""
    require_yosys()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    _write(tmp_path / "pkg.sv", "package p;\n  localparam int W = 4;\nendpackage\n")
    _write(
        tmp_path / "top.sv",
        "module top import p::*; (input logic [W-1:0] a, output logic [W-1:0] y);\n"
        "  inv u_inv(.a(a), .y(y));\n"
        "endmodule\n",
    )
    _write(
        tmp_path / "inv.sv",
        "module inv import p::*; (input logic [W-1:0] a, output logic [W-1:0] y);\n"
        "  assign y = ~a;\n"
        "endmodule\n",
    )
    design = Design(
        name="pkgtop",
        design_root=tmp_path,
        rtl={"sources": ["pkg.sv", "inv.sv", "top.sv"], "top": "top"},
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded


def test_yosys_reads_verilog_with_the_systemverilog_of_a_design_in_one_read_slang(tmp_path):
    """A design with a SystemVerilog source gives its Verilog sources to the same `read_slang`,
    as OpenROAD-flow-scripts does. The Verilog modules and the SystemVerilog modules then see one
    another in one compilation, and the include directories reach both."""
    require_yosys()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    _write(tmp_path / "inc" / "defs.vh", "`define W 4\n")
    _write(tmp_path / "pkg.sv", "package p;\n  localparam int W = 4;\nendpackage\n")
    _write(
        tmp_path / "gate.v",
        '`include "defs.vh"\n'
        "module gate(input [`W-1:0] a, output [`W-1:0] y);\n"
        "  assign y = ~a;\n"
        "endmodule\n",
    )
    _write(
        tmp_path / "top.sv",
        "module top import p::*; (input logic [W-1:0] a, output logic [W-1:0] y);\n"
        "  gate u_gate(.a(a), .y(y));\n"
        "endmodule\n",
    )
    design = Design(
        name="mixed",
        design_root=tmp_path,
        rtl={"sources": ["inc/defs.vh", "gate.v", "pkg.sv", "top.sv"], "top": "top"},
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded
    log = (flow.run_path / "yosys.log").read_text()
    assert len(re.findall(r"^\d+\. Executing SLANG frontend", log, re.MULTILINE)) == 1
    assert not [
        line for line in log.splitlines() if "Verilog-2005 frontend" in line and "gate.v" in line
    ]
    modules = json.loads((flow.run_path / "netlist.json").read_text())["modules"]
    assert "top" in modules


PARAMETERIZED_TOP = (
    "module top #(parameter W = 4, parameter [6:0] BV = 7'd1, parameter F = 1'b0,\n"
    '             parameter S = "zzzz", parameter [7:0] N = 0, parameter real R = 1.0)\n'
    "  (output [W-1:0] y, output [6:0] bv_o, output f_o, output [7:0] n_o,\n"
    "   output [$bits(S)-1:0] s_o, output [7:0] r_o);\n"
    "  assign y = {W{1'b1}};\n"
    "  assign bv_o = BV;\n"
    "  assign f_o = F;\n"
    "  assign n_o = N;\n"
    "  assign s_o = S;\n"
    "  assign r_o = $rtoi(R * 2.0);\n"
    "endmodule\n"
)


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
@pytest.mark.parametrize("top_file", ["top.sv", "top.v"])
def test_yosys_sets_every_type_of_parameter_through_read_slang(tmp_path, script_format, top_file):
    """slang elaborates a module while it reads it, so `chparam` cannot change the parameters of
    the top afterwards. The script gives them to `read_slang -G`, whatever the language of the
    top: an integer, a sized literal, a boolean, a string and a real number. A Verilog top is
    read by `read_slang` when the design has a SystemVerilog source too."""
    require_yosys_tcl() if script_format == "tcl" else require_yosys()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    _write(tmp_path / top_file, PARAMETERIZED_TOP)
    _write(tmp_path / "other.sv", "module other(input logic a);\nendmodule\n")
    sources = [top_file] if top_file == "top.sv" else [top_file, "other.sv"]
    design = Design(
        name="params",
        design_root=tmp_path,
        rtl={
            "sources": sources,
            "top": "top",
            "parameters": {
                "W": 3,
                "BV": "7'b0101001",
                "F": True,
                "N": 5,
                "S": "abcd",
                "R": 1.5,
            },
        },
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded
    assert "chparam" not in (flow.run_path / f"yosys_synth.{script_format}").read_text()
    ports = json.loads((flow.run_path / "netlist.json").read_text())["modules"]["top"]["ports"]
    bits = {name: "".join(str(b) for b in reversed(port["bits"])) for name, port in ports.items()}
    assert bits == {
        "y": "111",
        "bv_o": "0101001",
        "f_o": "1",
        "n_o": "00000101",
        "s_o": "".join(f"{ord(c):08b}" for c in "abcd"),
        "r_o": "00000011",
    }


@pytest.mark.parametrize("script_format", ["tcl"])
def test_a_tcl_script_gives_slang_a_string_parameter_with_whitespace(tmp_path, script_format):
    """A Tcl script has no tokenizer limit: the whole `NAME=VALUE` is one word, so whitespace and a
    `$` reach slang as they are. (A `.ys` script refuses whitespace in the value.)"""
    require_yosys_tcl()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    _write(tmp_path / "top.sv", PARAMETERIZED_TOP)
    design = Design(
        name="params",
        design_root=tmp_path,
        rtl={"sources": ["top.sv"], "top": "top", "parameters": {"S": "a b$"}},
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded
    ports = json.loads((flow.run_path / "netlist.json").read_text())["modules"]["top"]["ports"]
    assert "".join(str(b) for b in reversed(ports["s_o"]["bits"])) == "".join(
        f"{ord(c):08b}" for c in "a b$"
    )


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
def test_yosys_resolves_an_instance_of_a_verilog_lib_module_for_slang(tmp_path, script_format):
    """slang resolves every instance while it reads the sources, so the script reads a
    `verilog_lib` file (a black box) before them. Instances in a SystemVerilog and in a Verilog
    source both resolve."""
    require_yosys_tcl() if script_format == "tcl" else require_yosys()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    _write(
        tmp_path / "libcell.v", "module libcell(input a, output y);\n  assign y = ~a;\nendmodule\n"
    )
    _write(
        tmp_path / "gate.v",
        "module gate(input a, output y);\n  libcell u_lib(.a(a), .y(y));\nendmodule\n",
    )
    _write(
        tmp_path / "top.sv",
        "module top(input logic a, output logic y);\n  gate u_gate(.a(a), .y(y));\nendmodule\n",
    )
    design = Design(
        name="withlib", design_root=tmp_path, rtl={"sources": ["gate.v", "top.sv"], "top": "top"}
    )
    flow = DefaultRunner(tmp_path / "xeda run").run_flow(
        Yosys, design, {"verilog_lib": ["libcell.v"]}
    )
    assert flow is not None and flow.succeeded
    netlist = json.loads((flow.run_path / "netlist.json").read_text())["modules"]
    assert any(
        cell["type"] == "libcell"
        for module in netlist.values()
        for cell in module["cells"].values()
    )


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


def test_yosys_elaborates_a_vhdl_top_with_a_constraint_file_listed_after_it(tmp_path):
    """The top is the last source in a hardware language, not the last one listed: with `top.sdc`
    last the script handed the generic to GHDL and then asked `chparam` for it, and yosys stopped
    with "Module `inv' is used with parameters but is not parametric"."""
    require_yosys_ghdl_plugin()
    _write(
        tmp_path / "inv.vhd",
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity inv is generic(W: positive := 2);\n"
        "  port(a: in std_logic_vector(W-1 downto 0); y: out std_logic_vector(W-1 downto 0));\n"
        "end;\n"
        "architecture rtl of inv is begin y <= not a; end;\n",
    )
    _write(tmp_path / "top.sdc", "# constraints\n")
    design = Design(
        name="inv",
        design_root=tmp_path,
        rtl={"sources": ["inv.vhd", "top.sdc"], "top": "inv", "parameters": {"W": 5}},
    )
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, {})
    assert flow is not None and flow.succeeded
    script = (flow.run_path / "yosys_synth.ys").read_text()
    assert "-gW=5" in script and "chparam" not in script
    netlist = json.loads((flow.run_path / "netlist.json").read_text())
    assert len(netlist["modules"]["inv"]["ports"]["y"]["bits"]) == 5


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


@pytest.mark.parametrize("flow", ["yosys", "yosys_sim"])
def test_a_design_with_no_source_the_flow_reads_is_refused_when_planned(flow, tmp_path):
    """A netlist in a format yosys does not read is passed to no command: the script would
    read nothing, and `hierarchy` would fail inside the tool. Planning refuses it by name."""
    _write(tmp_path / "top.edf", "(edif top)\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["top.edf"], "top": "top"})
    with pytest.raises(FlowSettingsException, match=rf"{flow} reads none of the design's sources"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(flow, design)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


@pytest.mark.parametrize("flow", ["yosys", "yosys_sim"])
def test_a_language_the_flow_cannot_read_is_refused_by_name_when_planned(flow, tmp_path):
    """Passed over, a Chisel source among Verilog ones would leave a design without its
    modules, and the synthesis would go on with what is left."""
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "Top.sc", "// not for yosys\n")
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": ["top.v", "Top.sc"], "top": "top"}
    )
    with pytest.raises(
        FlowSettingsException,
        match=rf"{flow} cannot read the design's Chisel source\(s\) .*Top\.sc",
    ):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(flow, design)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


@pytest.mark.parametrize("flow", ["yosys", "yosys_sim"])
def test_a_bluespec_source_among_verilog_ones_is_converted_when_planned(flow, tmp_path):
    """bsc converts the Bluespec source, and the Verilog one passes through it."""
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "Top.bsv", "// for bsc\n")
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": ["top.v", "Top.bsv"], "top": "top"}
    )
    plan = DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(flow, design)
    assert [node.name for node in plan.nodes] == ["bsc", flow]
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


@pytest.mark.parametrize("flow", ["yosys", "yosys_sim"])
def test_a_design_of_the_types_the_flow_reads_is_planned_whatever_else_it_lists(flow, tmp_path):
    """Verilog, SystemVerilog, VHDL and their headers are read; a constraint file or a memory
    image beside them is passed over, not refused."""
    names = ["defs.vh", "pkg.svh", "a.v", "b.sv", "c.vhd", "top.sdc", "rom.mem"]
    for name in names:
        _write(tmp_path / name, "// a source\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": names, "top": "a"})
    planned = DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(flow, design)
    assert [node.name for node in planned.nodes] == [flow]


# ---------------------------------------------------------- what the shared bases may read


def _names_used_by(cls: type) -> dict[str, set[str]]:
    """Return each name that the code of `cls` (and of the classes nested in it) uses.

    A name is an attribute (`settings.name`) or a string literal that equals it, which covers
    `getattr(settings, "name")`, `setattr(...)` and a tuple of names that a loop reads by name.
    The value is the set of functions that use the name.
    """
    used: dict[str, set[str]] = {}

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{function}.{child.name}" if function else child.name)
                continue
            if isinstance(child, ast.Attribute):
                used.setdefault(child.attr, set()).add(function)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                used.setdefault(child.value, set()).add(function)
            visit(child, function)

    visit(ast.parse(inspect.getsource(cls)), "")
    return used


def test_the_shared_bases_read_no_setting_that_only_yosys_declares():
    """`YosysBase` and `YosysSynthesisBase` serve `yosys_fpga` and `yosys_sim` too. A method of
    theirs that reads a field of `Yosys.Settings` breaks the flows without it, and breaks when
    `Yosys` moves or removes that field. Each flow reads and prepares the settings it declares.
    """
    shared = set(YosysSynthesisBase.Settings.model_fields)
    assert shared >= set(YosysBase.Settings.model_fields)
    yosys_only = set(Yosys.Settings.model_fields) - shared
    assert {"arithmetic", "sequential", "abc", "techmap_files", "liberty", "gates"} <= yosys_only
    for base in (YosysBase, YosysSynthesisBase):
        reads = {
            name: sorted(functions)
            for name, functions in _names_used_by(base).items()
            if name in yosys_only
        }
        assert not reads, f"{base.__name__} uses a field that only Yosys.Settings declares: {reads}"


def test_the_map_files_resolve_against_the_design_root(tmp_path):
    """A relative map file of `yosys` (`arithmetic.adder_map_file`, `sequential.clockgate_map_file`,
    `sequential.latch_map_file` and `techmap_files`) names a file in the design root, and so does the
    `clockgate_map` of `yosys_fpga`. The settings of `yosys` keep the path as the user wrote it:
    `asic_values` gives the resolved path."""
    design = Design.from_file(TESTS_DIR / "resources" / "design0" / "design0.toml")
    root = design.design_root
    flow = Yosys(
        Yosys.Settings(
            liberty=["lib/cells.lib"],
            clock={"period": "5 ns"},
            arithmetic={"adder_map_file": "maps/add.v"},
            sequential={"clockgate_map_file": "maps/cg.v", "latch_map_file": "maps/latch.v"},
            techmap_files=["maps/a.v", "maps/b.v"],
        ),
        design,
        tmp_path / "yosys",
    )
    flow.init()
    assert isinstance(flow.settings, Yosys.Settings)
    values = flow.asic_values()
    assert values.adder_map == root / "maps/add.v"
    assert values.clockgate_map == root / "maps/cg.v"
    assert values.latch_map == root / "maps/latch.v"
    assert values.techmap_files == (("pre_dff", root / "maps/a.v"), ("pre_dff", root / "maps/b.v"))
    assert flow.settings.sequential.clockgate_map_file == Path("maps/cg.v")
    assert [item.path for item in flow.settings.techmap_files] == [
        Path("maps/a.v"),
        Path("maps/b.v"),
    ]
    fpga = YosysFpga(
        YosysFpga.Settings(
            fpga={"part": "xc7a12tcsg325-1"}, clock={"period": "5 ns"}, clockgate_map="maps/cg.v"
        ),
        design,
        tmp_path / "yosys_fpga",
    )
    fpga.init()
    assert isinstance(fpga.settings, YosysFpga.Settings)
    assert fpga.settings.clockgate_map == root / "maps/cg.v"
