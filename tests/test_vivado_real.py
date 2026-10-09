"""Vivado flows run by a real Vivado.

Opt-in, since each takes a minute or more: set ``XEDA_TESTS_VIVADO=1`` with `vivado` on PATH.
A wrapper that runs Vivado in a container works too, as long as it mounts the checkout: the tests
work under the checkout's ``xeda_run/`` (or ``XEDA_TESTS_WORK_DIR``), not the system temp
directory. The designs are tiny; what the tests check is that xeda drives Vivado correctly --
file names with spaces, brackets and ``$``, constraint files, the project a flow creates.
"""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoImpl, VivadoProject, VivadoSim, VivadoSynth, YosysFpga

from .tool_utils import checkout_work_dir, require_iverilog, require_vivado, require_yosys

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
    """`vivado_alt_synth` reads the XDC files it is given, not only its generated clock constraints."""
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


def test_xsim_native_power_uses_successful_declared_activity(work_dir):
    """A real routed netlist supplies checked activity; only its producer reports a verdict."""
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
    design.flow = {
        "vivado_synth": {"fpga": PART, "clock_period": 10.0, "ncpus": 2},
        "vivado_postsynth_sim": {"timeout": 240.0, "prerun_time": "10ns", "stop_time": "20ns"},
    }
    runner = DefaultRunner(work_dir / "run")
    launch = lambda: runner.run_flow(VivadoPower, design, {})
    flow = launch()
    assert flow.succeeded
    assert not any(key.startswith("sim.") for key in flow.results)
    assert "Total On-Chip Power (W)" in flow.results
    assert not (flow.run_path / "xsim_runtime.log").exists()
    simulation = next(f for f in runner.launched if f.name == "vivado_postsynth_sim")
    assert simulation.results["sim.ended_by"] == "stop_time"
    assert simulation.results["sim.time"] == 20000
    assert flow.inputs.activity == simulation.outputs.timing_saif
    from .tool_utils import launch_until_fresh

    launch_until_fresh(runner, launch)
    entered = len(runner.launched)
    assert launch().reused
    assert all(f.reused for f in runner.launched[entered:])


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


# ---------------------------------------------------------------------------------------------
# vivado_impl: the netlist yosys writes, implemented by Vivado (needs the real yosys too)
# ---------------------------------------------------------------------------------------------

IMPL_RESOURCES = Path(__file__).parent / "resources" / "vivado_impl"
BASYS3 = "xc7a35tcpg236-1"
IMPL_SETTINGS = {"fpga": BASYS3, "clock": {"period": 10.0}}


def _impl_design(root: Path, name: str, netlist: str | None = None) -> Design:
    """One of the Basys 3 designs of `resources/vivado_impl`, with its pin constraints, copied
    into `root`; its netlist is the synthesis's, or the EDIF file `netlist` (a name that is not
    the top's)."""
    root.mkdir(parents=True, exist_ok=True)
    for file in (f"{name}.v", f"{name}.xdc"):
        shutil.copy(IMPL_RESOURCES / file, root / file)
    sources = [f"{name}.xdc"] if netlist else [f"{name}.v", f"{name}.xdc"]
    if netlist:
        sources.insert(0, {"file": netlist, "type": "Edif"})  # type: ignore[arg-type]
    return Design(
        name=name,
        design_root=root,
        rtl={"sources": sources, "top": name, "clock": {"port": "clk"}},
    )


def test_vivado_impl_builds_the_bitstream_of_a_yosys_netlist(work_dir) -> None:
    """`xeda run yosys_fpga+vivado_impl`, on the real tools: a 27-bit counter driving four LEDs.
    A netlist read with its buses reversed kept 7 of its 27 registers, met timing and got a
    bitstream all the same: the register count is what shows the buses came through."""
    require_yosys()
    design = _impl_design(work_dir / "design", "blinky")
    bitstream = "outputs/blinky.bit"
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoImpl, design, {**IMPL_SETTINGS, "bitstream": bitstream}
    )
    assert flow is not None and flow.succeeded
    assert flow.results["ff"] == 27 and flow.results["lut"] == 1
    assert flow.results["wns"] > 0 and flow.results["Fmax"] > 100
    assert (flow.run_path / bitstream).stat().st_size > 100_000
    assert _logged(flow.run_path, "Parsing EDIF File [./blinky.edif]")
    assert _logged(flow.run_path, "link_design completed successfully")


def test_vivado_impl_runs_the_power_optimization_steps_it_is_asked_for(work_dir) -> None:
    """`impl.steps.power_opt` has the script optimize the power after placement and write a report
    in `reports/post_place`. Vivado makes no directory for a report, so the script makes this one.
    """
    require_yosys()
    steps = {"power_opt": {"verbose": True}}  # any value that is not empty switches the step on
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoImpl,
        _impl_design(work_dir / "design", "blinky"),
        {**IMPL_SETTINGS, "impl": {"steps": steps}},
    )
    assert flow is not None and flow.succeeded
    report = flow.run_path / "reports" / "post_place" / "post_place_power_optimization.rpt"
    assert report.is_file() and "Power optimization report" in report.read_text()
    assert flow.results["ff"] == 27 and flow.results["wns"] > 0


def test_vivado_impl_implements_an_edif_netlist_it_is_given(work_dir) -> None:
    """A listed netlist named other than its top: Vivado looks the top up by the name of the
    file, so the flow reads a copy named for it."""
    require_yosys()
    synthesis = DefaultRunner(work_dir / "synthesis").run_flow(
        YosysFpga, _impl_design(work_dir / "design", "blinky"), {"fpga": BASYS3}
    )
    assert synthesis is not None and synthesis.succeeded
    shutil.copy(synthesis.run_path / "netlist.edif", work_dir / "design" / "given.edf")
    runner = DefaultRunner(work_dir / "run")
    flow = runner.run_flow(
        VivadoImpl,
        _impl_design(work_dir / "design", "blinky", netlist="given.edf"),
        {**IMPL_SETTINGS, "bitstream": "outputs/blinky.bit"},
    )
    assert flow is not None and flow.succeeded
    assert [f.name for f in runner.launched] == ["vivado_impl"], "no synthesis ran"
    assert flow.results["ff"] == 27
    assert _logged(flow.run_path, "Parsing EDIF File [./blinky.edif]")


def test_vivado_finds_the_top_of_an_edif_netlist_by_the_name_of_its_file(work_dir) -> None:
    """The behavior the staged copy answers: a netlist not named for its top is not found."""
    require_yosys()
    synthesis = DefaultRunner(work_dir / "synthesis").run_flow(
        YosysFpga, _impl_design(work_dir / "design", "blinky"), {"fpga": BASYS3}
    )
    assert synthesis is not None and synthesis.succeeded
    shutil.copy(synthesis.run_path / "netlist.edif", work_dir / "given.edf")
    (work_dir / "link.tcl").write_text(
        "read_edif given.edf\nlink_design -part " + BASYS3 + " -top blinky\n"
    )
    result = subprocess.run(
        ["vivado", "-mode", "batch", "-nojournal", "-nolog", "-source", "link.tcl"],
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode != 0
    assert "No files found to match top module 'blinky'" in result.stdout + result.stderr


def _simulate(directory: Path, files: list[str], tops: list[str], libraries=()) -> str:
    """The output of an Icarus Verilog simulation of `files` in `directory`."""
    command = ["iverilog", "-g2012", "-o", "sim.vvp"]
    for top in tops:
        command += ["-s", top]
    for library in libraries:
        command += ["-y", library]
    subprocess.run([*command, "-Y", ".v", *files], cwd=directory, check=True, capture_output=True)
    return subprocess.run(
        ["vvp", "sim.vvp"], cwd=directory, check=True, capture_output=True, text=True
    ).stdout


def test_vivado_impl_implements_block_ram_dsp_and_carry_chains_and_the_result_works(
    work_dir,
) -> None:
    """A 1K x 16 block RAM with initial contents and a 16 x 16 multiply-accumulate, mapped by
    yosys and implemented by Vivado. The routed design is simulated and compared with the RTL:
    the flow's own script, with a functional netlist written where it writes the bitstream, and
    Vivado's own models of the primitives (copied out of its installation)."""
    require_yosys()
    require_iverilog()
    design = _impl_design(work_dir / "design", "macram")
    bitstream = "outputs/macram.bit"
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoImpl, design, {**IMPL_SETTINGS, "bitstream": bitstream}
    )
    assert flow is not None and flow.succeeded
    assert (flow.results["bram_RAMB18"], flow.results["dsp"], flow.results["ff"]) == (1, 1, 36)
    assert (flow.run_path / bitstream).stat().st_size > 100_000

    sim = work_dir / "sim"
    sim.mkdir()
    for name in ("clock.xdc", "macram.edif", "vivado_impl.tcl"):
        shutil.copy(flow.run_path / name, sim / name)
    for name in ("macram.v", "tb_macram.v"):
        shutil.copy(IMPL_RESOURCES / name, sim / name)
    script = (sim / "vivado_impl.tcl").read_text()
    write = f'write_bitstream -force "{bitstream}"'
    assert script.count(write) == 1
    unisims = "[file join $::env(XILINX_VIVADO) data verilog src %s]"
    (sim / "netlist.tcl").write_text(
        script.replace(
            write,
            "write_verilog -mode funcsim -force post_route.v\n"
            f"file copy -force {unisims % 'unisims'} unisims\n"
            f"file copy -force {unisims % 'glbl.v'} glbl.v",
        )
    )
    subprocess.run(
        ["vivado", "-mode", "batch", "-nojournal", "-nolog", "-source", "netlist.tcl"],
        cwd=sim,
        check=True,
        capture_output=True,
        timeout=1800,
    )
    expected = _simulate(sim, ["tb_macram.v", "macram.v"], ["tb_macram"])
    routed = _simulate(
        sim, ["tb_macram.v", "post_route.v", "glbl.v"], ["tb_macram", "glbl"], libraries=["unisims"]
    )
    assert len(expected.splitlines()) > 1900
    assert routed == expected
