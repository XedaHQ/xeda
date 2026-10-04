"""The Bluespec example designs (`examples/bluespec/`), end to end through both flows.

Each example is self-checking: its testbench prints `PASS` and finishes on success, and fails
with `$fatal` otherwise; defining `XEDA_INJECT_BUG` for the testbench plants a bug it must catch.
For every example this proves that `bsc` collects a Verilog file set that other tools accept as
it is (it elaborates, lints and synthesizes), that `bsc_sim` passes it on every simulator the
example can run on -- Bluesim cannot simulate imported Verilog -- and that a failing testbench
fails the run. A passing run must also print `PASS`: exit status 0 alone is what a testbench
that stopped early (`mkAutoFSM` finishes as soon as its sequence does) reports as well.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim, Yosys

from .tool_utils import (
    require_bluesim,
    require_bsc,
    require_iverilog,
    require_verilator,
    require_yosys,
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "bluespec"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*/*.yaml"))
SIMULATORS = ("bluesim", "verilator", "iverilog")


def _load(design_file: Path) -> Design:
    return Design.from_file(design_file)


def _imports_verilog(design: Design) -> bool:
    return bool(design.sources_of_type(SourceType.Verilog, SourceType.SystemVerilog))


def _run(flow_class: Any, design: Design, run_dir: Path, **settings: Any) -> Any:
    flow = DefaultRunner(run_dir, display_results=False).run_flow(flow_class, design, settings)
    assert flow is not None
    return flow


def _require_simulator(simulator: str) -> None:
    if simulator == "bluesim":
        require_bluesim()
        return
    require_bsc()
    (require_verilator if simulator == "verilator" else require_iverilog)()


def test_there_are_examples_in_both_languages():
    """The sweep below covers BSV, BH (Bluespec Classic), a mix of the two, and a design that
    imports Verilog."""
    names = {path.stem for path in EXAMPLES}
    assert {"gcd", "fir", "collatz", "crc32", "verilog_import"} <= names
    suffixes = {src.file.suffix for path in EXAMPLES for src in _load(path).rtl.sources}
    assert {".bsv", ".bs", ".v"} <= suffixes


@pytest.fixture(scope="module")
def generated() -> dict:
    """`bsc` run once per example, shared by the checks of its output."""
    return {}


def _generate(design_file: Path, generated: dict, tmp_path_factory) -> Any:
    if design_file not in generated:
        require_bsc()
        design = _load(design_file)
        run_dir = tmp_path_factory.mktemp(f"bsc_{design_file.stem}")
        # the examples compile without a single warning, and stay that way
        generated[design_file] = _run(Bsc, design, run_dir, promote_warnings=["ALL"])
    return generated[design_file]


@pytest.mark.parametrize("design_file", EXAMPLES, ids=lambda p: p.stem)
def test_bsc_collects_every_verilog_file_of_the_top(design_file, generated, tmp_path_factory):
    """The collected files are exactly a complete design: iverilog elaborates the top from them
    alone, and Verilator lints them."""
    flow = _generate(design_file, generated, tmp_path_factory)
    assert flow.succeeded
    top = flow.design.rtl.top
    assert flow.results["modules"][0] == top
    files: list[str] = list(flow.artifacts.verilog)
    assert files and Path(files[-1]).name == f"{top}.v" and all(Path(f).is_file() for f in files)
    require_iverilog()
    subprocess.run(
        ["iverilog", "-g2005", "-o", str(flow.run_path / "elab.vvp"), "-s", top, *files],
        check=True,
    )
    require_verilator()
    subprocess.run(
        ["verilator", "--lint-only", "-Wno-lint", "-Wno-style", "--no-timing"]
        + ["--top-module", top, *files],
        check=True,
        cwd=flow.run_path,
    )


@pytest.mark.parametrize("design_file", EXAMPLES, ids=lambda p: p.stem)
def test_the_generated_verilog_synthesizes(design_file, generated, tmp_path_factory, tmp_path):
    """A downstream flow takes `artifacts.verilog` as its sources as it is."""
    flow = _generate(design_file, generated, tmp_path_factory)
    assert flow.succeeded
    require_yosys()
    top = flow.design.rtl.top
    netlist_design = Design(
        name=f"{design_file.stem}_verilog",
        design_root=flow.run_path,
        rtl={"sources": list(flow.artifacts.verilog), "top": top, "clock_port": "CLK"},
    )
    synth = _run(Yosys, netlist_design, tmp_path)
    assert synth.succeeded


def _cases():
    for design_file in EXAMPLES:
        for simulator in SIMULATORS:
            marks = []
            if simulator == "bluesim" and _imports_verilog(_load(design_file)):
                marks = [pytest.mark.skip(reason="Bluesim cannot simulate imported Verilog")]
            yield pytest.param(
                design_file, simulator, id=f"{design_file.stem}-{simulator}", marks=marks
            )


@pytest.mark.parametrize("design_file, simulator", list(_cases()))
def test_the_testbench_passes(design_file, simulator, tmp_path, capfd):
    _require_simulator(simulator)
    flow = _run(BscSim, _load(design_file), tmp_path, simulator=simulator)
    out = capfd.readouterr().out
    assert flow.succeeded, out[-2000:]
    assert "PASS" in out


@pytest.mark.parametrize("design_file", EXAMPLES, ids=lambda p: p.stem)
def test_the_testbench_catches_an_injected_bug(design_file, tmp_path, capfd):
    """The testbenches check what they claim to: with the bug each one plants, the simulation
    runs and reports a failure, which fails the run -- not a compilation that breaks. One
    simulator suffices here; `test_bsc.py` shows a failure fails the run on each."""
    design = _load(design_file)
    simulator = "iverilog" if _imports_verilog(design) else "bluesim"
    _require_simulator(simulator)
    design.tb.defines = {**design.tb.defines, "XEDA_INJECT_BUG": True}
    flow = _run(BscSim, design, tmp_path, simulator=simulator)
    out = capfd.readouterr().out
    assert not flow.succeeded
    assert Path(flow.artifacts.executable).is_file()
    assert "FAIL" in out and "PASS" not in out
