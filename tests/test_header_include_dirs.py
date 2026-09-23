"""Every header's directory is an include directory, for the testbench's headers too.

VCS is not installed here, so its command lines are recorded rather than run; Verilator runs.
"""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Vcs, Verilator

from .tool_utils import require_verilator


@pytest.fixture
def commands(monkeypatch) -> list[list[str]]:
    """Every command a tool would run, instead of running it."""
    recorded: list[list[str]] = []

    def record(executable, args=None, **kwargs):
        recorded.append([str(executable), *(str(a) for a in args or [])])
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    return recorded


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_vlogan_searches_every_header_directory(tmp_path, commands):
    """`+incdir+` was built from a list nothing ever filled, so a design whose sources include
    headers from another directory could not be analyzed. Every RTL and testbench header's
    directory is an include directory, once each, as Verilator and yosys have it."""
    _write(tmp_path / "inc" / "defs.vh", "`define W 4\n")
    _write(tmp_path / "inc" / "more.vh", "`define V 4\n")
    _write(tmp_path / "tb inc" / "tb_defs.svh", "`define N 3\n")
    _write(tmp_path / "rtl" / "top.v", '`include "defs.vh"\nmodule top; endmodule\n')
    _write(tmp_path / "tb.sv", '`include "tb_defs.svh"\nmodule tb; top uut(); endmodule\n')
    design = Design(
        name="incdirs",
        design_root=tmp_path,
        rtl={"sources": ["inc/defs.vh", "inc/more.vh", "rtl/top.v"], "top": "top"},
        tb={"sources": ["tb inc/tb_defs.svh", "tb.sv"], "top": "tb"},
    )
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Vcs, design, {})
    assert flow is not None
    vlogan = [cmd for cmd in commands if Path(cmd[0]).name == "vlogan"]
    assert len(vlogan) == 2, "one analysis for Verilog, one for SystemVerilog"
    expected = [f"+incdir+{tmp_path / 'inc'}", f"+incdir+{tmp_path / 'tb inc'}"]
    for cmd in vlogan:
        assert [a for a in cmd if a.startswith("+incdir+")] == expected


def test_verilator_searches_the_testbench_header_directories(tmp_path):
    """Verilator compiles the testbench too, but only the RTL headers' directories were
    include directories, so a testbench header could not be found."""
    require_verilator()
    _write(tmp_path / "inc dir" / "defs.vh", "`define INC 1\n")
    _write(tmp_path / "tb inc" / "tb_defs.vh", "`define EXPECTED 5\n")
    _write(
        tmp_path / "rtl" / "top.v",
        '`include "defs.vh"\nmodule top(input [3:0] a, output [3:0] y); assign y = a + `INC; endmodule\n',
    )
    _write(
        tmp_path / "tb.v",
        '`include "tb_defs.vh"\n'
        "module tb;\n"
        "  reg [3:0] a = 4; wire [3:0] y;\n"
        "  top uut(.a(a), .y(y));\n"
        "  initial begin #1; if (y != `EXPECTED) $stop; $finish; end\n"
        "endmodule\n",
    )
    design = Design(
        name="headers",
        design_root=tmp_path,
        rtl={"sources": ["inc dir/defs.vh", "rtl/top.v"], "top": "top"},
        tb={"sources": ["tb inc/tb_defs.vh", "tb.v"], "top": "tb"},
    )
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Verilator, design, {"timing": True})
    assert flow is not None and flow.succeeded
