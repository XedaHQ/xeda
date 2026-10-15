"""Command-line handoff checks for the standalone Ascon example."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Openroad, Yosys

from .tool_utils import require_bsc, require_yosys

EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "bluespec" / "ascon"
RUN_SCRIPT = EXAMPLE_DIR / "run.py"


def test_invalid_unroll_factor_fails_before_a_run_root_is_created(tmp_path):
    """Reject invalid round divisors before starting Xeda or an EDA tool."""
    run_root = tmp_path / "xeda_run"
    result = subprocess.run(
        [
            sys.executable,
            str(RUN_SCRIPT),
            "fpga",
            "--run-root",
            str(run_root),
            "--unroll-factor",
            "5",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unroll factor must divide 12 and 6" in result.stderr
    assert not run_root.exists()


@pytest.mark.parametrize("flow_class", (Yosys, Openroad), ids=("synthesis", "place-route"))
def test_asic_helper_sets_clocks_on_yosys_and_openroad_place(flow_class, tmp_path, monkeypatch):
    """Check the clock reaches synthesis and optional placement in both ASIC paths."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("ascon_run", RUN_SCRIPT)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    (tmp_path / "top.v").write_text("module mkAscon(input CLK); endmodule\n")
    design = Design(
        name="ascon_verilog",
        design_root=tmp_path,
        rtl={"sources": ["top.v"], "top": "mkAscon", "clock": "CLK"},
    )
    args = helper.argparse.Namespace(
        platform="nangate45", clock_period="10 ns", pdk_root=tmp_path / "pdks"
    )
    settings = helper._asic_flow_settings(args, design.rtl.clock)

    assert "clock" not in settings
    assert "clocks" not in settings
    plan = DefaultRunner(tmp_path / "plan", display_results=False).plan(
        flow_class, design, flow_settings=settings
    )
    names = ("yosys", "openroad_place") if flow_class is Openroad else ("yosys",)
    for name in names:
        clock = next(iter(plan.node(name).settings.clocks.values()))
        assert clock.port == "CLK"
        assert clock.period.to("ns") == 10
        assert plan.node(name).settings.pdk_root == tmp_path / "pdks"


@pytest.mark.real_tool
def test_fpga_helper_hands_off_bsc_artifacts_with_top_and_clock(tmp_path):
    """Run the helper through Yosys to cover its public artifact handoff."""
    require_bsc()
    require_yosys()
    run_root = tmp_path / "xeda_run"
    result = subprocess.run(
        [
            sys.executable,
            str(RUN_SCRIPT),
            "fpga",
            "--run-root",
            str(run_root),
            "--clock-period",
            "10ns",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output[-4000:]
    assert re.search(r"Handoff: [1-9][0-9]* Verilog files, top=mkAscon, clock=CLK", output)
    assert "yosys_fpga:" in output
