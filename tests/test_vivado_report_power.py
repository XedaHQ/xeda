"""`report_power`: the run fails when Vivado wrote no `power.xml`.

The scripts pass `-quiet` to `report_power`, so that a design without a clock does not fail the
run. Vivado then returns success for a failed `report_power`, and the run would otherwise succeed
with no report. The fake Vivado does the same (`tests/fake_tools/fake_tool.py`): a `report_power
-quiet` named in `XEDA_FAKE_TOOL_FAIL` fails silently and writes nothing.
"""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoSynth

from .tool_utils import fake_calls, use_fake_tools

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
SETTINGS = {"fpga": "xc7a12tcsg325-1", "clock": {"period": 5.5}, "report_power": True}

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)

#: a flow that runs the steps in a Vivado project, and one that runs them without a project
FLOWS = [
    pytest.param(VivadoSynth, id="vivado_synth"),
    pytest.param(VivadoAltSynth, id="vivado_alt_synth"),
]


def _launch(flow_class, root: Path, **settings):
    flow = DefaultRunner(root, rebuild_all=True).run_flow(
        flow_class, Design.from_file(SQRT), {**SETTINGS, **settings}
    )
    assert flow is not None
    return flow


def _power_xml(flow) -> Path:
    return flow.run_path / "reports" / "route_design" / "power.xml"


def _error(flow) -> str:
    return json.loads((flow.run_path / "results.json").read_text())["error"]["message"]


@needs_tclsh
@pytest.mark.parametrize("flow_class", FLOWS)
def test_a_run_that_wrote_the_power_report_succeeds(flow_class, tmp_path, monkeypatch) -> None:
    use_fake_tools(monkeypatch)
    flow = _launch(flow_class, tmp_path / "run")
    assert flow.succeeded
    assert _power_xml(flow).is_file()
    assert any(call[0] == "report_power" and "-quiet" in call for call in fake_calls(flow.run_path))


@needs_tclsh
@pytest.mark.parametrize("flow_class", FLOWS)
def test_a_run_whose_report_power_wrote_nothing_fails_naming_it(
    flow_class, tmp_path, monkeypatch
) -> None:
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "report_power")
    flow = _launch(flow_class, tmp_path / "run")
    assert not flow.succeeded
    assert not _power_xml(flow).exists()
    message = _error(flow)
    assert "`report_power` is set" in message and "reports/route_design/power.xml" in message


@needs_tclsh
@pytest.mark.parametrize("flow_class", FLOWS)
def test_a_power_report_of_an_earlier_run_does_not_count(
    flow_class, tmp_path, monkeypatch
) -> None:
    """A report that an earlier run left is not this run's. The earlier run did not read it, so
    the launcher does not remove it before the next run."""
    use_fake_tools(monkeypatch)
    first = _launch(flow_class, tmp_path / "run", report_power=False)
    assert first.succeeded and not _power_xml(first).exists()
    stale = _power_xml(first)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("<report/>")
    an_hour_ago = time.time() - 3600
    os.utime(stale, (an_hour_ago, an_hour_ago))
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "report_power")
    second = _launch(flow_class, tmp_path / "run")
    assert second.run_path == first.run_path
    assert stale.read_text() == "<report/>"  # the file is still the earlier one
    assert not second.succeeded
    assert "wrote no power report" in _error(second)


@needs_tclsh
@pytest.mark.parametrize("flow_class", FLOWS)
def test_without_report_power_no_report_is_expected(flow_class, tmp_path, monkeypatch) -> None:
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "report_power")
    flow = _launch(flow_class, tmp_path / "run", report_power=False)
    assert flow.succeeded
    assert not _power_xml(flow).exists()
