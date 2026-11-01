"""The tracked notebooks run as tests.

Each notebook runs in a kernel whose working directory is a temporary directory, and finds the
examples through the `XEDA_EXAMPLES` variable. A notebook writes only under its work directory,
`notebook-output/`, and it is committed without outputs.
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("nbclient")
import nbformat  # noqa: E402

from . import tool_utils  # noqa: E402
from .notebook_harness import cell_output, run_notebook  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NB = ROOT / "notebooks"
NOTEBOOKS = sorted(NB.glob("[0-9][0-9]-*.ipynb"))
EXAMPLES_ENV = {"XEDA_EXAMPLES": str(ROOT / "examples")}

# The tools that run each notebook, by notebook name.
NOTEBOOK_PROBES: dict[str, tuple] = {}

# The probe of each flow that a notebook may launch, and of the cocotb testbenches.
FLOW_PROBES = {
    "ghdl_sim": tool_utils.require_ghdl,
    "verilator": tool_utils.require_verilator,
    "yosys_fpga": tool_utils.require_yosys,
    "nextpnr": tool_utils.require_nextpnr_ecp5,
    "cocotb": tool_utils.require_cocotb,
}

# Public names that `xeda` does not export yet. The set only shrinks: the test below fails when a
# name in it is exported, so the entry goes.
NOT_EXPORTED_YET = {"plan", "run", "sweep", "load", "records", "scrub", "local_settings"}

PUBLIC_NAMES = """
Design DesignPart Rtl Testbench Source Project Spec LocalSettings local_settings
Plan PlanNode PlanInput PlanDelivery
Workspace Remote CancelToken Event plan run sweep load records scrub
Result NodeResult RunRecord OutputRecord InputRecord InputSource DeliveryRecord
MetricInfo ErrorInfo Outcome Disposition OpenRun
SweepResult SweepPoint RunListing RecordProblem ScrubResult
UNSET Unset __version__
""".split()


def test_the_public_surface_exists():
    import xeda
    import xeda.errors
    import xeda.introspect  # noqa: F401

    assert set(PUBLIC_NAMES) - NOT_EXPORTED_YET <= set(xeda.__all__)
    assert not NOT_EXPORTED_YET & set(xeda.__all__)


def _run(name, tmp_path):
    for probe in NOTEBOOK_PROBES[name]:
        probe()
    return run_notebook(NB / f"{name}.ipynb", cwd=tmp_path, env=EXAMPLES_ENV)


@pytest.mark.parametrize("name", [p.stem for p in NOTEBOOKS])
def test_every_notebook_writes_only_under_its_work_directory(tmp_path, name):
    _run(name, tmp_path)
    work = tmp_path / "notebook-output"
    assert all(p == work or p.is_relative_to(work) for p in tmp_path.rglob("*"))


def _flow_names(notebook: Path) -> set[str]:
    """The flow names that the `Spec(...)` calls of the code cells of `notebook` give as text."""
    names: set[str] = set()
    for cell in nbformat.read(notebook, as_version=4).cells:
        if cell.cell_type != "code":
            continue
        for node in ast.walk(ast.parse(cell.source)):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", "")) == "Spec"
            ):
                continue
            given = node.args[:1] + [k.value for k in node.keywords if k.arg == "flow"]
            for arg in given:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.update(re.split(r"[+\s]+", arg.value.strip()))
    return names


@pytest.mark.parametrize("name", [p.stem for p in NOTEBOOKS])
def test_the_probes_cover_the_flows_of_each_notebook(name):
    unknown = {flow for flow in _flow_names(NB / f"{name}.ipynb") if flow not in FLOW_PROBES}
    assert not unknown, f"{name} launches flows without a probe: {sorted(unknown)}"
    for flow in _flow_names(NB / f"{name}.ipynb"):
        assert FLOW_PROBES[flow] in NOTEBOOK_PROBES[name], (name, flow)


def _tracked(paths):
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "notebooks"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [p for p in paths if p.relative_to(ROOT).as_posix() in listed]


def test_tracked_notebooks_have_no_outputs():
    for path in _tracked(NOTEBOOKS):
        nb = nbformat.read(path, as_version=4)
        for cell in nb.cells:
            if cell.cell_type == "code":
                assert cell.outputs == [] and cell.execution_count is None, path
        assert "widgets" not in nb.metadata, path


def _notebook(path: Path, *cells) -> Path:
    nb = nbformat.v4.new_notebook(
        cells=[nbformat.v4.new_code_cell(s, metadata={"tags": t}) for s, t in cells]
    )
    path.write_text(nbformat.writes(nb))
    return path


def test_the_harness_runs_a_notebook_in_its_directory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    path = _notebook(
        tmp_path / "nb.ipynb",
        ("from pathlib import Path; print(Path.cwd()); _ = Path('x').write_text('1')", ["cwd"]),
        ("import os; print(os.environ['XEDA_EXAMPLES'])", ["examples"]),
    )
    nb = run_notebook(path, cwd=work, env={"XEDA_EXAMPLES": "somewhere"})
    assert cell_output(nb, "cwd") == str(work.resolve())
    assert (work / "x").read_text() == "1"
    assert cell_output(nb, "examples") == "somewhere"
    assert [p.name for p in tmp_path.iterdir()] == ["nb.ipynb", "work"]


def test_the_harness_reports_a_failed_cell(tmp_path):
    from nbclient.exceptions import CellExecutionError

    path = _notebook(tmp_path / "nb.ipynb", ("raise ValueError('no')", []))
    with pytest.raises(CellExecutionError, match="no"):
        run_notebook(path, cwd=tmp_path, env={})


def test_the_harness_rejects_an_unknown_tag(tmp_path):
    path = _notebook(tmp_path / "nb.ipynb", ("print(1)", ["one"]))
    nb = run_notebook(path, cwd=tmp_path, env={})
    with pytest.raises(KeyError, match="two"):
        cell_output(nb, "two")
