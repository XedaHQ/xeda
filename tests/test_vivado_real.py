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
    """Provide a workspace for optional real Vivado tests."""
    require_vivado()
    path = checkout_work_dir("vivado_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, text: str) -> str:
    """Write a source file for a real Vivado test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def _logged(run_path: Path, text: str) -> bool:
    """Read messages from a Vivado run log."""
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


@pytest.mark.parametrize("waive_drc", [False, True], ids=["drc_errors", "drc_waived"])
def test_vivado_synth_fails_when_write_bitstream_does(work_dir, capfd, waive_drc) -> None:
    """Pins without a location or I/O standard fail `write_bitstream`'s DRC (UCIO-1, NSTD-1),
    and with it the implementation run: the flow fails, naming the run, its status and its log,
    and registers no bitstream. With the two checks made warnings before the step, the same
    design writes its bitstream where it is registered."""
    root = work_dir / "design"
    _write(root / "inv.v", INVERTER_V)
    _write(root / "top.vhd", TOP_VHD)
    waiver = _write(
        root / "waive_drc.tcl", "set_property SEVERITY {Warning} [get_drc_checks {NSTD-1 UCIO-1}]\n"
    )
    design = Design(
        name="bit",
        design_root=root,
        rtl={"sources": ["inv.v", "top.vhd"], "top": "top", "clock_port": "clk"},
    )
    settings = {"fpga": PART, "clock_period": 10.0, "bitstream": "outputs/top.bit"}
    if waive_drc:
        settings["impl"] = {"steps": {"WRITE_BITSTREAM": {"TCL": {"PRE": waiver}}}}
    flow = DefaultRunner(work_dir / "run").run_flow(VivadoSynth, design, settings)
    assert flow is not None and flow.succeeded == waive_drc
    log = flow.run_path / "bit.runs" / "impl_1" / "runme.log"
    # Vivado itself names the log when it launches the run ("Run output will be captured here")
    output = capfd.readouterr().out.splitlines()
    messages = [line for line in output if "ERROR:" in line and str(log) in line]
    if waive_drc:
        assert flow.results["status"] == "write_bitstream Complete!"
        assert (flow.run_path / flow.artifacts["bitstream"]).is_file()
        assert not messages
    else:
        assert flow.results["status"] == "write_bitstream ERROR"
        assert "bitstream" not in flow.results.artifacts
        assert len(messages) == 1 and '"write_bitstream ERROR"' in messages[0], messages
        assert _logged(flow.run_path, "UCIO-1")


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
    """Vivado sim runs a testbench."""
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
    report "tb done"; std.env.finish; wait;
  end process;
end;
""",
    )
    design = Design(
        name="sim",
        language={"vhdl": {"standard": "2008"}},
        design_root=root,
        rtl={"sources": ["inv.v"], "top": "inv"},
        tb={"sources": ["tb [x] $v.vhd"], "top": "tb"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(VivadoSim, design, {})
    assert flow is not None and flow.succeeded
    assert _logged(flow.run_path, "tb done")


@pytest.mark.parametrize(
    "body,settings,passes,ending,errors,warnings",
    [
        ("$finish(0);", {}, True, "finish", 0, 0),
        ("#5; $finish;", {}, True, "finish", 0, 0),
        ("#5;", {}, False, "unknown", 0, 0),
        ('#5; $warning("warning"); #1; $finish;', {}, True, "finish", 0, 1),
        ('#5; $error("error"); #1; $finish;', {}, False, "finish", 1, 0),
        ('#5; assert(0) else $error("assertion"); #1; $finish;', {}, False, "finish", 1, 0),
        ('#5; $fatal(1,"fatal");', {}, False, "fatal", 1, 0),
        ("#5; $stop;", {"fail_severity": "fatal"}, False, "error", 1, 0),
        ("forever #1 a=~a;", {"prerun_time": "10ns", "stop_time": "20ns"}, True, "stop_time", 0, 0),
        ("#5; $finish;", {"prerun_time": "10ns", "stop_time": "20ns"}, True, "finish", 0, 0),
        ("#5;", {"stop_time": "20ns"}, True, "stop_time", 0, 0),
    ],
    ids=[
        "finish0",
        "finish5",
        "drain",
        "warning",
        "error",
        "assertion",
        "fatal",
        "stop",
        "prerun_limit",
        "prerun_finish",
        "empty_limit",
    ],
)
def test_xsim_native_sv_evidence(work_dir, body, settings, passes, ending, errors, warnings):
    root = work_dir / "design"
    _write(root / "inv.v", INVERTER_V)
    _write(
        root / "tb.sv",
        "`timescale 1ns/1ps\nmodule tb; reg a=0; wire y; inv dut(a,y); initial begin "
        + body
        + " end endmodule\n",
    )
    design = Design(
        name="native",
        design_root=root,
        rtl={"sources": ["inv.v"], "top": "inv"},
        tb={"sources": ["tb.sv"], "top": "tb", "uut": "dut"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoSim, design, {"timeout": 180.0, **settings}
    )
    assert flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ending
    assert flow.results["sim.errors"] == errors
    assert flow.results["sim.warnings"] == warnings
    if ending == "stop_time":
        assert flow.results["sim.time"] == 20000
        assert flow.results["sim.time_unit"] == "1000fs"


@pytest.mark.parametrize(
    "body,passes,ending,errors,warnings",
    [
        ("finish;", True, "finish", 0, 0),
        ("stop;", True, "finish", 0, 0),
        ('report "warning" severity warning; wait for 1 ns; finish;', True, "finish", 0, 1),
        (
            'assert false report "error" severity error; wait for 1 ns; finish;',
            False,
            "finish",
            1,
            0,
        ),
        ('assert false report "failure" severity failure;', False, "fatal", 1, 0),
    ],
    ids=["finish", "stop", "warning", "assertion_error", "assertion_failure"],
)
def test_xsim_native_vhdl_evidence(work_dir, body, passes, ending, errors, warnings):
    root = work_dir / "design"
    _write(
        root / "tb.vhd",
        "library std; use std.env.all; entity tb is end; architecture sim of tb is begin process begin wait for 5 ns; "
        + body
        + " wait; end process; end;\n",
    )
    design = Design(
        name="native",
        design_root=root,
        rtl={"sources": ["tb.vhd"], "top": "tb"},
        tb={"top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoSim, design, {"timeout": 180.0, "prerun_time": "10ns", "stop_time": "20ns"}
    )
    assert flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ending
    assert flow.results["sim.errors"] == errors
    assert flow.results["sim.warnings"] == warnings


def test_xsim_native_power_delegates_netlist_evidence(work_dir):
    """A real routed netlist supplies activity and the same verdict to the power reporter."""
    from xeda.flows import VivadoPower

    root = work_dir / "design"
    _write(root / "inv.v", INVERTER_V)
    _write(root / "top.vhd", TOP_VHD)
    _write(
        root / "tb.sv",
        "`timescale 1ns/1ps\nmodule tb; reg clk=0,a=0; wire y; top dut(clk,a,y); always #1 clk=~clk; always #3 a=~a; endmodule\n",
    )
    design = Design(
        name="power",
        design_root=root,
        rtl={"sources": ["inv.v", "top.vhd"], "top": "top", "clock_port": "clk"},
        tb={"sources": ["tb.sv"], "top": "tb", "uut": "dut"},
    )
    settings = {
        "timing_sim": False,
        "timeout": 240.0,
        "prerun_time": "10ns",
        "stop_time": "20ns",
        "postsynthsim": {"synth": {"fpga": PART, "clock_period": 10.0, "ncpus": 2}},
    }
    flow = DefaultRunner(work_dir / "run").run_flow(VivadoPower, design, settings)
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "stop_time"
    assert flow.results["sim.time"] == 20000
    assert "Total On-Chip Power (W)" in flow.results
    assert not (flow.run_path / "xsim_runtime.log").exists()
    simulation = flow.run_path.parent / "vivado_postsynth_sim"
    import json

    assert (
        json.loads((simulation / "results.json").read_text())["sim.evidence"]
        == flow.results["sim.evidence"]
    )


def test_xsim_native_partial_line_diagnostic(work_dir):
    test_xsim_native_sv_evidence(
        work_dir,
        '#5; $write("progress:"); $error("partial"); #1; $finish;',
        {},
        False,
        "finish",
        1,
        0,
    )


def test_xsim_native_timeout_stops_the_container(work_dir):
    """Use Xeda's named-container runtime so its bounded stop hook is exercised too."""
    from .tool_utils import require_docker_image

    require_docker_image("axemsolutions/vivado:2024.2")
    root = work_dir / "design"
    _write(
        root / "tb.sv", "`timescale 1ns/1ps\nmodule tb; reg clk=0; always #1 clk=~clk; endmodule\n"
    )
    design = Design(
        name="timeout", design_root=root, rtl={"sources": ["tb.sv"], "top": "tb"}, tb={"top": "tb"}
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoSim, design, {"dockerized": True, "timeout": 90.0}
    )
    assert not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"
    assert "XEDA_XSIM_RUNTIME_START" in (flow.run_path / "xsim_runtime.log").read_text()


def test_xsim_native_partial_line_finish_during_prerun(work_dir):
    test_xsim_native_sv_evidence(
        work_dir,
        '#5; $write("progress:"); $finish;',
        {"prerun_time": "10ns", "stop_time": "20ns"},
        True,
        "finish",
        0,
        0,
    )
