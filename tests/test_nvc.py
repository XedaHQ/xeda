#!/usr/bin/env python3
import json
from pathlib import Path

import pytest

from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nvc
from xeda.flows.nvc import NvcTool

from .test_ghdl import _evidence_design
from .tool_utils import require_nvc_evidence

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_nvc_sim_py(tmp_path: Path) -> None:
    require_nvc_evidence()
    design_paths = [
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium-dc.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "pipeline" / "pipelined_adder.yaml",
    ]
    run_dir = tmp_path / "xeda_run"
    for design in design_paths:
        xeda_runner = DefaultRunner(run_dir, debug=debug)
        flow = xeda_runner.run(Nvc, design, flow_overrides=dict(debug=debug, verbose=debug))
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert flow.succeeded
        assert isinstance(flow.settings, Nvc.Settings)
        assert results_json.exists()


@pytest.mark.parametrize("one_shot", [True, False], ids=["combined", "separate"])
@pytest.mark.parametrize("messages", [None, "compact"], ids=["full", "compact"])
@pytest.mark.parametrize(
    "body,clock,settings,passes,ended,time",
    [
        ("", "", {}, False, "drained", 0),
        ("wait for 5 ns;", "", {}, False, "drained", 5_000_000),
        ("wait until rising_edge(clk); std.env.finish;", "", {}, False, "drained", 0),
        ("std.env.finish;", "", {}, True, "finish", 0),
        ("wait for 5 ns; std.env.finish;", "", {}, True, "finish", 5_000_000),
        ("wait for 5 ns; std.env.finish(7);", "", {}, False, "error", 5_000_000),
        ("wait for 5 ns; std.env.stop;", "", {}, True, "finish", 5_000_000),
        (
            'wait for 5 ns; assert false report "PROBE_error" severity error; std.env.finish;',
            "",
            {},
            False,
            "error",
            5_000_000,
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
            "drained",
            0,
        ),
        (
            'assert false report "PROBE_failure" severity failure; std.env.finish;',
            "",
            {},
            False,
            "fatal",
            0,
        ),
        ("wait for 5 ns; z := 1 / z; std.env.finish;", "", {}, False, "fatal", 5_000_000),
        ("", "clk <= not clk after 1 ns;", {"stop_time": "10ns"}, True, "stop_time", 10_000_000),
        ("", "clk <= not clk after 1 ns;", {"stop_time": 10}, True, "stop_time", 10_000_000),
        ("", "clk <= not clk after 500 ps;", {"stop_time": 1.5}, True, "stop_time", 1_500_000),
        ("", "", {"stop_time": "10ns"}, False, "drained", 0),
        ("wait for 5 ns;", "", {"stop_time": "10ns"}, False, "drained", 5_000_000),
        ("wait for 5 ns;", "", {"stop_time": "5ns"}, False, "drained", 5_000_000),
        ("wait for 5 ns; std.env.stop;", "", {"stop_time": "5ns"}, True, "finish", 5_000_000),
        ("", "clk <= not clk after 1 ns;", {"stop_time": "5ns"}, True, "stop_time", 5_000_000),
        ("", "clk <= not clk after 15 ns;", {"stop_time": "10ns"}, False, "stop_time", 0),
        ("", "clk <= not clk after 15 ns;", {"stop_time": "17ns"}, False, "stop_time", 15_000_000),
        ("wait for 5 ns; std.env.finish;", "", {"stop_time": "10ns"}, True, "finish", 5_000_000),
        ('report "FINISH called";', "", {}, False, "drained", 0),
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
        "drain-at-bound",
        "stop-at-bound-QB1",
        "clock-at-bound",
        "sparse-stop10",
        "sparse-stop17",
        "early-finish",
        "finish-message",
    ],
)
def test_nvc_runtime_evidence(
    tmp_path, one_shot, messages, body, clock, settings, passes, ended, time
):
    require_nvc_evidence()
    design = _evidence_design(tmp_path, body, clock=clock)
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc, design, {"one_shot": one_shot, "messages": messages, "timeout": 5, **settings}
    )
    assert flow is not None and flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ended
    assert flow.results["sim.time"] == time
    assert flow.results["sim.time_unit"] == "1fs"
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["sim.evidence"] == flow.results["sim.evidence"]
    assert flow.settings.messages == messages
    assert (flow.run_path / "nvc_end.json").is_file()
    if "PROBE_error" in body:
        event = flow.results["sim.evidence"]["events"][0]
        assert event["kind"] == "error" and event["time"] == 5_000_000
        assert "tb.vhdl" in event["location"]


@pytest.mark.parametrize("one_shot", [True, False])
def test_nvc_time_limit_retains_runtime_diagnostics(tmp_path, one_shot):
    require_nvc_evidence()
    design = _evidence_design(tmp_path, 'report "BEFORE_TIMEOUT"; loop wait for 1 ns; end loop;')
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc, design, {"one_shot": one_shot, "timeout": 1}
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"
    assert "BEFORE_TIMEOUT" in (flow.run_path / "sim.log").read_text()


@pytest.mark.parametrize(
    "body", ["std.env.finish;", "wait for 5 ns;", "assert false severity error; std.env.finish;"]
)
def test_nvc_default_evidence_captures_stderr_and_rejects_drain_and_error(tmp_path, body):
    require_nvc_evidence()
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc, _evidence_design(tmp_path, body)
    )
    assert flow is not None and flow.succeeded is (body == "std.env.finish;")
    assert (flow.run_path / "sim.log").is_file()


@pytest.mark.parametrize("value", [None, "note", "error", "failure"])
def test_nvc_exit_severity_is_removed_with_migration(value):
    with pytest.raises(Exception, match="exit_severity.*removed.*fail_severity"):
        Nvc.Settings(exit_severity=value)
    ss = Nvc.Settings()
    with pytest.raises(Exception, match="exit_severity.*removed.*fail_severity"):
        ss.exit_severity = value


@pytest.mark.parametrize(
    "flag",
    [
        "--exit-severity=note",
        "--exit-severity=failure",
        "--exit-severity",
        "--stop-time=5ns",
        "--stop-time",
    ],
)
def test_nvc_rejects_conflicting_runtime_flags(tmp_path, flag):
    with pytest.raises(FlowSettingsException, match="fail_severity|stop_time"):
        DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
            Nvc, _evidence_design(tmp_path, "std.env.finish;"), {"run_flags": [flag]}
        )


@pytest.mark.parametrize("severity", ["failure", "fatal"])
def test_nvc_severity_maps_fatal_to_failure_and_copies_flags(tmp_path, severity):
    require_nvc_evidence()
    flags = ["--exit-severity=failure"]
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc,
        _evidence_design(tmp_path, "assert false severity error; std.env.finish;"),
        {"fail_severity": severity, "run_flags": flags, "analysis_flags": [], "elab_flags": []},
    )
    assert flow is not None and flow.succeeded
    assert flow.results["sim.errors"] == 1
    assert flags == flow.settings.run_flags == ["--exit-severity=failure"]
    assert flow.settings.analysis_flags == flow.settings.elab_flags == []


@pytest.mark.parametrize("one_shot", [True, False])
def test_nvc_cocotb_time_limit(tmp_path, one_shot):
    from .test_cocotb import _inverter_design

    design = _inverter_design(
        tmp_path,
        "nvc",
        "import cocotb\nfrom cocotb.triggers import Timer\n@cocotb.test()\nasync def forever(dut):\n    while True:\n        await Timer(1, unit='ns')\n",
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc, design, {"timeout": 3, "one_shot": one_shot}
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"
    assert not (flow.run_path / "sim.log").exists()
    assert not (flow.run_path / "nvc_end.cpp").exists()


@pytest.mark.parametrize(
    "text,ended,time,kinds",
    [
        (
            "** Note: 0ms+0: FINISH called\n   Procedure FINISH [] at lib/std.08/env-body.vhd:42\n",
            "finish",
            0,
            ["finish"],
        ),
        (
            "lib/std.08/env-body.vhd:32:9: note: 5ns+0: STOP called\n",
            "finish",
            5_000_000,
            ["finish"],
        ),
        (
            "lib/std.08/env-body.vhd:42:9: note: 5ns+0: FINISH called with status 7\n",
            "error",
            5_000_000,
            ["finish"],
        ),
        (
            "tb.vhdl:4:8: error: 5ns+0: bad\nlib/std.08/env-body.vhd:42:9: note: 5ns+0: FINISH called\n",
            "finish",
            5_000_000,
            ["error", "finish"],
        ),
        ("** Warning: 0ms+0: bad\n   Process :tb:p at tb.vhdl:4\n", "unknown", None, ["warning"]),
        (
            "** Fatal: 5ns+0: division by zero\n   Process :tb:p at tb.vhdl:4\n",
            "fatal",
            5_000_000,
            ["fatal"],
        ),
        ("tb.vhdl:4:8: note: 5ns+0: FINISH called\n", "unknown", None, []),
        ("** Note: 5ns+0: FINISH called\n   Process :tb:p at tb.vhdl:4\n", "unknown", None, []),
        ("note: loading plugin FINISH called\n", "unknown", None, []),
        ("lib/std.08/env-body.vhd:42:9: note: garbage: FINISH called\n", "unknown", None, []),
        ("", "unknown", None, []),
    ],
)
def test_nvc_native_log_evidence(tmp_path, text, ended, time, kinds):
    from xeda.flow.sim_evidence import parse_nvc_log

    flow = Nvc({}, _evidence_design(tmp_path, ""), tmp_path)
    path = tmp_path / "sim.log"
    path.write_text(text)
    flow.start_run()
    assert parse_nvc_log(flow, path) is None
    path.write_text("XEDA_NVC_RUNTIME_START\n" + text)
    evidence = parse_nvc_log(flow, path)
    assert evidence is not None and evidence.ended_by == ended and evidence.time == time
    assert [e.kind for e in evidence.events] == kinds


@pytest.mark.parametrize(
    "record,finish,passes",
    [
        (None, False, False),
        (None, True, True),
        ("{", True, False),
        ('{"time": 9, "time_unit": "1fs", "next_time": null}', True, False),
        ('{"time": -1, "time_unit": "1fs", "next_time": null}', False, False),
        ('{"time": 0, "time_unit": "bogus", "next_time": null}', True, False),
        ('{"time": 0, "time_unit": "1fs"}', False, False),
        ('{"time": 5000000, "time_unit": "1fs", "next_time": null}', False, False),
        ('{"time": 5000000, "time_unit": "1fs", "next_time": 6000000}', False, True),
        ('{"time": 5000000, "time_unit": "1fs", "next_time": 5000000}', False, False),
    ],
)
def test_nvc_end_record_evidence_is_required_for_silent_cutoff(tmp_path, record, finish, passes):
    flow = Nvc({"stop_time": "5ns"}, _evidence_design(tmp_path, ""), tmp_path)
    flow._sim_log = tmp_path / "sim.log"
    flow._end_record = tmp_path / "nvc_end.json"
    flow.start_run()
    flow._sim_log.write_text(
        "XEDA_NVC_RUNTIME_START\n"
        + ("lib/std.08/env-body.vhd:42:9: note: 5ns+0: FINISH called\n" if finish else "")
    )
    if record is not None:
        flow._end_record.write_text(record)
    assert flow.check_results() is passes


def test_nvc_evidence_does_not_read_stale_logs_or_records(tmp_path):
    flow = Nvc({"stop_time": "5ns"}, _evidence_design(tmp_path, ""), tmp_path)
    flow._sim_log = tmp_path / "sim.log"
    flow._end_record = tmp_path / "nvc_end.json"
    flow._sim_log.write_text("XEDA_NVC_RUNTIME_START\n")
    flow._end_record.write_text('{"time": 5000000, "time_unit": "1fs", "next_time": 6000000}')
    flow.start_run()
    assert not flow.check_results()
    flow._sim_log.write_text("XEDA_NVC_RUNTIME_START\n")
    assert not flow.check_results()


def test_nvc_plugin_load_failure_is_a_failure(tmp_path):
    require_nvc_evidence()
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc,
        _evidence_design(tmp_path, "std.env.finish;"),
        {"vhpi": [tmp_path / "missing.so"], "timeout": 5},
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["error"]["type"] == "NonZeroExitCode"


@pytest.mark.parametrize("one_shot", [True, False])
@pytest.mark.parametrize("redirect", [None, Path("tool.log")])
def test_nvc_evidence_excludes_build_diagnostics_and_avoids_redirect_conflict(
    tmp_path, monkeypatch, one_shot, redirect
):
    import sys
    from xeda.proc_utils import run_process

    require_nvc_evidence()
    original = NvcTool.run
    invocations = []

    def run(tool, *args, **kwargs):
        if tool.executable == "nvc" and "-r" in args:
            invocations.append((args, kwargs))
            assert tool.redirect_stdout is None
            text = (
                "lib/std.08/env-body.vhd:42:9: note: 0ms+0: FINISH called\nXEDA_NVC_RUNTIME_START\n"
            )
            return run_process(
                sys.executable,
                ["-c", f"import sys; sys.stderr.write({text!r})"],
                tee=kwargs.get("tee"),
                merge_stderr=kwargs.get("merge_stderr", False),
            )
        return original(tool, *args, **kwargs)

    monkeypatch.setattr(NvcTool, "run", run)
    monkeypatch.setattr(Nvc, "nvc", property(lambda flow: NvcTool(redirect_stdout=redirect)))
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc, _evidence_design(tmp_path, "std.env.finish;"), {"one_shot": one_shot, "timeout": 5}
    )
    assert flow is not None and not flow.succeeded
    assert invocations[-1][1]["timeout"] == 5
    assert invocations[-1][1]["merge_stderr"] is True
    assert flow.nvc.redirect_stdout == redirect


def test_nvc_evidence_clears_previous_files_before_runtime(tmp_path, monkeypatch):
    require_nvc_evidence()
    design = _evidence_design(tmp_path, "std.env.finish;")
    runner = DefaultRunner(tmp_path / "runs", display_results=False, rebuild_all=True)
    first = runner.run_flow(Nvc, design, {"timeout": 5})
    assert first is not None and first.succeeded
    original = NvcTool.run

    def no_runtime(tool, *args, **kwargs):
        if tool.executable == "nvc" and "-r" in args:
            assert not (first.run_path / "sim.log").exists()
            assert not (first.run_path / "nvc_end.json").exists()
            return ""
        return original(tool, *args, **kwargs)

    monkeypatch.setattr(NvcTool, "run", no_runtime)
    second = runner.run_flow(Nvc, design, {"timeout": 5})
    assert second is not None and not second.succeeded


@pytest.mark.parametrize("messages", [None, "compact"])
def test_nvc_evidence_captures_stdout_and_preserves_user_vhpi(tmp_path, messages):
    import sys
    from xeda.tool import Tool

    require_nvc_evidence()
    plugin = tmp_path / "user.cpp"
    plugin.write_text(
        '#include <cstdio>\nstatic void start() { std::fprintf(stderr, "USER_VHPI_LOADED\\n"); }\n'
        'extern "C" { void (*vhpi_startup_routines[])() = {start, nullptr}; }\n'
    )
    flags = (
        ["-dynamiclib", "-undefined", "dynamic_lookup"]
        if sys.platform == "darwin"
        else ["-shared", "-fPIC"]
    )
    library = tmp_path / "user.so"
    Tool(executable="c++", print_command=False).run(*flags, plugin, "-o", library, timeout=30)
    libraries = [library]
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc,
        _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;"),
        {"vhpi": libraries, "messages": messages, "std_error": "failure", "timeout": 5},
    )
    assert flow is not None and flow.succeeded
    assert libraries == flow.settings.vhpi == [library]
    transcript = (flow.run_path / "sim.log").read_text()
    assert "USER_VHPI_LOADED" in transcript and "FINISH called" in transcript


def test_nvc_evidence_compiles_against_container_paths(tmp_path, monkeypatch):
    from xeda.run_dir import RunDirectory
    from xeda.tool import Tool
    from .tool_utils import use_fake_tools

    use_fake_tools(monkeypatch)
    run_path = tmp_path / "run"
    run_path.mkdir()
    monkeypatch.chdir(run_path)
    flow = Nvc(
        {"docker": "test/nvc", "dockerized": True},
        _evidence_design(tmp_path, ""),
        run_path,
        run_directory=RunDirectory.claimed(run_path, tmp_path),
    )
    calls = []

    def execute(tool, executable, *args, **kwargs):
        calls.append((tool, executable, args))
        if executable == "sh":
            return "/opt/container/nvc/bin/nvc\n"
        return ""

    monkeypatch.setattr(Tool, "execute", execute)
    monkeypatch.setattr(Tool, "probe_stdout", lambda *args, **kwargs: "nvc 1.23.0")
    assert flow.build_end_monitor() == "./nvc_end.so"
    _, _, args = calls[-1]
    assert args[:2] == ("-shared", "-fPIC")
    assert "/opt/container/nvc/include" in args
    assert not any("homebrew" in str(arg) for arg in args)
    assert calls[-1][0].dockerized


def test_nvc_evidence_probe_rejects_a_silent_plugin(tmp_path, monkeypatch):
    import subprocess
    from . import tool_utils

    require_nvc_evidence()
    original = subprocess.run
    tool_utils._probe_nvc_evidence.cache_clear()

    def run(command, **kwargs):
        if any(str(arg).startswith("--load=") for arg in command):
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    try:
        assert not tool_utils._probe_nvc_evidence()
    finally:
        tool_utils._probe_nvc_evidence.cache_clear()


@pytest.mark.parametrize("one_shot", [True, False])
@pytest.mark.parametrize("messages", [None, "compact"])
def test_nvc_evidence_records_error_below_threshold_before_native_cutoff(
    tmp_path, one_shot, messages
):
    require_nvc_evidence()
    design = _evidence_design(
        tmp_path,
        'wait for 2 ns; assert false report "CONTINUING_ERROR" severity error;',
        clock="clk <= not clk after 1 ns;",
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Nvc,
        design,
        {
            "one_shot": one_shot,
            "messages": messages,
            "fail_severity": "failure",
            "stop_time": "5ns",
            "timeout": 5,
        },
    )
    assert flow is not None and flow.succeeded
    assert flow.results["sim.ended_by"] == "stop_time"
    assert flow.results["sim.errors"] == 1
    assert flow.results["sim.time"] == 5_000_000
