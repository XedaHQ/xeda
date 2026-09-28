"""`vivado_synth` succeeds only when the Vivado runs it launched completed their steps.

In project mode the script launches Vivado's synthesis run (`synth_1`), then its implementation
run (`impl_1`) to `route_design`, or to `write_bitstream` with a `bitstream` asked for, and waits
for each. What became of a run is in its properties alone: `wait_on_run` returns normally when
the run failed in Vivado 2021.1, so a failed synthesis, implementation step or `write_bitstream`
passed as a successful run, without the bitstream it was asked for. (Vivado 2024.2 raises an
error there, which ended the script without saying which run failed or why.) The fake Vivado
runs each run's steps and step hooks (`TCL_MODEL`), fails a step named in `XEDA_FAKE_TOOL_FAIL`,
and reports what became of each run as Vivado 2024.2 does (`STATUS`, `PROGRESS`).
"""

import re
import shutil
from pathlib import Path

import click
import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoSynth
from xeda.flows.vivado import vivado_synth as vs

from .tool_utils import fake_calls, fake_returns, use_fake_tools

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.toml"
FLOWS_DIR = Path(__file__).parent.parent / "src" / "xeda" / "flows"
PART = "xc7a12tcsg325-1"
BITSTREAM = "outputs/sqrt.bit"

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)


def _run(tmp_path: Path, monkeypatch, fail: list[str] | None = None, **settings) -> VivadoSynth:
    """Run `vivado_synth` on `sqrt` with the fake Vivado, failing the steps named in `fail`."""
    use_fake_tools(monkeypatch)
    if fail:
        monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", " ".join(fail))
    flow = DefaultRunner(tmp_path / "run").run_flow(
        VivadoSynth, Design.from_toml(SQRT), {"fpga": PART, "clock_period": 5.5, **settings}
    )
    assert isinstance(flow, VivadoSynth)
    return flow


def _checked_runs(flow: VivadoSynth) -> list[str]:
    """The runs whose status the script asked Vivado for, in order."""
    return [c[2] for c in fake_calls(flow.run_path) if c[:2] == ["get_property", "STATUS"]]


def _run_errors(output: str, run: str) -> list[str]:
    """The errors in the flow's output that name `run`'s log: the script's message about it."""
    log = f"sqrt.runs/{run}/runme.log"
    lines = click.unstyle(output).splitlines()
    return [line for line in lines if "ERROR:" in line and log in line]


@needs_tclsh
@pytest.mark.parametrize("bitstream", [None, BITSTREAM], ids=["routed", "bitstream"])
def test_a_run_whose_vivado_runs_complete_their_steps_succeeds(
    tmp_path, monkeypatch, bitstream
) -> None:
    flow = _run(tmp_path, monkeypatch, **({"bitstream": bitstream} if bitstream else {}))
    assert flow.succeeded
    assert _checked_runs(flow) == ["synth_1", "impl_1"]
    step = "write_bitstream" if bitstream else "route_design"
    assert flow.results["status"] == f"{step} Complete!"
    if bitstream:
        # Vivado's `write_bitstream` step wrote it, the step's hook put it where it is registered
        assert flow.artifacts[vs.BITSTREAM] == Path(bitstream)
        assert (flow.run_path / bitstream).is_file()


@needs_tclsh
@pytest.mark.parametrize(
    ("step", "run", "bitstream"),
    [
        ("synth_design", "synth_1", None),
        ("place_design", "impl_1", None),
        ("route_design", "impl_1", BITSTREAM),
        ("write_bitstream", "impl_1", BITSTREAM),
    ],
    ids=["synthesis", "placement", "routing", "write_bitstream"],
)
def test_a_vivado_run_that_does_not_complete_its_step_fails_the_flow(
    tmp_path, monkeypatch, capfd, step, run, bitstream
) -> None:
    """The script stops at the first run that did not complete, with one message naming the
    run, its status and its log; `status` records it. The bitstream is not registered."""
    settings = {"bitstream": bitstream} if bitstream else {}
    flow = _run(tmp_path, monkeypatch, fail=[step], **settings)
    assert not flow.succeeded
    assert flow.results["status"] == f"{step} ERROR"
    assert _checked_runs(flow) == ["synth_1", "impl_1"][: 1 + (run == "impl_1")]
    launched = [c[1] for c in fake_calls(flow.run_path) if c[0] == "launch_runs"]
    assert launched == ["synth_1", "impl_1"][: 1 + (run == "impl_1")]
    (message,) = _run_errors(capfd.readouterr().out, run)
    assert f"run {run} did not complete" in message
    assert f'"{step} ERROR"' in message
    assert str(flow.run_path / "sqrt.runs" / run / "runme.log") in message
    assert vs.BITSTREAM not in flow.results.artifacts
    assert not (flow.run_path / BITSTREAM).exists()


@needs_tclsh
@pytest.mark.parametrize("fail_timing", [True, False], ids=["fail_timing", "no_fail_timing"])
def test_a_route_hook_that_fails_on_timing_fails_the_flow(
    tmp_path, monkeypatch, capfd, fail_timing
) -> None:
    """With `fail_timing`, xeda's own `route_design` hook fails the step on a negative slack, and
    with it the implementation run: the flow fails as the run did. Without it the run goes on to
    write the bitstream (the fake's canned timing reports, which the flow parses, meet timing)."""
    fake_returns(monkeypatch, {("get_property", "SLACK"): "-0.5"})
    flow = _run(tmp_path, monkeypatch, bitstream=BITSTREAM, fail_timing=fail_timing)
    assert flow.succeeded != fail_timing
    messages = _run_errors(capfd.readouterr().out, "impl_1")
    if fail_timing:
        assert flow.results["status"] == "route_design ERROR"
        assert len(messages) == 1 and '"route_design ERROR"' in messages[0]
    else:
        assert flow.results["status"] == "write_bitstream Complete!"
        assert not messages
        assert (flow.run_path / BITSTREAM).is_file()


@needs_tclsh
def test_a_run_reporting_success_without_its_bitstream_fails_naming_it(
    tmp_path, monkeypatch
) -> None:
    """A run that reports it completed `write_bitstream` although the bitstream is not where it
    is registered fails, naming the path, as `ise_synth` and `diamond_synth` do."""
    fake_returns(
        monkeypatch,
        {
            ("get_property", "STATUS", "impl_1"): "write_bitstream Complete!",
            ("get_property", "PROGRESS", "impl_1"): "100%",
        },
    )
    with pytest.raises(FlowFatalError, match=re.escape(str(tmp_path / "run"))) as raised:
        _run(tmp_path, monkeypatch, fail=["write_bitstream"], bitstream=BITSTREAM)
    assert re.search(r"bitstream .*sqrt\.bit", str(raised.value))


@needs_tclsh
def test_a_run_that_reports_its_step_incomplete_fails_the_flow(tmp_path, monkeypatch) -> None:
    """A run's status alone is not enough: its progress must be 100% too."""
    fake_returns(monkeypatch, {("get_property", "PROGRESS", "impl_1"): "80%"})
    flow = _run(tmp_path, monkeypatch)
    assert not flow.succeeded
    assert flow.results["status"] == "route_design Complete!"


def test_status_is_documented_for_the_flow_that_sets_it_only() -> None:
    """Only project mode has runs whose status `status` records: `vivado_alt_synth` runs Vivado's
    steps itself, and never sets it."""
    assert "status" in VivadoSynth.results_description
    assert "status" not in VivadoAltSynth.results_description


def test_every_vivado_run_a_script_launches_is_checked() -> None:
    """A sweep over every flow's TCL templates: a script that launches a Vivado run waits for it
    only through `xedaWaitOnRun`, which checks that it completed its step."""
    checked = 0
    for template in sorted(FLOWS_DIR.glob("*/templates/*.tcl")):
        text = re.sub(r"\{#.*?#\}", "", template.read_text(), flags=re.S)
        code = [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]
        launched = [line.split()[1] for line in code if line.startswith("launch_runs ")]
        waited = [line.split()[1] for line in code if line.startswith("xedaWaitOnRun ")]
        assert launched == waited, template.name
        waits = [line for line in code if re.search(r"\bwait_on_runs?\b", line)]
        assert waits == (["catch {wait_on_run $run}"] if launched else []), template.name
        checked += len(launched)
    assert checked == 2  # vivado_synth's synth_1 and impl_1
