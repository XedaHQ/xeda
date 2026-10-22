"""Focused simulation coverage for the standalone Bluespec Ascon example."""

from __future__ import annotations

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim

from .tool_utils import (
    require_bluesim,
    require_bsc,
    require_iverilog,
    require_verilator,
    run_report,
)

pytestmark = pytest.mark.usefixtures("tool_output_on_terminal")

DESIGN_FILE = Path(__file__).parent.parent / "examples" / "bluespec" / "ascon" / "ascon.yaml"
CONFIGURATIONS = (
    pytest.param(False, 2, id="ascon128-unroll2"),
    pytest.param(False, 3, id="ascon128-unroll3"),
    pytest.param(False, 6, id="ascon128-unroll6"),
    pytest.param(True, 1, id="ascon128a-unroll1"),
    pytest.param(True, 2, id="ascon128a-unroll2"),
    pytest.param(True, 4, id="ascon128a-unroll4"),
)


def _design(ascon128a: bool, unroll_factor: int) -> Design:
    design = Design.from_file(DESIGN_FILE)
    assert design.rtl is not None
    design.rtl.defines = {"UNROLL_FACTOR": unroll_factor}
    if ascon128a:
        design.rtl.defines["ASCON128A"] = True
    return design


def _run(design: Design, run_root: Path):
    flow = DefaultRunner(run_root, display_results=False).run_flow(
        BscSim, design, {"simulator": "bluesim"}
    )
    assert flow is not None
    return flow


@pytest.mark.parametrize("ascon128a, unroll_factor", CONFIGURATIONS)
def test_reference_vectors_pass_for_supported_unroll_factors(
    ascon128a: bool, unroll_factor: int, tmp_path: Path, capfd
):
    """Each supported round grouping passes the independent v1.2 reference vectors."""
    require_bluesim()
    flow = _run(_design(ascon128a, unroll_factor), tmp_path)
    captured = capfd.readouterr()
    assert flow.succeeded, run_report(flow, captured)
    assert "PASS:" in captured.out


@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_ascon128a_unroll4_passes_on_verilog_simulators(simulator: str, tmp_path: Path, capfd):
    """The Ascon-128a factor-4 build passes on both Verilog simulators."""
    require_bsc()
    (require_iverilog if simulator == "iverilog" else require_verilator)()
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(
        BscSim, _design(True, 4), {"simulator": simulator}
    )
    assert flow is not None
    captured = capfd.readouterr()
    assert flow.succeeded, run_report(flow, captured)
    assert "PASS:" in captured.out


def test_ascon128a_testbench_catches_an_injected_bug(tmp_path: Path, capfd):
    """The Ascon-128a build fails its first reference comparison after fault injection."""
    require_bluesim()
    design = _design(True, 1)
    assert design.tb is not None
    design.tb.defines = {"XEDA_INJECT_BUG": True}
    flow = _run(design, tmp_path)
    output = capfd.readouterr().out
    assert not flow.succeeded
    assert "FAIL:" in output and "PASS:" not in output


def test_ascon128_rejects_an_unroll_factor_that_does_not_divide_pb(tmp_path: Path, capfd):
    """The compiler reports the incompatible six-round P_b factor."""
    require_bsc()
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(Bsc, _design(False, 4))
    assert flow is not None and not flow.succeeded
    captured = capfd.readouterr()
    diagnostic = f"{captured.out}\n{captured.err}".lower()
    assert "proviso" in diagnostic
    assert "t0043" in diagnostic
    assert "cannot synthesize `mkasconcipher'" in diagnostic
