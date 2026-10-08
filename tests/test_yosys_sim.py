"""Real CXXRTL smoke test and translation of CXXRTL backend options."""

from pathlib import Path
import json

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows.yosys.cxx_rtl import YosysSim

from .tool_utils import require_c_toolchain, require_yosys


def _design_with_a_source(root):
    """A design a `YosysSim` can be built on, for a test of its settings: the flow needs a source
    it reads."""
    (root / "d.v").write_text("module d; endmodule\n")
    return Design(name="d", design_root=root, rtl={"sources": ["d.v"], "top": "d"})


def _driver_design(tmp_path, body="return 0;", *, rtl=None, header=True):
    require_yosys()
    require_c_toolchain()
    (tmp_path / "dut.sv").write_text(
        rtl or "module dut(input a, output y); assign y = a; endmodule\n"
    )
    (tmp_path / "main.cpp").write_text(
        "#include <cassert>\n#include <cstdio>\n#include <cstdlib>\n"
        + (
            '#include "model.h"\n'
            if header
            else "#include <cxxrtl/capi/cxxrtl_capi.h>\n"
            'extern "C" cxxrtl_toplevel cxxrtl_design_create();\n'
        )
        + "int main(int argc, char **argv) { "
        + body
        + " }\n"
    )
    return Design(
        name="driver",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["main.cpp"]},
    )


def _run_driver(tmp_path, body="return 0;", *, settings=None, rtl=None, header=True):
    design = _driver_design(tmp_path, body, rtl=rtl, header=header)
    options = {"timeout": 10, "cxxrtl": {"filename": "model.cpp", "header": header}}
    options.update(settings or {})
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        YosysSim, design, options
    )
    assert flow is not None
    return flow


@pytest.mark.parametrize("header", [True, False])
@pytest.mark.parametrize(
    "body", ["return 0;", 'std::puts("ERROR: arbitrary user output"); return 0;']
)
def test_user_driver_exit_has_execution_evidence(tmp_path, header, body):
    flow = _run_driver(tmp_path, body, header=header)
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "exit"
    assert flow.results["sim.evidence"]["exit_code"] == 0
    assert flow.results["sim.time"] is None
    assert flow.results["sim.errors"] == 0
    record = flow.run_path / "cxxrtl_end.json"
    assert record.is_file() and json.loads(record.read_text())["ended_by"] == "exit"


@pytest.mark.parametrize("body", ["return 3;", "std::abort();", "assert(false);", "std::_Exit(0);"])
def test_driver_abnormal_or_unobserved_exit_fails(tmp_path, body):
    flow = _run_driver(tmp_path, body)
    assert not flow.succeeded
    if body == "return 3;":
        assert flow.results["sim.evidence"]["exit_code"] == 3


def test_driver_timeout_preserves_output_and_names_limit(tmp_path):
    flow = _run_driver(
        tmp_path, 'std::puts("driver started"); while (true) {}', settings={"timeout": 0.5}
    )
    assert not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"
    assert "0.5" in flow.results["error"]["message"]
    assert "driver started" in (flow.run_path / "cxxrtl_sim.log").read_text()
    assert not (flow.run_path / "cxxrtl_end.json").exists()


@pytest.mark.parametrize("stop", [0, 10, "10ns"])
def test_driver_stop_time_is_rejected_before_compilation(tmp_path, monkeypatch, stop):
    from xeda.flow import FlowSettingsException
    from xeda.flows.yosys.common import YosysBase

    called = []
    monkeypatch.setattr(YosysBase, "init", lambda self: called.append(True))
    flow = YosysSim(YosysSim.Settings(stop_time=stop), _design_with_a_source(tmp_path), tmp_path)
    with pytest.raises(FlowSettingsException, match="stop_time.*driver"):
        flow.init()
    assert not called


def test_driver_settings_keep_the_simflow_diamond(tmp_path):
    from xeda.flow import SimFlow

    settings = YosysSim.Settings(timeout=2, fail_severity="failure", vcd=True)
    assert isinstance(settings, SimFlow.Settings)
    assert settings.vcd == "dump.vcd"
    assert settings.timeout == 2 and settings.fail_severity == "failure"


@pytest.mark.parametrize(
    "severity,passes", [("warning", False), ("error", False), ("failure", True), ("fatal", True)]
)
@pytest.mark.parametrize("ndebug", [False, True])
@pytest.mark.parametrize("header", [True, False])
def test_generated_rtl_assertion_evidence_uses_threshold(
    tmp_path, severity, passes, ndebug, header
):
    flags = ["-DNDEBUG", "-DCXXRTL_NDEBUG"] if ndebug else []
    if not header:
        flags.append("-DCXXRTL_INCLUDE_CAPI_IMPL")
    flow = _run_driver(
        tmp_path,
        (
            "cxxrtl_design::p_dut top; top.p_a.set<bool>(false); top.step(); return 0;"
            if header
            else "auto top = cxxrtl_create(cxxrtl_design_create()); "
            "cxxrtl_step(top); cxxrtl_destroy(top); return 0;"
        ),
        header=header,
        rtl="module dut(input a); always @* assert(a); endmodule\n",
        settings={
            "read_verilog_flags": ["-formal"],
            "fail_severity": severity,
            "cxxrtl": {
                "filename": "model.cpp",
                "header": header,
                "ccflags": flags,
            },
        },
    )
    assert flow.succeeded == passes
    assert flow.results["sim.errors"] >= 1
    event = flow.results["sim.evidence"]["events"][0]
    assert event["kind"] == "error" and "model" in event["location"]


def test_cxxrtl_native_monitor_capability_probe():
    from .tool_utils import require_cxxrtl_evidence

    require_cxxrtl_evidence()


def test_frontend_finish_error_is_not_driver_evidence(tmp_path):
    flow = _run_driver(tmp_path, rtl="module dut; initial $finish; endmodule\n")
    assert not flow.succeeded
    assert "System task `$finish' executed" in (flow.run_path / "yosys.log").read_text()
    assert "sim.evidence" not in flow.results


@pytest.mark.parametrize(
    "record",
    [
        None,
        "malformed",
        '{"ended_by":"finish","time":0}',
        '{"ended_by":"exit","time":1,"time_unit":"1ns"}',
        '{"ended_by":"exit","events":[],"exit_code":0}',
    ],
)
def test_driver_missing_malformed_or_stale_monitor_fails(tmp_path, monkeypatch, record):
    from xeda.tool import Tool
    from xeda.proc_utils import run_process
    import sys

    design = _driver_design(tmp_path)
    original = Tool.execute
    reached = []

    def execute(tool, executable, *args, **kwargs):
        if Path(executable).name != "model":
            return original(tool, executable, *args, **kwargs)
        reached.append(True)
        program = "from pathlib import Path\nPath('runtime_marker').write_text('executed')\n"
        if record is not None and "exit_code" not in record:
            program += f"Path('cxxrtl_end.json').write_text({record!r})\n"
            program += "Path('cxxrtl_events.jsonl').write_text('')\n"
        return run_process(sys.executable, ["-c", program], timeout=10)

    monkeypatch.setattr(Tool, "execute", execute)
    if record and "exit_code" in record:
        # Seed before Flow.start_run; the successful runtime leaves it untouched.
        run_path = tmp_path / "runs" / "driver" / "yosys_sim"
        run_path.mkdir(parents=True)
        (tmp_path / "runs" / ".xeda-run-root").touch()
        (run_path / "cxxrtl_end.json").write_text(record)
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        YosysSim, design, {"cxxrtl": {"filename": "model.cpp"}}
    )
    assert reached and flow is not None and not flow.succeeded


@pytest.mark.parametrize("events", [None, "partial", '{"kind":"unexpected"}\n'])
def test_driver_missing_or_invalid_events_fail(tmp_path, monkeypatch, events):
    from xeda.proc_utils import run_process
    from xeda.tool import Tool
    import sys

    design = _driver_design(tmp_path)
    original = Tool.execute

    def execute(tool, executable, *args, **kwargs):
        if Path(executable).name != "model":
            return original(tool, executable, *args, **kwargs)
        program = "from pathlib import Path\n"
        program += 'Path(\'cxxrtl_end.json\').write_text(\'{"ended_by":"exit","events":[]}\')\n'
        if events is not None:
            program += f"Path('cxxrtl_events.jsonl').write_text({events!r})\n"
        return run_process(sys.executable, ["-c", program], timeout=10)

    monkeypatch.setattr(Tool, "execute", execute)
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        YosysSim, design, {"cxxrtl": {"filename": "model.cpp"}}
    )
    assert flow is not None and not flow.succeeded


def test_driver_monitor_paths_use_the_execution_environment(tmp_path, monkeypatch):
    from xeda.proc_utils import run_process
    from xeda.tool import Tool
    import sys

    design = _driver_design(tmp_path)
    compiled = []

    def execute(tool, executable, *args, **kwargs):
        words = [str(a) for a in args]
        name = Path(executable).name
        if name == "sh":
            assert words[-3:] == ["yosys", "yosys-config", "yosys"]
            return "/container/yosys/bin/yosys-config"
        if name == "yosys-config":
            return "/container/yosys/include"
        if name == "g++":
            compiled.append(words)
            assert "-I/container/yosys/include/backends/cxxrtl/runtime" in words
            assert "cxxrtl_evidence.cpp" in words and "cxxrtl_evidence.h" in words
            assert all(
                not Path(w).is_absolute() for w in ("cxxrtl_evidence.cpp", "cxxrtl_evidence.h")
            )
        if name == "model":
            assert kwargs["env"]["XEDA_CXXRTL_END_RECORD"] == "cxxrtl_end.json"
            assert kwargs["env"]["XEDA_CXXRTL_EVENTS"] == "cxxrtl_events.jsonl"
            assert kwargs["timeout"] == 4 and kwargs["merge_stderr"]
            return run_process(
                sys.executable,
                [
                    "-c",
                    "from pathlib import Path; Path('cxxrtl_end.json').write_text('{\"ended_by\":\"exit\",\"events\":[]}'); Path('cxxrtl_events.jsonl').write_text('')",
                ],
                timeout=10,
            )
        return ""

    monkeypatch.setattr(Tool, "execute", execute)
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        YosysSim, design, {"dockerized": True, "timeout": 4, "cxxrtl": {"filename": "model.cpp"}}
    )
    assert compiled and flow is not None and flow.succeeded


def test_yosys_sim_runs_the_cxxrtl_example(tmp_path):
    require_yosys()
    require_c_toolchain()
    example = Path(__file__).parent.parent / "examples/mixed_language/blink/blinky.xeda.yaml"
    design = Design.from_file(example)
    flow = DefaultRunner(tmp_path).run_flow(YosysSim, design, {"cxxrtl": {"filename": None}})
    assert flow is not None and flow.succeeded
    assert (flow.run_path / "blink.cpp").is_file()
    assert (flow.run_path / "blink.h").is_file()
    assert (flow.run_path / "blink").is_file()
    assert flow.artifacts["cxxrtl_cpp"] == Path("blink.cpp")
    assert flow.settings.cxxrtl.filename is None
    assert "netlist_json" not in flow.artifacts


def test_cxxrtl_backend_settings_are_rendered(tmp_path):
    design = _design_with_a_source(tmp_path)
    settings = YosysSim.Settings(
        cxxrtl={
            "filename": "sim.cpp",
            "header": True,
            "flatten": False,
            "hierarchy": False,
            "proc": False,
            "debug": 1,
            "opt": 3,
            "namespace": "sim",
        }
    )
    flow = YosysSim(settings, design, tmp_path)
    flow.init()
    script = flow.copy_from_template(
        "yosys_sim.ys",
        ghdl_args=[],
        parameters={},
        defines=[],
        cxxrtl_filename=settings.cxxrtl.filename,
        lstrip_blocks=True,
        trim_blocks=False,
    )
    text = (tmp_path / script).read_text()
    assert (
        "write_cxxrtl -header -noflatten -nohierarchy -noproc -g1 -O3 -namespace sim sim.cpp"
        in text
    )


def test_flatten_is_a_separate_command_in_both_script_formats(tmp_path):
    design = _design_with_a_source(tmp_path)
    flow = YosysSim(YosysSim.Settings(flatten=True), design, tmp_path)
    flow.init()
    for template, command in (
        ("yosys_sim.ys", "flatten"),
        ("yosys_sim.tcl", "yosys flatten"),
    ):
        script = flow.copy_from_template(
            template,
            ghdl_args=[],
            parameters={},
            defines=[],
            cxxrtl_filename="d.cpp",
            lstrip_blocks=True,
            trim_blocks=False,
        )
        lines = [line.strip() for line in (tmp_path / script).read_text().splitlines()]
        assert command in lines
        assert any(line.startswith(command.replace("flatten", "check")) for line in lines)
        assert f"{command.replace('flatten', 'check')} -initdrv -assert" in lines
        assert any(line.endswith(" d.cpp") and "write_cxxrtl" in line for line in lines)


@pytest.mark.parametrize("check_assert", [True, False])
def test_invalid_init_driver_fails_before_cxxrtl(tmp_path, check_assert):
    require_yosys()
    (tmp_path / "bad.v").write_text(
        "module top(input a, output b); "
        "(* init = 1'b0 *) wire w; assign w = a; assign b = w; endmodule\n"
    )
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    design = Design(
        name="bad_init_driver",
        design_root=tmp_path,
        rtl={"sources": ["bad.v"], "top": "top"},
        tb={"sources": ["main.cpp"]},
    )
    flow = DefaultRunner(tmp_path / "runs").run_flow(
        YosysSim, design, {"check_assert": check_assert, "cxxrtl": {"filename": "sim.cpp"}}
    )
    assert flow is not None and not flow.succeeded
    log = (flow.run_path / "yosys.log").read_text()
    assert "has 'init' attribute and is not driven by an FF cell" in log
    assert "ERROR: Found 1 problems in 'check -assert'" in log
    assert not (flow.run_path / "sim.cpp").exists()


def test_simulation_top_and_hdl_testbench_source_are_used(tmp_path, capfd):
    require_yosys()
    require_c_toolchain()
    (tmp_path / "dut.v").write_text("module dut(input a, output y); assign y = a; endmodule\n")
    (tmp_path / "sim_top.v").write_text(
        "module sim_top(input a, output y); dut u(.a(a), .y(y)); endmodule\n"
    )
    # The general Yosys include root is needed by older generated CXXRTL models and can also
    # be used by testbenches; the current generated model exercises the newer runtime root.
    (tmp_path / "sim_main.cpp").write_text(
        '#include <libs/sha1/sha1.h>\n#include "model.h"\n'
        "int main() { cxxrtl_design::p_sim__top top; return 0; }\n"
    )
    design = Design(
        name="sim_top_example",
        design_root=tmp_path,
        rtl={"sources": ["dut.v"], "top": "dut"},
        tb={"sources": ["sim_top.v", "sim_main.cpp"], "top": "sim_top"},
    )
    flow = DefaultRunner(tmp_path / "runs").run_flow(
        YosysSim, design, {"log_file": None, "cxxrtl": {"filename": "model.cpp"}}
    )
    assert flow is not None and flow.succeeded
    assert "struct p_sim__top : public module" in (flow.run_path / "model.h").read_text()
    assert "Executing CHECK pass" in capfd.readouterr().out
    assert not (flow.run_path / "yosys.log").exists()


def test_a_testbench_source_the_flow_cannot_read_is_refused_when_planned(tmp_path):
    """The testbench is read as the RTL is: a Bluespec testbench beside SystemVerilog RTL would
    be passed over, and the model would be built and run without it."""
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "Tb.bsv").write_text("package Tb; endpackage\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["Tb.bsv"]},
    )
    with pytest.raises(FlowSettingsException, match=r"yosys_sim cannot read .* Bluespec source"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan("yosys_sim", design)


def test_a_cpp_driver_beside_the_rtl_is_planned(tmp_path):
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["main.cpp"]},
    )
    planned = DefaultRunner(tmp_path / "xeda_run", display_results=False).plan("yosys_sim", design)
    assert [node.name for node in planned.nodes] == ["yosys_sim"]
