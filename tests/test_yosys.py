import gzip
import json
import re
import shutil
import tempfile
from functools import cache
from pathlib import Path
from typing import get_args

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys, YosysFpga
from xeda.flows.yosys.yosys import preproc_libs

from .tool_utils import _command_succeeds, _require, require_yosys, require_yosys_ghdl_plugin

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


def test_yosys_synth_py() -> None:
    require_yosys_ghdl_plugin()
    # settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    # run_dir = "tests_run_dir"
    design_paths = [
        EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.toml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.xeda.yaml",
        EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky_vhdl.xeda.yaml",
    ]
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
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
)
# Path settings the sweep does not set: the reports land in the run directory, whose path has a
# space; yosys reads no `lib_paths` of its own -- the ghdl plugin's are `ghdl.lib_paths`, which
# `test_yosys_reads_vhdl_from_a_path_with_spaces` sets.
NOT_SWEPT = {"reports_dir", "outputs_dir", "checkpoints_dir", "lib_paths"}


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
    """Write a Liberty file for Yosys tests."""
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
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    flow = DefaultRunner(tmp_path).run_flow(YosysFpga, design, {"fpga": "LFE5U-25F-6BG381C"})
    assert flow is not None and flow.succeeded
    ghdl_line = next(
        line
        for line in (flow.run_path / "yosys_fpga_synth.ys").read_text().splitlines()
        if line.startswith("ghdl ")
    )
    assert ghdl_line.count("sqrt.vhdl") == 1, ghdl_line
