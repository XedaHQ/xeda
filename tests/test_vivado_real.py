"""Vivado flows run by a real Vivado.

Opt-in, since each takes a minute or more: set ``XEDA_TESTS_VIVADO=1`` with `vivado` on PATH.
A wrapper that runs Vivado in a container works too, as long as it mounts the checkout: the tests
work under the checkout's ``xeda_run/`` (or ``XEDA_TESTS_WORK_DIR``), not the system temp
directory. The designs are tiny; what the tests check is that xeda drives Vivado correctly --
file names with spaces, brackets and ``$``, constraint files, the project a flow creates.
"""

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoProject, VivadoSim, VivadoSynth

from .tool_utils import checkout_work_dir, require_vivado

PART = "xc7a12tcsg325-1"
INVERTER_V = "module inv(input a, output y); assign y = ~a; endmodule\n"
TOP_VHD = """library ieee; use ieee.std_logic_1164.all;
entity top is port (clk, a : in std_logic; y : out std_logic); end;
architecture rtl of top is
  component inv port (a : in std_logic; y : out std_logic); end component;
  signal n : std_logic;
begin
  u_inv : inv port map (a => a, y => n);
  process (clk) begin if rising_edge(clk) then y <= n; end if; end process;
end;
"""
IO_XDC = "set_property IOSTANDARD LVCMOS33 [get_ports *]\n"


@pytest.fixture
def work_dir():
    require_vivado()
    path = checkout_work_dir("vivado_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def _logged(run_path: Path, text: str) -> bool:
    return any(text in log.read_text(errors="ignore") for log in run_path.rglob("*.log"))


def test_vivado_synth_reads_every_file_by_its_own_name(work_dir) -> None:
    """Sources named with a space, brackets and `$` reach `read_verilog`/`read_vhdl` whole, and
    a constraint file with a space reaches the project (`add_files` refuses `[`, `]`, `$`)."""
    root = work_dir / "design"
    _write(root / "rtl" / "inv [x] $v.v", INVERTER_V)
    _write(root / "rtl" / "top [x] $v.vhd", TOP_VHD)
    xdc = _write(root / "constr" / "io x.xdc", IO_XDC)
    design = Design(
        name="odd",
        design_root=root,
        rtl={
            "sources": ["rtl/inv [x] $v.v", "rtl/top [x] $v.vhd"],
            "top": "top",
            "clock_port": "clk",
        },
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoSynth, design, {"fpga": PART, "clock_period": 10.0, "xdc_files": [xdc]}
    )
    assert flow is not None and flow.succeeded
    assert flow.results["lut"] >= 1 and flow.results["ff"] >= 1
    assert _logged(flow.run_path, "io x.xdc")


def test_vivado_alt_synth_reads_the_xdc_files_it_is_given(work_dir) -> None:
    """`vivado_alt_synth` used to read its generated clock constraints only."""
    root = work_dir / "design"
    _write(root / "inv.v", INVERTER_V)
    _write(root / "top.vhd", TOP_VHD)
    xdc = _write(root / "io.xdc", IO_XDC)
    design = Design(
        name="alt",
        design_root=root,
        rtl={"sources": ["inv.v", "top.vhd"], "top": "top", "clock_port": "clk"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoAltSynth, design, {"fpga": PART, "clock_period": 10.0, "xdc_files": [xdc]}
    )
    assert flow is not None and flow.succeeded
    assert _logged(flow.run_path, f"Parsing XDC File [{xdc}]")


def test_vivado_project_creates_the_project_without_a_display(work_dir) -> None:
    """The project flow ended in `start_gui`, which fails in a headless Vivado; it creates and
    saves the project, with the constraints in the constraint fileset only."""
    root = work_dir / "design"
    _write(root / "inv.v", INVERTER_V)
    _write(root / "top.vhd", TOP_VHD)
    _write(root / "io.xdc", IO_XDC)
    design = Design(
        name="proj",
        design_root=root,
        rtl={"sources": ["inv.v", "top.vhd", "io.xdc"], "top": "top", "clock_port": "clk"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoProject, design, {"fpga": PART, "clock_period": 10.0}
    )
    assert flow is not None and flow.succeeded
    project = flow.run_path / flow.artifacts["project"]
    filesets = {
        fileset.get("Name"): sorted(Path(f.get("Path", "")).name for f in fileset.iter("File"))
        for fileset in ET.parse(project).getroot().iter("FileSet")
    }
    assert filesets["sources_1"] == ["inv.v", "top.vhd"]
    assert filesets["constrs_1"] == ["clock.xdc", "io.xdc"]
    assert filesets["utils_1"] == sorted(
        f"post_{step}_hook.tcl"
        for step in ("synth_design", "place_design", "phys_opt_design", "route_design")
    )


def test_vivado_sim_runs_a_testbench(work_dir) -> None:
    root = work_dir / "design"
    _write(root / "inv.v", INVERTER_V)
    _write(
        root / "tb [x] $v.vhd",
        """library ieee; use ieee.std_logic_1164.all;
entity tb is end;
architecture sim of tb is
  component inv port (a : in std_logic; y : out std_logic); end component;
  signal a, y : std_logic := '0';
begin
  u_inv : inv port map (a => a, y => y);
  process begin
    a <= '0'; wait for 1 ns; assert y = '1' report "inverter failed" severity failure;
    a <= '1'; wait for 1 ns; assert y = '0' report "inverter failed" severity failure;
    report "tb done"; wait;
  end process;
end;
""",
    )
    design = Design(
        name="sim",
        design_root=root,
        rtl={"sources": ["inv.v"], "top": "inv"},
        tb={"sources": ["tb [x] $v.vhd"], "top": "tb"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(VivadoSim, design, {})
    assert flow is not None and flow.succeeded
    assert _logged(flow.run_path, "tb done")
