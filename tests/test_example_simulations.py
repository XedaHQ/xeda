"""Every bundled example that brings a testbench passes under the simulators it is shown with.

An example is the first thing a user runs, so a testbench that no longer passes under the
evidence rule (for example one that ends with `wait;` and never calls `std.env.finish`) is a bug
of the product. `SIMULATED` lists, for each example design, the simulators it must pass on; a
design file or a project design that has a testbench and is neither listed there nor in
`COVERED_ELSEWHERE` fails the scan, and so does a simulator that a design's own `flows` section
names but the table leaves out. Each run is the documented command: `xeda run <simulator>
<design file>` in the design's directory (for a project, `--design-name`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.design import Design
from xeda.flow import SimFlow
from xeda.flow_runner import get_flow_class
from xeda.xedaproject import XedaProject

from . import tool_utils

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

VHDL = ("ghdl_sim", "nvc")

#: (design file, design name in a project or None) -> simulators the design passes on
SIMULATED: dict[tuple[str, str | None], tuple[str, ...]] = {
    ("vhdl/xedaproject.yaml", "full-adder"): VHDL,
    ("vhdl/pipeline/pipelined_adder.yaml", None): VHDL,
    ("vhdl/sqrt/sqrt.yaml", None): VHDL,
    ("vhdl/Trivium/trivium.yaml", None): VHDL,
    ("vhdl/Trivium/trivium-dc.xeda.yaml", None): VHDL,
    ("sv/fifo/fifo.xeda.yaml", None): ("verilator",),
    ("sv/fifo/fifo_cocotb.xeda.yaml", None): ("verilator",),
    ("mixed_language/blink/blinky.xeda.yaml", None): ("yosys_sim",),
    ("mixed_language/blink/xedaproject.yaml", "blink"): ("yosys_sim",),
}

#: examples with a testbench that another test file runs, by directory
COVERED_ELSEWHERE = {
    "bluespec/": "tests/test_bsc_examples.py runs every one through bsc and bsc_sim",
}


def _design_files() -> list[Path]:
    return sorted(
        p
        for p in EXAMPLES_DIR.rglob("*")
        if p.suffix in (".yaml", ".yml", ".toml", ".json") and "xeda_run" not in p.parts
    )


def _designs() -> list[tuple[str, str | None, Design]]:
    """Every example design as (file relative to the examples, project design name, design)."""
    found = []
    for path in _design_files():
        relative = path.relative_to(EXAMPLES_DIR).as_posix()
        if path.stem == "xedaproject":
            project = XedaProject.from_file(path)
            for index in range(len(project.designs)):
                design = project.get_design(index)
                assert design is not None
                found.append((relative, design.name, design))
        else:
            found.append((relative, None, Design.from_file(path)))
    return found


DESIGNS = _designs()


def _has_testbench(design: Design) -> bool:
    return bool(design.tb.sources) or bool(design.tb.cocotb)


@pytest.mark.parametrize(
    ("relative", "name", "design"),
    [d for d in DESIGNS if _has_testbench(d[2])],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_example_with_a_testbench_is_simulated_by_the_oracle(relative, name, design):
    if any(relative.startswith(prefix) for prefix in COVERED_ELSEWHERE):
        return
    assert (relative, name) in SIMULATED, (
        f"{relative}{' (' + name + ')' if name else ''} has a testbench: add it to SIMULATED "
        "with the simulators it passes on"
    )


@pytest.mark.parametrize(
    ("relative", "name", "design"),
    [d for d in DESIGNS if (d[0], d[1]) in SIMULATED],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_simulator_an_example_names_is_in_the_table(relative, name, design):
    named = {
        get_flow_class(flow).name
        for flow in design.flow or {}
        if issubclass(get_flow_class(flow), SimFlow)
    }
    assert named <= set(SIMULATED[(relative, name)]), (relative, named)


def test_the_table_lists_only_designs_that_exist():
    existing = {(relative, name) for relative, name, _ in DESIGNS}
    assert set(SIMULATED) <= existing


def _require_tools(simulator: str, design: Design) -> None:
    if design.tb.cocotb:
        tool_utils.require_cocotb()
    if any(source.file.suffix in (".c", ".cpp") for source in design.tb.sources):
        tool_utils.require_c_toolchain()
    {
        "ghdl_sim": tool_utils.require_ghdl,
        "nvc": tool_utils.require_nvc_evidence,
        "verilator": tool_utils.require_verilator,
        "yosys_sim": tool_utils.require_cxxrtl_evidence,
    }[simulator]()


CASES = [
    (relative, name, simulator)
    for (relative, name), simulators in SIMULATED.items()
    for simulator in simulators
]


@pytest.mark.parametrize(
    ("relative", "name", "simulator"),
    CASES,
    ids=[f"{simulator}-{name or Path(relative).stem}" for relative, name, simulator in CASES],
)
def test_an_example_passes_on_each_simulator_it_is_shown_with(
    relative, name, simulator, tmp_path, monkeypatch
):
    design = next(d for r, n, d in DESIGNS if (r, n) == (relative, name))
    _require_tools(simulator, design)
    path = EXAMPLES_DIR / relative
    monkeypatch.chdir(path.parent)
    # a project is found in the directory, as the documentation shows
    which = ["--design-name", name] if name else [path.name]
    arguments = ["run", simulator, *which, "--run-root", str(tmp_path / "xeda_run"), "--json"]
    result = CliRunner().invoke(cli, arguments)
    document = json.loads(result.stdout)
    assert result.exit_code == 0 and document["outcome"] == "passed", document["error"]
