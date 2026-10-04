import json
import re
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import VhdlSettings
from xeda.flow import FlowException
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, GhdlSynth
from xeda.flows.ghdl import GhdlTool

from .tool_utils import require_ghdl

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_ghdl_sim_py(tmp_path: Path) -> None:
    require_ghdl()
    # settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    # run_dir = "tests_run_dir"
    design_paths = [
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium-dc.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "pipeline" / "pipelined_adder.yaml",
    ]
    run_dir = tmp_path / "xeda_run"
    for design in design_paths:
        xeda_runner = DefaultRunner(run_dir, debug=debug)
        flow = xeda_runner.run(GhdlSim, design, flow_overrides=dict(debug=debug, verbose=debug))
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert flow.succeeded
        assert isinstance(flow.settings, GhdlSim.Settings)
        assert results_json.exists()


@pytest.mark.parametrize(
    "lib_paths, flags",
    [
        ([], []),
        ([(None, "/libs/a")], ["-P/libs/a"]),
        ([("mylib", "/libs/b"), (None, "/libs/c")], ["-P/libs/b", "-P/libs/c"]),
        ([("ieee_proposed", None)], []),
    ],
)
def test_lib_paths_become_ghdl_search_directories(lib_paths, flags):
    """`lib_paths` entries are (library name, path) pairs; GHDL's `-P<dir>` takes the path. The
    whole pair used to be formatted in, as `-P('mylib', '/libs/b')`."""
    ss = GhdlSim.Settings(lib_paths=lib_paths)
    assert [f for f in ss.common_flags(VhdlSettings()) if f.startswith("-P")] == flags


def _vhdl_inverter(entity: str) -> str:
    """Create a small VHDL inverter design."""
    return (
        "library ieee; use ieee.std_logic_1164.all;\n"
        f"entity {entity} is port(a: in std_logic; y: out std_logic); end;\n"
        f"architecture rtl of {entity} is begin y <= not a; end;\n"
    )


def _same_stem_design(root: Path, top: bool = False) -> Design:
    """`rtl/a/fifo.vhd` and `rtl/b/fifo.vhd`: distinct files sharing a base name, which GHDL's
    LLVM and GCC backends compile to the same object file (`fifo.o`)."""
    sources = []
    for sub in ("a", "b"):
        path = root / "rtl" / sub / "fifo.vhd"
        path.parent.mkdir(parents=True)
        path.write_text(_vhdl_inverter(f"fifo_{sub}"))
        sources.append(f"rtl/{sub}/fifo.vhd")
    if top:
        (root / "top.vhd").write_text(
            "library ieee; use ieee.std_logic_1164.all;\n"
            "entity top is port(a: in std_logic; y1, y2: out std_logic); end;\n"
            "architecture rtl of top is begin\n"
            "  u1: entity work.fifo_a port map(a, y1);\n"
            "  u2: entity work.fifo_b port map(a, y2);\n"
            "end;\n"
        )
        sources.append("top.vhd")
    return Design(
        name="stem",
        design_root=root,
        rtl={"sources": sources, "top": "top" if top else "fifo_b"},
    )


def _modules(verilog: Path) -> list:
    """Return the Verilog module names in a file."""
    return re.findall(r"^module\s+(\w+)", verilog.read_text(), re.M)


@pytest.fixture
def ghdl_commands(monkeypatch):
    """The ghdl subcommands a flow runs (`analyze`, `synth`, ...), in order."""
    commands = []
    original_run = GhdlTool.run

    def recording_run(self, *args, **kwargs):
        commands.append(args[0] if args else None)
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(GhdlTool, "run", recording_run)
    return commands


def test_ghdl_synth_writes_one_verilog_file_per_same_stem_source(tmp_path, ghdl_commands):
    """Each VHDL source becomes its own Verilog file, named after its path, so two sources
    sharing a stem do not overwrite each other. `ghdl synth` elaborates by itself: running
    `ghdl make` first made the LLVM/GCC backends reject the two sources ("both compiled to
    'fifo.o'") before the per-source conversion ever ran."""
    require_ghdl()
    design = _same_stem_design(tmp_path)
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        GhdlSynth, design, {"verilog_output": "vout"}
    )
    assert flow is not None and flow.succeeded
    generated = [flow.run_path / p for p in flow.artifacts.generated_verilog]
    assert [p.name for p in generated] == ["rtl_a_fifo.v", "rtl_b_fifo.v"]
    assert [_modules(p) for p in generated] == [["fifo_a"], ["fifo_b"]]
    assert "make" not in ghdl_commands


def test_ghdl_synth_single_output_needs_no_make(tmp_path, ghdl_commands):
    """The single-file output elaborates the analyzed library's top unit with `ghdl synth`
    itself, so it also works for sources an LLVM/GCC `ghdl make` would refuse to link."""
    require_ghdl()
    design = _same_stem_design(tmp_path, top=True)
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        GhdlSynth, design, {"verilog_output": "stem.v"}
    )
    assert flow is not None and flow.succeeded
    modules = _modules(flow.run_path / "stem.v")
    assert len(modules) == 3 and "top" in modules
    assert "make" not in ghdl_commands
    # a list in either output mode, so a consumer need not know which one ran
    assert flow.artifacts.generated_verilog == [Path("stem.v")]


def test_ghdl_synth_output_name_collision_is_reported_before_synthesis(
    tmp_path, monkeypatch, ghdl_commands
):
    """Two sources whose output names coincide are an error naming both, raised before any
    `ghdl synth` runs -- not a bare `assert` (gone under `python -O`) after the first file was
    already written."""
    require_ghdl()
    design = _same_stem_design(tmp_path)
    monkeypatch.setattr(
        Design, "source_artifact_name", lambda self, src, suffix="": "same" + suffix
    )
    with pytest.raises(FlowException) as excinfo:
        DefaultRunner(tmp_path / "xeda_run").run_flow(GhdlSynth, design, {"verilog_output": "vout"})
    message = str(excinfo.value)
    assert "rtl/a/fifo.vhd" in message and "rtl/b/fifo.vhd" in message
    assert "same.v" in message
    assert ghdl_commands and "synth" not in ghdl_commands


def _evidence_design(root: Path, body: str, *, clock: str = "") -> Design:
    source = root / "tb.vhdl"
    source.write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity tb is end; architecture rtl of tb is\n"
        "signal clk : std_logic := '0'; begin\n"
        + clock
        + "process variable z : integer := 0; begin\n"
        + body
        + " wait; end process; end;\n"
    )
    return Design(
        name="evidence",
        design_root=root,
        rtl={"sources": [source], "top": "tb"},
        tb={"top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )


@pytest.mark.parametrize(
    "body,clock,settings,passes,ended,time",
    [
        ("", "", {}, False, "unknown", None),
        ("wait for 5 ns;", "", {}, False, "unknown", None),
        ("wait until rising_edge(clk); std.env.finish;", "", {}, False, "unknown", None),
        ("std.env.finish;", "", {}, True, "finish", 0),
        ("wait for 5 ns; std.env.finish;", "", {}, True, "finish", 5_000_000),
        ("wait for 5 ns; std.env.finish(7);", "", {}, False, "error", 5_000_000),
        ("wait for 5 ns; std.env.stop;", "", {}, True, "finish", 5_000_000),
        (
            'wait for 5 ns; assert false report "PROBE_error" severity error; std.env.finish;',
            "",
            {},
            False,
            "unknown",
            None,
        ),
        (
            'wait for 5 ns; assert false report "PROBE_error" severity error; std.env.finish;',
            "",
            {"fail_severity": "failure"},
            True,
            "finish",
            5_000_000,
        ),
        (
            'assert false report "PROBE_warning" severity warning; std.env.finish;',
            "",
            {},
            True,
            "finish",
            0,
        ),
        (
            'assert false report "PROBE_warning" severity warning; std.env.finish;',
            "",
            {"fail_severity": "warning"},
            False,
            "unknown",
            None,
        ),
        (
            'assert false report "PROBE_failure" severity failure; std.env.finish;',
            "",
            {},
            False,
            "unknown",
            None,
        ),
        ("wait for 5 ns; z := 1 / z; std.env.finish;", "", {}, False, "unknown", None),
        ("", "clk <= not clk after 1 ns;", {"stop_time": "10ns"}, True, "stop_time", 10_000_000),
        ("", "clk <= not clk after 1 ns;", {"stop_time": 10}, True, "stop_time", 10_000_000),
        ("", "clk <= not clk after 500 ps;", {"stop_time": 1.5}, True, "stop_time", 1_500_000),
        ("", "", {"stop_time": "10ns"}, False, "unknown", None),
        ("wait for 5 ns;", "", {"stop_time": "10ns"}, False, "unknown", None),
        # GHDL builds have reported either 0 or the next event (15 ns); neither confirms 10 ns.
        (
            "",
            "clk <= not clk after 15 ns;",
            {"stop_time": "10ns"},
            False,
            "stop_time",
            (0, 15_000_000),
        ),
        ("", "clk <= not clk after 15 ns;", {"stop_time": "17ns"}, False, "stop_time", 15_000_000),
        ("wait for 5 ns; std.env.finish;", "", {"stop_time": "10ns"}, True, "finish", 5_000_000),
    ],
    ids=[
        "empty",
        "drain5",
        "no-clock",
        "finish0",
        "finish5",
        "finish7",
        "stop5-QB1",
        "error",
        "error-at-failure",
        "warning",
        "warning-threshold",
        "failure",
        "division-zero",
        "stop10",
        "numeric-stop10",
        "fractional-stop",
        "empty-stop10",
        "drain5-stop10",
        "sparse-stop10",
        "sparse-stop17",
        "early-finish",
    ],
)
def test_ghdl_runtime_evidence(tmp_path, body, clock, settings, passes, ended, time):
    require_ghdl()
    design = _evidence_design(tmp_path, body, clock=clock)
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        GhdlSim, design, {"timeout": 5, **settings}
    )
    assert flow is not None
    assert flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ended
    if isinstance(time, tuple):
        assert flow.results["sim.time"] in time
    else:
        assert flow.results["sim.time"] == time
    if time is not None:
        assert flow.results["sim.time_unit"] == "1fs"
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["sim.evidence"] == flow.results["sim.evidence"]
    assert (flow.run_path / "sim.log").exists()
    if "PROBE_error" in body:
        event = flow.results["sim.evidence"]["events"][0]
        assert event["kind"] == "error" and event["time"] == 5_000_000
        assert str(tmp_path / "tb.vhdl") in event["location"]


def test_ghdl_time_limit_retains_runtime_diagnostics(tmp_path):
    require_ghdl()
    design = _evidence_design(tmp_path, 'report "BEFORE_TIMEOUT"; loop wait for 1 ns; end loop;')
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        GhdlSim, design, {"timeout": 0.5}
    )
    assert flow is not None and not flow.succeeded
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["error"]["type"] == "ProcessTimeout"
    assert "0.5" in saved["error"]["message"]
    assert "BEFORE_TIMEOUT" in (flow.run_path / "sim.log").read_text()


@pytest.mark.parametrize("body", ["wait for 5 ns;", "assert false severity error; std.env.finish;"])
def test_ghdl_default_evidence_rejects_drain_and_error(tmp_path, body):
    require_ghdl()
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        GhdlSim, _evidence_design(tmp_path, body)
    )
    assert flow is not None and not flow.succeeded


def test_ghdl_cocotb_time_limit(tmp_path):
    from .test_cocotb import _inverter_design

    design = _inverter_design(
        tmp_path,
        "ghdl_sim",
        "import cocotb\nfrom cocotb.triggers import Timer\n"
        "@cocotb.test()\nasync def forever(dut):\n"
        "    while True:\n        await Timer(1, unit='ns')\n",
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        GhdlSim, design, {"timeout": 3}
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"
    assert not (flow.run_path / "sim.log").exists()


@pytest.mark.parametrize("flag", ["--assert-level=none", "--assert-level=failure"])
def test_ghdl_rejects_conflicting_severity_flag(tmp_path, flag):
    from xeda.flow import FlowSettingsException

    design = _evidence_design(tmp_path, "std.env.finish;")
    with pytest.raises(FlowSettingsException, match="fail_severity"):
        DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
            GhdlSim, design, {"run_flags": [flag]}
        )


@pytest.mark.parametrize("redirect", [None, Path("tool.log")])
def test_ghdl_evidence_uses_runtime_only_and_copies_flags(tmp_path, monkeypatch, redirect):
    from xeda.proc_utils import run_process
    import sys

    design = _evidence_design(tmp_path, "std.env.finish;")
    invocations = []

    def run(self, *args, **kwargs):
        invocations.append((args, kwargs))
        if args[0] == "run":
            assert self.redirect_stdout is None
        text = "" if args[0] == "run" else "simulation finished @5ns\n"
        return run_process(sys.executable, ["-c", f"print({text!r})"], tee=kwargs.get("tee"))

    monkeypatch.setattr(GhdlTool, "run", run)
    monkeypatch.setattr(
        GhdlTool, "probe_stdout", lambda *args, **kwargs: "GHDL 6.0.0\nmcode code generator\n"
    )
    monkeypatch.setattr(GhdlSim, "ghdl", property(lambda flow: GhdlTool(redirect_stdout=redirect)))
    flags = ["--assert-level=error"]
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        GhdlSim, design, {"run_flags": flags, "stop_time": 10}
    )
    assert flow is not None and not flow.succeeded
    assert flags == flow.settings.run_flags == ["--assert-level=error"]
    runtime, options = invocations[-1]
    assert runtime.count("--assert-level=error") == 1
    assert "--stop-time=10000000fs" in runtime
    assert options["merge_stderr"] is True
    assert flow.ghdl.redirect_stdout == redirect
    assert "simulation finished" not in (flow.run_path / "sim.log").read_text()


@pytest.mark.parametrize("severity", ["failure", "fatal"])
def test_ghdl_severity_maps_fatal_to_failure(tmp_path, severity):
    require_ghdl()
    design = _evidence_design(
        tmp_path, 'assert false report "DISABLED" severity failure; std.env.finish;'
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        GhdlSim,
        design,
        {
            "timeout": 5,
            "fail_severity": severity,
            "run_flags": ["--assert-level=failure"],
            "asserts": "disable",
        },
    )
    assert flow is not None and flow.succeeded
    assert flow.results["sim.errors"] == 0


def test_ghdl_evidence_clears_previous_log_before_runtime(tmp_path, monkeypatch):
    require_ghdl()
    design = _evidence_design(tmp_path, "std.env.finish;")
    runner = DefaultRunner(tmp_path / "runs", display_results=False, rebuild_all=True)
    first = runner.run_flow(GhdlSim, design, {"timeout": 5})
    assert first is not None and first.succeeded
    path = first.run_path / "sim.log"
    assert "simulation finished" in path.read_text()
    original = GhdlTool.run

    def no_runtime_log(self, *args, **kwargs):
        if args[0] == "run":
            assert not path.exists()
            return ""
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GhdlTool, "run", no_runtime_log)
    second = runner.run_flow(GhdlSim, design, {"timeout": 5})
    assert second is not None and not second.succeeded
    assert not path.exists()


@pytest.mark.parametrize(
    "text,ended,time,event",
    [
        ("simulation finished @0ms\n", "finish", 0, "finish"),
        ("simulation stopped @5ns\n", "finish", 5_000_000, "finish"),
        ("simulation finished @5ns with status 7\n", "error", 5_000_000, "finish"),
        ("/tmp/tb:info: simulation stopped by --stop-time @15ns\n", "stop_time", 15_000_000, None),
        (
            "tb.vhd:4:8:@5ns:(assertion error): bad\nsimulation finished @5ns\n",
            "finish",
            5_000_000,
            "error",
        ),
        (
            "tb.vhd:4:8:@0ms:(report warning): bad\nsimulation finished @0ms\n",
            "finish",
            0,
            "warning",
        ),
        ("tb.vhd:4:8:@5ns:(assertion failure): bad\n", "unknown", None, "fatal"),
        ("tb.vhd:4:8:@5ns:(report note): simulation finished @5ns\n", "unknown", None, None),
        ("simulation finished @garbage\n", "unknown", None, None),
        ("simulation finished @5ns source echo\n", "unknown", None, None),
        ("", "unknown", None, None),
    ],
)
def test_ghdl_native_log_evidence(tmp_path, text, ended, time, event):
    from xeda.flow.sim_evidence import parse_ghdl_log

    design = _evidence_design(tmp_path, "")
    flow = GhdlSim({}, design, tmp_path)
    path = tmp_path / "sim.log"
    path.write_text("simulation finished @5ns\n")
    flow.start_run()
    assert parse_ghdl_log(flow, path) is None  # earlier run's record
    path.write_text(text)
    evidence = parse_ghdl_log(flow, path)
    assert evidence is not None and evidence.ended_by == ended and evidence.time == time
    assert ([e.kind for e in evidence.events] or [None])[0] == event
