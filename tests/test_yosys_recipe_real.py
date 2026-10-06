"""Compare pass-only synthesis with native yosys under matching reader and pass choices.

`tests/test_yosys_recipe.py` pins the *shape* of the rendered script under the fakes. This is the
other half: with reader flags, source paths and mapping choices matched, the netlist xeda writes
with `synth_pass_only` is the one the native command writes, cell for cell and name for name.

Name for name matters. Every `read_verilog` advances yosys's shared `autoidx`, which renumbers
the design's generated cell names, and ABC9 maps by those names -- so reading the target's
primitive libraries one extra time is by itself enough to change the netlist. Comparing the whole
netlist, not only its cell-type counts, is what makes this test see that.

Gated on `require_yosys()` rather than on `XEDA_TESTS_OPENXC7=1`: `synth_xilinx` is built into
plain yosys, so this runs wherever yosys does, CI included.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga

from .tool_utils import require_yosys, require_yosys_config

PART = "xc7a100tcsg324-1"

#: A hierarchy with arithmetic, comparison and shifting in submodules: enough LUT-mapping
#: pressure for ABC9's choices to show, and `-flatten` leaves `$scopeinfo` cells that the default
#: recipe's pre-pass `opt_clean -purge` removes.
SOURCE = """\
module adder #(parameter W = 16) (input [W-1:0] a, input [W-1:0] b, output [W:0] s);
  assign s = a + b;
endmodule

module cmp #(parameter W = 16) (input [W-1:0] a, input [W-1:0] b, output lt, output eq);
  assign lt = a < b;
  assign eq = a == b;
endmodule

module shifter (input [15:0] d, input [3:0] n, output [15:0] q);
  assign q = d << n;
endmodule

module top (input clk, input rst, input [15:0] a, input [15:0] b, input [3:0] n,
            output reg [16:0] acc, output reg flag);
  wire [16:0] s;
  wire lt, eq;
  wire [15:0] sh;
  adder #(.W(16)) u_add (.a(a), .b(b), .s(s));
  cmp   #(.W(16)) u_cmp (.a(a), .b(b), .lt(lt), .eq(eq));
  shifter u_sh (.d(a), .n(n), .q(sh));
  always @(posedge clk) begin
    if (rst) begin acc <= 0; flag <= 0; end
    else begin
      acc  <= s + {1'b0, sh} + {16'b0, lt};
      flag <= eq ^ lt;
    end
  end
endmodule
"""

#: `flatten` so the pass gets `-flatten`, as openXC7's own Makefile passes it
SETTINGS: dict[str, Any] = {
    "fpga": PART,
    "clock": {"period": 5.0},
    "flatten": True,
    # Plain `yosys file.v` uses its Verilog reader without xeda's default `-sv`, and its own
    # front end rather than xeda's default slang plugin.
    "read_verilog_flags": [],
    "systemverilog": "default",
}


def _design(tmp_path: Path, source: str = "hier.v", text: str = SOURCE) -> Design:
    root = tmp_path / "design"
    root.mkdir(parents=True, exist_ok=True)
    (root / source).write_text(text)
    return Design(
        name="top",
        design_root=root,
        rtl={"sources": [source], "top": "top", "clock": {"port": "clk"}},
    )


#: Plain Verilog that is not SystemVerilog: `logic` is an identifier here and a keyword under
#: `read_verilog -sv`, so the one reader flag xeda's own default passes decides whether it reads
KEYWORD_SOURCE = """\
module top(input clk, input a, output reg q);
  wire logic;
  assign logic = ~a;
  always @(posedge clk) q <= logic;
endmodule
"""


def _structure(netlist: Any) -> Any:
    """The netlist without its attributes: modules, cells, their types, parameters and wiring.

    `creator` carries the yosys version, and `attributes` carry `src` locations that name the
    file each side read -- and which xeda strips by default (`netlist_src_attrs`). Everything
    else, cell names included, must be equal.
    """
    if isinstance(netlist, dict):
        return {
            key: _structure(value)
            for key, value in netlist.items()
            if key not in ("attributes", "creator")
        }
    if isinstance(netlist, list):
        return [_structure(value) for value in netlist]
    return netlist


def _launch(tmp_path: Path, name: str, **settings: Any):
    """Run `yosys_fpga` in its own run root, so two recipes keep two netlists."""
    runner = DefaultRunner(tmp_path / name, display_results=False)
    flow = runner.run(YosysFpga, _design(tmp_path), flow_settings={**SETTINGS, **settings})
    assert flow is not None and flow.succeeded, f"{name} did not synthesize"
    return flow


def _netlist(flow: Any) -> Any:
    return _structure(json.loads((Path(flow.run_path) / "netlist.json").read_text()))


def _synth_line(flow: Any) -> str:
    """The one command `synth_pass_only` renders: the pass with the flags xeda chose.

    Taken from the rendered script rather than rebuilt here, so the reference runs literally what
    xeda ran and no flag is ever hand-typed.
    """
    script = (Path(flow.run_path) / "yosys_fpga_synth.ys").read_text()
    (line,) = [ln.strip() for ln in script.splitlines() if ln.startswith("synth_")]
    assert line.startswith("synth_xilinx "), line
    return line


def _reference(
    tmp_path: Path, synth: str, abc9_script: str | None = None, source: str = "hier.v"
) -> Any:
    """Run the native pass with the same optional mapping script and reader settings.

    The real command line, not a deferred-read equivalent of it, so a difference between
    `read_verilog -sv` and the frontend yosys picks for a `.v` file would show up here.
    """
    work = tmp_path / "reference"
    work.mkdir(parents=True, exist_ok=True)
    out = work / "reference.json"
    yosys = shutil.which("yosys")
    assert yosys, "require_yosys() passed but yosys is not on PATH"
    mapping = f"scratchpad -copy abc9.script.{abc9_script} abc9.script; " if abc9_script else ""
    result = subprocess.run(
        [
            yosys,
            "-q",
            "-p",
            f"{mapping}{synth}; write_json {out}",
            str(tmp_path / "design" / source),
        ],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return _structure(json.loads(out.read_text()))


@pytest.fixture(scope="module")
def _yosys() -> None:
    require_yosys()


pytestmark = pytest.mark.usefixtures("_yosys")


def test_synth_pass_only_writes_the_netlist_the_pass_writes_on_its_own(tmp_path):
    mode = _launch(tmp_path, "pass-only", synth_pass_only=True)
    assert _netlist(mode) == _reference(tmp_path, _synth_line(mode))


@pytest.mark.parametrize(
    "script", ["default", "default.area", "default.fast", "flow", "flow2", "flow3", "flow3mfs"]
)
def test_a_selected_abc9_script_matches_the_same_native_mapping_choice(tmp_path, script):
    mode = _launch(tmp_path, "selected-script", synth_pass_only=True, abc9_script=script)
    assert _netlist(mode) == _reference(tmp_path, _synth_line(mode), script)


def test_a_library_the_pass_reads_is_not_read_again_when_verilog_lib_spells_it_as_a_path(tmp_path):
    """`+/xilinx/cells_sim.v` and the file it names under yosys' data directory are one library.

    The data directory is what the installed yosys reports (`yosys-config --datdir`), the
    directory its own `+/` stands for. One more read of that library, by whatever spelling,
    moves the netlist (`test_an_extra_primitive_library_read_alone_changes_the_netlist`).
    """
    require_yosys_config()
    datdir = Path(
        subprocess.run(
            ["yosys-config", "--datdir"], capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()
    )
    library = datdir / "xilinx" / "cells_sim.v"
    assert library.is_file(), f"`+/` does not stand for {datdir}"
    mode = _launch(tmp_path, "pass-only", synth_pass_only=True)
    spelled = _launch(tmp_path, "as-path", synth_pass_only=True, verilog_lib=[str(library)])
    assert _netlist(spelled) == _netlist(mode) == _reference(tmp_path, _synth_line(mode))


def test_the_default_recipe_writes_a_different_netlist_on_the_same_design(tmp_path):
    """The teeth: without this, a `synth_pass_only` that changed nothing would still pass.

    The full recipe elaborates, cleans and tells ABC9 the clock period, so its result can differ.
    This comparison checks that the modes differ; it makes no claim about their area or timing.
    """
    mode = _launch(tmp_path, "pass-only", synth_pass_only=True)
    default = _launch(tmp_path, "default")
    reference = _reference(tmp_path, _synth_line(mode))
    assert _netlist(default) != reference
    # and the difference is in the mapped logic, not only in bookkeeping cells
    assert _cells(_netlist(default)) != _cells(reference)
    assert _cells(_netlist(mode)) == _cells(reference)


def _cells(netlist: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for module in netlist["modules"].values():
        for cell in module["cells"].values():
            counts[cell["type"]] = counts.get(cell["type"], 0) + 1
    return dict(sorted(counts.items()))


def test_an_extra_primitive_library_read_alone_changes_the_netlist(tmp_path):
    """Why the mode may not keep xeda's early library read, as a measurement rather than a claim.

    The libraries that result are the same either way -- the pass re-reads them and the last read
    wins -- but each read advances `autoidx`, the design's generated cell names move with it, and
    ABC9 maps by those names. It is also why a one- or two-cell difference between two flows is
    no evidence at all about either one's quality.
    """
    mode = _launch(tmp_path, "pass-only", synth_pass_only=True)
    synth = _synth_line(mode)
    work = tmp_path / "autoidx"
    work.mkdir(parents=True, exist_ok=True)
    yosys = shutil.which("yosys")
    assert yosys
    netlists = []
    for reads in (0, 1):
        out = work / f"read{reads}.json"
        prologue = "read_verilog -lib -specify +/xilinx/cells_sim.v; " * reads
        result = subprocess.run(
            [
                yosys,
                "-q",
                "-p",
                f"{prologue}read_verilog -defer {tmp_path / 'design/hier.v'}; "
                f"{synth}; write_json {out}",
            ],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        netlists.append(_structure(json.loads(out.read_text())))
    assert netlists[0] != netlists[1], (
        "an extra library read no longer changes the netlist on this yosys; re-check the "
        "`autoidx` reasoning in design-notes before relying on it"
    )
    assert netlists[0] == _reference(tmp_path, synth)


# ----------------------------------------------- the reader, as plain `yosys <file>` has it


def _keyword_launch(tmp_path: Path, name: str, **settings: Any) -> Any:
    runner = DefaultRunner(tmp_path / name, display_results=False)
    return runner.run(
        YosysFpga,
        _design(tmp_path, "kw.v", KEYWORD_SOURCE),
        flow_settings={"fpga": PART, "clock": {"period": 5.0}, **settings},
    )


def test_the_mode_reads_a_plain_verilog_source_as_yosys_does_and_the_default_reader_does_not(
    tmp_path,
):
    """The reader flag that moved the mode away from `yosys <file>`, observed rather than argued.

    Native yosys reads `kw.v`. xeda's default `read_verilog_flags` (`-sv`) cannot: the same
    source is a syntax error there, so a comparison made with the default would have compared a
    failure with a netlist. With the flags the mode requires (`[]`), xeda reads it and writes
    the netlist the native pass writes, cell for cell and name for name.
    """
    from xeda.flow import FlowSettingsError

    # the full recipe, default `-sv`: the source does not read
    failed = _keyword_launch(tmp_path, "default-recipe")
    assert failed is None or not failed.succeeded
    # the mode refuses the default before any tool runs, and says what to write instead
    with pytest.raises(FlowSettingsError, match=r"read_verilog_flags: \[\]"):
        _keyword_launch(tmp_path, "refused", synth_pass_only=True)
    # the mode with no reader flag reads it as yosys does, and writes the native netlist
    mode = _keyword_launch(
        tmp_path, "pass-only", synth_pass_only=True, read_verilog_flags=[], systemverilog="default"
    )
    assert mode is not None and mode.succeeded
    netlist = _structure(json.loads((Path(mode.run_path) / "netlist.json").read_text()))
    assert netlist == _reference(tmp_path, _synth_line(mode), source="kw.v")


SV_SOURCE = """\
module top(input logic clk, input logic [3:0] a, output logic [3:0] q);
  logic [3:0] r;
  always_ff @(posedge clk) r <= a + 4'd1;
  assign q = r;
endmodule
"""


def test_the_mode_reads_a_systemverilog_source_as_yosys_does(tmp_path):
    """`yosys top.sv` reads a `.sv` file with the built-in front end, `read_verilog -sv`.

    The mode needs `systemverilog=default` for exactly that: with it, the netlist is the native
    one, name for name. The default front end (the slang plugin) is refused before any tool
    runs; that front end is not run here, so nothing is claimed about what it would write.
    """
    from xeda.flow import FlowSettingsError

    design = _design(tmp_path, "top.sv", SV_SOURCE)

    def launch(name: str, **settings: Any) -> Any:
        return DefaultRunner(tmp_path / name, display_results=False).run(
            YosysFpga,
            design,
            flow_settings={"fpga": PART, "clock": {"period": 5.0}, **settings},
        )

    with pytest.raises(FlowSettingsError, match=r"systemverilog: default"):
        launch("slang", synth_pass_only=True, read_verilog_flags=[])
    mode = launch("pass-only", synth_pass_only=True, read_verilog_flags=[], systemverilog="default")
    assert mode is not None and mode.succeeded
    netlist = _structure(json.loads((Path(mode.run_path) / "netlist.json").read_text()))
    assert netlist == _reference(tmp_path, _synth_line(mode), source="top.sv")
