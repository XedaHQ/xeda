"""Launched VCS contracts against synthetic, documentation-based tools; no licensed run."""

import json
import os
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Vcs
from xeda.tool import Tool

from .sim_evidence_cases import use_fake_vcs


def launch(tmp_path, monkeypatch, state="silent", one_shot=False, settings=None, vhdl=False):
    use_fake_vcs(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_VCS_STATE", state)
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    source = root / ("tb.vhd" if vhdl else "tb.sv")
    source.write_text(
        "entity tb is end; architecture rtl of tb is begin end;\n"
        if vhdl
        else "module tb; endmodule\n"
    )
    design = Design(name="tb", design_root=root, rtl={"sources": [source]}, tb={"top": "tb"})
    return DefaultRunner(tmp_path / "run", rebuild_all=True).run_flow(
        Vcs, design, {"one_shot_run": one_shot, **(settings or {})}
    )


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize(
    "state,settings,passes,ending,errors,warnings",
    [
        ("silent", {}, False, "unknown", 0, 0),
        ("finish0", {}, False, "unknown", 0, 0),
        ("finish5", {}, True, "finish", 0, 0),
        ("vhdl_finish", {}, False, "unknown", 0, 0),
        ("vhdl_stop", {}, False, "unknown", 0, 0),
        ("drain5", {}, False, "unknown", 0, 0),
        ("lookalike", {}, False, "unknown", 0, 0),
        ("error_finish", {}, False, "finish", 1, 0),
        ("error_finish", {"fail_severity": "failure"}, True, "finish", 1, 0),
        ("warning_finish", {}, True, "finish", 0, 1),
        ("warning_finish", {"fail_severity": "warning"}, False, "finish", 0, 1),
        ("rt_warning", {}, True, "finish", 0, 1),
        ("rt_warning", {"fail_severity": "warning"}, False, "finish", 0, 1),
        ("assertion", {}, False, "finish", 1, 0),
        ("error_stderr", {}, False, "finish", 1, 0),
        ("fatal", {}, False, "fatal", 1, 0),
        ("verilog_stop", {"fail_severity": "fatal"}, False, "error", 1, 0),
        ("vhdl_error", {}, False, "unknown", 1, 0),
        ("vhdl_failure", {}, False, "fatal", 1, 0),
        ("limit10", {"stop_time": "10ns"}, True, "stop_time", 0, 0),
        ("break10", {"stop_time": "10ns"}, False, "unknown", 0, 0),
        ("drain10", {"stop_time": "10ns"}, False, "unknown", 0, 0),
        ("drain5", {"stop_time": "10ns"}, False, "unknown", 0, 0),
        ("finish5", {"stop_time": "10ns"}, True, "finish", 0, 0),
        ("finish_nonzero", {}, False, "finish", 0, 0),
    ],
)
def test_vcs_runtime_verdict(
    tmp_path, monkeypatch, one_shot, state, settings, passes, ending, errors, warnings
):
    flow = launch(tmp_path, monkeypatch, state, one_shot, settings, vhdl=state.startswith("vhdl"))
    assert flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ending
    assert flow.results["sim.errors"] == errors
    assert flow.results["sim.warnings"] == warnings
    assert (flow.run_path / "fake_vcs.runtime").read_text() == "runtime executed"
    calls = [
        json.loads(line)
        for line in (flow.run_path / "fake_vcs.invocations").read_text().splitlines()
    ]
    assert any(name == ("vhdlan" if state.startswith("vhdl") else "vlogan") for name, _ in calls)
    assert any(name == "vcs" for name, _ in calls)
    assert any(name == "simv" for name, _ in calls) is (not one_shot)


@pytest.mark.parametrize("one_shot", [False, True])
def test_vcs_timeout_preserves_runtime_diagnostics(tmp_path, monkeypatch, one_shot):
    flow = launch(tmp_path, monkeypatch, "timeout", one_shot, {"timeout": 2.0})
    assert not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"
    assert flow.results["sim.errors"] == 1
    assert "XEDA_VCS_RUNTIME_START" in (flow.run_path / "vcs_runtime.log").read_text()


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("wave", [None, "fsdb", "vpd", "evcd"])
def test_vcs_waveforms_gui_and_runtime_flags_keep_the_contract(
    tmp_path, monkeypatch, one_shot, wave
):
    settings = {"gui": "dve", "simv_flags": ["+runtime_only"], "vcs_flags": ["+compile_only"]}
    if wave:
        settings[wave] = "wave." + wave
    flow = launch(tmp_path, monkeypatch, "finish5", one_shot, settings)
    assert flow.succeeded
    calls = [
        json.loads(line)
        for line in (flow.run_path / "fake_vcs.invocations").read_text().splitlines()
    ]
    compile_args = next(args for name, args in calls if name == "vcs")
    runtime_args = (
        compile_args if one_shot else next(args for name, args in calls if name == "simv")
    )
    assert "-gui=dve" in runtime_args and "-ucli" in runtime_args and "-i" in runtime_args
    assert ("+runtime_only" in compile_args) is False
    assert ("+compile_only" in runtime_args) is one_shot
    script = (flow.run_path / "vcs_runtime.tcl").read_text()
    assert "XEDA_VCS_RUNTIME_START" in script and "senv time" in script
    if wave:
        assert "dump -file" in script


@pytest.mark.parametrize("one_shot", [False, True])
def test_vcs_user_script_runs_inside_evidence_boundary(tmp_path, monkeypatch, one_shot):
    tmp_path.joinpath("design").mkdir()
    script = tmp_path / "design" / "user script.tcl"
    script.write_text("puts USER_SCRIPT_EXECUTED\nrun\nquit\n")
    flow = launch(tmp_path, monkeypatch, "finish5", one_shot, {"ucli_script": script})
    assert flow.succeeded
    assert "USER_SCRIPT_EXECUTED" in (flow.run_path / "vcs_runtime.log").read_text()


@pytest.mark.parametrize("mode", ["gui", "ucli_script"])
def test_vcs_rejects_unenforceable_user_stop_time(tmp_path, monkeypatch, mode):
    settings = {"stop_time": "10ns", mode: True if mode == "gui" else "user.tcl"}
    with pytest.raises(FlowSettingsException, match="stop_time"):
        launch(tmp_path, monkeypatch, settings=settings)
    assert not (tmp_path / "run" / "fake_vcs.runtime").exists()


def evidence_flow(tmp_path):
    """A flow built directly, in `tmp_path`, whose design has a source VCS reads."""
    root = tmp_path / "design"
    root.mkdir()
    (root / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(name="tb", design_root=root, rtl={"sources": ["tb.sv"]}, tb={"top": "tb"})
    return Vcs({}, design, tmp_path)


@pytest.mark.parametrize(
    "mutation", ["missing", "stale", "utf8", "time", "precision", "duplicate", "limit", "compiler"]
)
def test_vcs_missing_stale_or_malformed_runtime_cannot_pass(tmp_path, mutation):
    from xeda.flows.vcs_evidence import parse_vcs_evidence

    flow = evidence_flow(tmp_path)
    path = tmp_path / "vcs_runtime.log"
    valid = (
        'XEDA_VCS_RUNTIME_START\n$finish called from file "tb.sv", line 4.\n'
        "XEDA_VCS_TIME=5 NS\nXEDA_VCS_PRECISION=1 PS\nXEDA_VCS_RUNTIME_END\n"
    )
    if mutation == "stale":
        path.write_text(valid)
    flow.start_run()
    if mutation == "missing" or mutation == "stale":
        assert parse_vcs_evidence(flow) is None
        return
    if mutation == "utf8":
        path.write_bytes(b"\xff")
    else:
        content = valid
        if mutation == "time":
            content = content.replace("5 NS", "bogus")
        elif mutation == "precision":
            content = content.replace("1 PS", "0 PS")
        elif mutation == "duplicate":
            content = content + valid
        elif mutation == "limit":
            content = content.replace(
                '$finish called from file "tb.sv", line 4.', "XEDA_VCS_LIMIT=10 NS"
            )
        elif mutation == "compiler":
            content = '$finish called from file "tb.sv", line 4.\n'
        path.write_text(content)
    assert parse_vcs_evidence(flow) is None


@pytest.mark.parametrize("one_shot", [False, True])
def test_vcs_suppressed_checkpoint_fails_the_actual_launch(tmp_path, monkeypatch, one_shot):
    original = Tool.run

    def run(tool, *args, **kwargs):
        result = original(tool, *args, **kwargs)
        if tool.executable == "./simv" or tool.executable == "vcs" and "-R" in args:
            path = Path("vcs_runtime.log")
            path.write_text(path.read_text().replace("XEDA_VCS_RUNTIME_END", "SUPPRESSED"))
        return result

    monkeypatch.setattr(Tool, "run", run)
    flow = launch(tmp_path, monkeypatch, "finish5", one_shot)
    assert not flow.succeeded
    assert (flow.run_path / "fake_vcs.runtime").exists()


def test_vcs_runtime_keeps_settings_lists_and_process_environment(tmp_path, monkeypatch):
    before = {key: os.environ.get(key) for key in ("VCS_TARGET_ARCH", "VCS_ARCH_OVERRIDE")}
    settings = {"vcs_flags": ["+custom"], "vlogan_flags": ["+v2k"], "simv_flags": ["+custom_run"]}
    original = json.loads(json.dumps(settings))
    flow = launch(tmp_path, monkeypatch, "finish5", settings=settings)
    assert flow.succeeded
    assert settings == original
    assert {key: os.environ.get(key) for key in before} == before
    assert flow.settings.vcs_flags == ["+custom"]
    assert flow.settings.simv_flags == ["+custom_run"]


@pytest.mark.parametrize("one_shot", [False, True])
def test_vcs_linked_runtime_log_does_not_overwrite_outside_data(tmp_path, monkeypatch, one_shot):
    outside = tmp_path / "outside.log"
    outside.write_text("keep me")
    original = Tool.run

    def run(tool, *args, **kwargs):
        result = original(tool, *args, **kwargs)
        if tool.executable == "vlogan":
            Path("vcs_runtime.log").symlink_to(outside)
        return result

    monkeypatch.setattr(Tool, "run", run)
    flow = launch(tmp_path, monkeypatch, "finish5", one_shot)
    assert flow.succeeded
    assert outside.read_text() == "keep me"


@pytest.mark.parametrize("stop_time", ["0ns", "0", "10", 10, 10.0, "1e1ns", "0.01us"])
def test_vcs_absolute_limit_validation_and_numeric_units(tmp_path, monkeypatch, stop_time):
    if stop_time in ("0ns", "0"):
        with pytest.raises(FlowSettingsException, match="positive"):
            launch(tmp_path, monkeypatch, settings={"stop_time": stop_time})
    else:
        flow = launch(tmp_path, monkeypatch, "limit10", settings={"stop_time": stop_time})
        assert flow.succeeded


@pytest.mark.parametrize("one_shot", [False, True])
def test_vcs_rerun_cannot_reuse_an_earlier_finish(tmp_path, monkeypatch, one_shot):
    positive = launch(tmp_path, monkeypatch, "finish5", one_shot)
    assert positive.succeeded
    silent = launch(tmp_path, monkeypatch, "silent", one_shot)
    assert not silent.succeeded
    assert silent.results["sim.ended_by"] == "unknown"
    assert (
        '$finish called from file "tb.sv"' not in (silent.run_path / "vcs_runtime.log").read_text()
    )


@pytest.mark.parametrize("one_shot", [False, True])
def test_vcs_user_script_and_waveform_keep_the_input_script(tmp_path, monkeypatch, one_shot):
    tmp_path.joinpath("design").mkdir()
    script = tmp_path / "design" / "user.tcl"
    script.write_text("puts ORIGINAL_SCRIPT\nrun\nquit\n")
    original = script.read_bytes()
    flow = launch(
        tmp_path, monkeypatch, "finish5", one_shot, {"ucli_script": script, "fsdb": "dump.fsdb"}
    )
    assert flow.succeeded
    assert script.read_bytes() == original
    assert "ORIGINAL_SCRIPT" in (flow.run_path / "vcs_runtime.log").read_text()


def _invocations(flow) -> list[tuple[str, list[str]]]:
    """The tools the fake VCS ran and their arguments, in order."""
    lines = (flow.run_path / "fake_vcs.invocations").read_text().splitlines()
    return [tuple(json.loads(line)) for line in lines]  # type: ignore[misc]


def test_vcs_analyzes_the_sources_it_reads_by_language_in_design_order(tmp_path, monkeypatch):
    """Verilog, then SystemVerilog, then VHDL, each as the RTL sources followed by the
    testbench's, in design order. A header is found through its directory, and a source of a type
    VCS is not given (a constraint file) is not analyzed."""
    use_fake_vcs(monkeypatch)
    root = tmp_path / "design"
    (root / "inc").mkdir(parents=True)
    files = {
        "inc/defs.vh": "`define W 4\n",
        "b.v": "module b; endmodule\n",
        "a.sv": "module a; endmodule\n",
        "c.vhd": "entity c is end;\n",
        "pins.xdc": "# a constraint\n",
        "tb.sv": "module tb; endmodule\n",
        "d.v": "module d; endmodule\n",
    }
    for name, text in files.items():
        (root / name).write_text(text)
    design = Design(
        name="order",
        design_root=root,
        rtl={"sources": ["inc/defs.vh", "b.v", "a.sv", "c.vhd", "pins.xdc"], "top": "a"},
        tb={"sources": ["tb.sv", "d.v"], "top": "tb"},
    )
    flow = DefaultRunner(tmp_path / "run", rebuild_all=True).run_flow(Vcs, design, {})
    assert flow is not None

    def handed(args):
        return [Path(a).name for a in args if a.startswith(str(root))]

    vlogan = [args for name, args in _invocations(flow) if name == "vlogan"]
    vhdlan = [args for name, args in _invocations(flow) if name == "vhdlan"]
    assert [handed(args) for args in vlogan] == [["b.v", "d.v"], ["a.sv", "tb.sv"]]
    assert "-sverilog" not in vlogan[0] and "-sverilog" in vlogan[1]
    assert f"+incdir+{root / 'inc'}" in vlogan[0] + vlogan[1]
    assert [handed(args) for args in vhdlan] == [["c.vhd"]]


def test_vcs_refuses_a_chisel_source_when_planned(tmp_path, monkeypatch):
    """VCS analyzes Verilog, SystemVerilog and VHDL. A Chisel source used to be passed over. Now
    the design is refused, naming the source, before anything is set up."""
    use_fake_vcs(monkeypatch)
    root = tmp_path / "design"
    root.mkdir()
    (root / "tb.sv").write_text("module tb; endmodule\n")
    (root / "gen.sc").write_text("object Gen\n")
    design = Design(
        name="d", design_root=root, rtl={"sources": ["tb.sv", "gen.sc"]}, tb={"top": "tb"}
    )
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(tmp_path / "run", rebuild_all=True).run_flow(Vcs, design, {})
    message = str(raised.value)
    assert message.startswith("vcs cannot read the design's Chisel source(s) "), message
    assert str(root / "gen.sc") in message
    assert not (tmp_path / "run").exists()


TOOLS = ("vlogan", "vhdlan", "vcs", "fsdb2vcd", "vpd2vcd", "simv")


def _built_vcs(tmp_path, **settings) -> Vcs:
    root = tmp_path / "design"
    root.mkdir(parents=True, exist_ok=True)
    (root / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(
        name="tb", design_root=root, rtl={"sources": [root / "tb.sv"]}, tb={"top": "tb"}
    )
    flow = Vcs(Vcs.Settings(**settings), design, tmp_path / "run")
    flow.init()
    return flow


def test_a_vcs_flow_makes_its_own_tools(tmp_path):
    """The flow's settings reach its tools, so a tool is made for the flow: one held by the class
    would be every flow's, and carry what one learned (or was set to) to the next."""
    assert not [name for name in TOOLS if isinstance(vars(Vcs).get(name), Tool)]
    first, second = _built_vcs(tmp_path / "a"), _built_vcs(tmp_path / "b")
    for name in TOOLS:
        assert isinstance(getattr(first, name), Tool)
        assert getattr(first, name) is not getattr(second, name)
    assert first.simv.highlight_rules is not None  # non-interactive: its output is colored
    # a change to one flow's tool is that flow's only
    first.simv.highlight_rules = None
    assert second.simv.highlight_rules is not None


def test_the_settings_of_a_vcs_flow_reach_its_tools(tmp_path):
    flow = _built_vcs(tmp_path, dockerized=True, docker="example/vcs:2026", print_commands=False)
    for name in TOOLS:
        tool = getattr(flow, name)
        assert tool.dockerized and tool.docker is not None
        assert tool.docker.image == "example/vcs:2026"
        assert tool.print_command is False
    assert not _built_vcs(tmp_path / "plain").vcs.dockerized
