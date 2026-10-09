import json
import re
import tempfile
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FPGA, Flow, FlowFatalError
from xeda.flow_runner import DefaultRunner
from xeda.flows import IseSynth
from xeda.flows.ise import format_value
from .tool_utils import fake_calls, use_fake_tools

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


def test_ise_synth_py(monkeypatch) -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    use_fake_tools(monkeypatch)
    assert path.exists()
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock={"period": 5.5})
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(IseSynth, design, settings)
        assert flow is not None, "run_flow returned None"
        assert flow.run_path is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        # assert flow.succeeded

        recorded = json.loads(settings_json.read_text())["flow_settings"]
        # Project properties are recorded exactly as written. ISE's own quoting is applied by
        # the template, because quoting is not idempotent: a validator that quoted on the way in
        # turned "High" into ""High"" on every re-validation, including reloading this file.
        assert recorded["synthesis_options"]["Optimization Effort"] == "High"

        effective = json.loads(settings_json.read_text())["effective_flow_settings"]
        assert effective["xcf_file"] == "constraints.xcf"
        assert effective["ucf_files"] == ["constraints.ucf"]

        script = (flow.run_path / "ise_synth.tcl").read_text()
        assert 'project set "Optimization Effort" "High" -process "Synthesize - XST"' in script
        assert (
            'project set "Optimize Instantiated Primitives" TRUE -process "Synthesize - XST"'
            in script
        )
        calls = fake_calls(flow.run_path)
        for process in ("Implement Design", "Generate Programming File"):
            assert ["process", "run", process] in calls
            assert ["process", "get", process, "status"] in calls
        # the files the fake xtclsh writes, as ISE names them: after the top, in the project
        top = flow.run_path / str(design.rtl.top)
        expected = {
            "bitstream": Path(f"{top}.bit"),
            "place_route_report": Path(f"{top}_par.xrpt"),
            "synthesis_report": Path(f"{top}.syr"),
        }
        assert flow.artifacts == expected
        assert all(path.is_file() for path in expected.values())
        recorded_artifacts = json.loads(results_json.read_text())["artifacts"]
        assert recorded_artifacts == {k: str(v) for k, v in expected.items()}


ISE_SETTINGS = dict(fpga=FPGA("xc7a12tcsg325-1"), clock={"period": 5.5})


def _run_ise(run_dir: Path, monkeypatch, **launcher) -> Flow:
    use_fake_tools(monkeypatch)
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    flow = DefaultRunner(run_dir, **launcher).run_flow(IseSynth, design, ISE_SETTINGS)
    assert flow is not None
    return flow


@pytest.mark.parametrize("how", ["result", "status", "both"])
@pytest.mark.parametrize("process", ["Implement Design", "Generate Programming File"])
def test_a_failed_ise_process_fails_the_run(process, how, tmp_path, monkeypatch) -> None:
    """ISE's `process run` reports a failed process only by its result and the process status,
    never by a TCL error: xtclsh exited 0 after a failed bitgen, and the run went on to record the
    bitstream a previous run had left in the reused directory. The script checks both and exits
    1, and the flow removes its own previous outputs first, so no earlier bitstream survives."""
    first = _run_ise(tmp_path, monkeypatch)
    stale = first.run_path / "sqrt.bit"
    assert stale.is_file()

    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "{" + process + "}")
    monkeypatch.setenv("XEDA_FAKE_ISE_FAILURE", how)
    flow = _run_ise(tmp_path, monkeypatch)

    assert flow.run_path == first.run_path
    assert not flow.results.success
    assert not stale.exists()
    assert "bitstream" not in flow.artifacts
    recorded = json.loads((flow.run_path / "results.json").read_text())
    assert "bitstream" not in recorded["artifacts"]
    ran = [call[2] for call in fake_calls(flow.run_path) if call[:2] == ["process", "run"]]
    assert ran[-1] == process  # nothing runs after a failed process


def test_a_failed_ise_run_leaves_nothing_of_the_previous_run_in_its_run_directory(
    tmp_path, monkeypatch
) -> None:
    """ISE empties its run directory before running (a stale ISE project would be reopened
    otherwise), so a run whose tool writes nothing leaves none of the previous run's outputs
    (bitstream, reports) and lists no artifacts. The run directory is xeda's; the user's own
    files, in the directory xeda was started from, are never touched."""
    work = tmp_path / "work"
    work.mkdir()
    theirs = [work / name for name in ("sqrt.bit", "sqrt.syr", "notes.txt", "sqrt.ucf")]
    for path in theirs:
        path.write_text("the user's\n")
    (work / "rtl").mkdir()
    (work / "rtl" / "core.vhdl").write_text("-- a source\n")
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(work)
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    runner = DefaultRunner(tmp_path / "xeda_run", rebuild_all=True)

    first = runner.run_flow(IseSynth, design, ISE_SETTINGS)
    assert first is not None
    earlier = [path for path in first.run_path.rglob("*") if path.suffix in {".bit", ".syr"}]
    assert any(path.suffix == ".bit" for path in earlier)

    # a run that writes none of the files: whatever is left of them would be from before
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "{Implement Design}")
    flow = runner.run_flow(IseSynth, design, ISE_SETTINGS)

    assert flow is not None and flow.run_path == first.run_path
    assert not flow.results.success
    assert [path for path in earlier if path.exists()] == []
    assert not flow.artifacts
    assert all(path.read_text() == "the user's\n" for path in theirs)
    assert (work / "rtl" / "core.vhdl").is_file()


def test_an_ise_run_without_its_bitstream_fails_naming_it(tmp_path, monkeypatch) -> None:
    """A run whose tool reported success but wrote no bitstream fails, naming the file it
    expected, and records nothing that does not exist."""
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_NO_OUTPUT", "1")
    monkeypatch.chdir(tmp_path)
    design = Design.from_file(RESOURCES_DIR / "design0/design0.toml")
    flow = IseSynth(IseSynth.Settings(**ISE_SETTINGS), design, tmp_path)
    flow.init()

    with pytest.raises(FlowFatalError, match=re.escape(str(tmp_path / "design0.bit"))):
        flow.run()
    assert ["process", "run", "Generate Programming File"] in fake_calls(tmp_path)
    assert not flow.artifacts


def test_ise_needs_a_top_before_any_tool_runs(tmp_path, monkeypatch) -> None:
    """ISE names its outputs after the top, so a design without one cannot run. That was
    asserted only after the whole ISE run, although it is known from the design."""
    use_fake_tools(monkeypatch)
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "clock": "clk"})
    run_dir = tmp_path / "run"

    with pytest.raises(FlowFatalError, match="rtl.top"):
        DefaultRunner(run_dir).run_flow(IseSynth, design, ISE_SETTINGS)
    assert not list(run_dir.rglob("fake_xtclsh.calls"))


def test_ise_project_options_are_quoted_exactly_once() -> None:
    """Every option group renders through `format_value`, `translate_options` included.

    `translate_options` was left out of the validator that quoted the other four, so a string
    given there reached `project set` bare while the same string elsewhere was quoted.
    """
    settings = IseSynth.Settings(  # type: ignore[call-arg]
        fpga=FPGA("xc7a12tcsg325-1"),
        clock={"period": 5.5},
        translate_options={"Allow Unmatched LOC Constraints": "true"},
    )
    assert settings.translate_options["Allow Unmatched LOC Constraints"] == "true"
    assert format_value(settings.translate_options["Allow Unmatched LOC Constraints"]) == '"true"'

    reloaded = IseSynth.Settings.model_validate(settings.model_dump())
    assert reloaded.model_dump() == settings.model_dump()
