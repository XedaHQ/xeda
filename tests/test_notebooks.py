"""The tracked notebooks run as tests.

Each notebook runs in a kernel whose working directory is a temporary directory, and finds the
examples through the `XEDA_EXAMPLES` variable. A notebook writes only under its work directory,
`notebook-output/`, and it is committed without outputs.
"""

import ast
import csv
import re
import subprocess
import sys
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
NOTEBOOK_PROBES: dict[str, tuple] = {
    "01-quickstart": (
        tool_utils.require_verilator,
        tool_utils.require_yosys,
        tool_utils.require_nextpnr_ecp5,
    ),
    "02-simulation": (tool_utils.require_verilator,),
    "03-sweep": (
        tool_utils.require_yosys,
        tool_utils.require_yosys_ghdl_plugin,
        tool_utils.require_nextpnr_ecp5,
    ),
    "04-plan": (tool_utils.require_yosys, tool_utils.require_nextpnr_ecp5),
    "05-generated": (tool_utils.require_yosys, tool_utils.require_nextpnr_ecp5),
    "06-qor-history": (tool_utils.require_yosys, tool_utils.require_nextpnr_ecp5),
}

# The probe of each flow that a notebook may launch, and of the cocotb testbenches.
FLOW_PROBES = {
    "ghdl_sim": tool_utils.require_ghdl,
    "verilator": tool_utils.require_verilator,
    "yosys_fpga": tool_utils.require_yosys,
    "nextpnr": tool_utils.require_nextpnr_ecp5,
    "cocotb": tool_utils.require_cocotb,
}

PUBLIC_NAMES = """
Design DesignPart Rtl Testbench Source Project Spec LocalSettings local_settings
Plan PlanNode PlanInput PlanDelivery
Workspace Remote CancelToken Event plan run sweep load_record records scrub
Result NodeResult RunRecord OutputRecord InputRecord InputSource DeliveryRecord
MetricInfo ErrorInfo Outcome Disposition OpenRun
SweepResult SweepPoint RunListing RecordProblem ScrubResult
UNSET Unset __version__
""".split()


def test_the_public_surface_exists():
    import xeda
    import xeda.errors
    import xeda.introspect  # noqa: F401

    assert set(PUBLIC_NAMES) <= set(xeda.__all__)


def _run(name, tmp_path, env=EXAMPLES_ENV):
    for probe in NOTEBOOK_PROBES[name]:
        probe()
    return run_notebook(NB / f"{name}.ipynb", cwd=tmp_path, env=env)


@pytest.fixture(scope="module")
def executions(tmp_path_factory):
    """Run a notebook twice in one directory, once for the whole module, and give the work
    directory and both executed notebooks. The second run finds the run directories of the first."""
    done: dict[str, tuple] = {}

    def get(name):
        if name not in done:
            work = tmp_path_factory.mktemp(name)
            done[name] = (work, _run(name, work), _run(name, work))
        return done[name]

    return get


@pytest.mark.parametrize("name", [p.stem for p in NOTEBOOKS])
def test_every_notebook_writes_only_under_its_work_directory(executions, name):
    work, _, _ = executions(name)
    output = work / "notebook-output"
    assert all(p == output or p.is_relative_to(output) for p in work.rglob("*"))


def test_the_quickstart_passes_and_reuses_its_runs(executions):
    _, first, second = executions("01-quickstart")
    for nb in (first, second):
        assert cell_output(nb, "passed-outcome") == "passed True"
        assert cell_output(nb, "synth-outcome") == "passed True"
        assert cell_output(nb, "reused") == "reused reused"


SIMULATION_OUTCOMES = {
    "passed-outcome": "passed True",
    "rejected-outcome": "rejected rejected",
    "failed-outcome": "failed nonzero_exit",
    "timeout-outcome": "failed timeout",
    "canceled-outcome": "canceled canceled None",
    "replaced-run": "run_replaced",
    "hashed-runs": "still open: True",
    "design-error": "design_file_not_found",
}


def test_the_simulation_notebook_shows_each_way_to_end(executions):
    # The second run finds a rejected run to reuse, and still stops the endless testbench twice.
    _, first, second = executions("02-simulation")
    for nb in (first, second):
        assert {tag: cell_output(nb, tag) for tag in SIMULATION_OUTCOMES} == SIMULATION_OUTCOMES


def test_a_notebook_without_the_examples_says_what_to_set(tmp_path):
    from nbclient.exceptions import CellExecutionError

    with pytest.raises(CellExecutionError, match="XEDA_EXAMPLES"):
        _run("01-quickstart", tmp_path, env={"XEDA_EXAMPLES": str(tmp_path / "missing")})
    assert not (tmp_path / "notebook-output").exists()


def _writes_environment(source: str) -> bool:
    """Whether the code writes `os.environ` or calls `os.putenv` or `os.unsetenv`."""

    def is_environ(node):
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Subscript) and is_environ(node.value):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if is_environ(node.func.value) and node.func.attr in {
                "update",
                "setdefault",
                "pop",
                "popitem",
                "clear",
            }:
                return True
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in {"putenv", "unsetenv"}
            ):
                return True
    return False


@pytest.mark.parametrize("name", [p.stem for p in NOTEBOOKS])
def test_no_notebook_writes_the_environment(name):
    for cell in nbformat.read(NB / f"{name}.ipynb", as_version=4).cells:
        if cell.cell_type == "code":
            assert not _writes_environment(cell.source), cell.source


def test_the_environment_check_finds_a_write():
    assert _writes_environment("import os\nos.environ['A'] = '1'")
    assert _writes_environment("import os\nos.environ.update(A='1')")
    assert _writes_environment("import os\nos.putenv('A', '1')")
    assert not _writes_environment("import os\nx = os.environ.get('A', 'b')")


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


def test_the_sweep_notebook(executions):
    # The second run reuses the runs of the first, so only the first run has `shared` nodes.
    work, nb, again = executions("03-sweep")
    assert cell_output(again, "sweep-counts") == "{'passed': 8, 'failed': 1}"
    points = "(0, 1, 2, 3, 4, 5, 6, 7, 8)"
    assert cell_output(nb, "colliding-run-dirs") == f"colliding_run_dirs\n{points}"
    assert cell_output(nb, "sweep-counts") == "{'passed': 8, 'failed': 1}"
    # Four widths: the two seeds of a width share one synthesis. The 288-bit point has its own.
    assert cell_output(nb, "shared-producer") == "{'ran': 5, 'shared': 4}"
    reason = cell_output(nb, "failed-point").splitlines()
    assert reason[0] == "8 failed reported_failure"
    assert reason[1].startswith("nextpnr: timing constraints are not met")
    assert cell_output(nb, "replaced-run") == "run_replaced\nTrue\nTrue True"
    assert cell_output(nb, "empty-history").startswith("no runs yet")
    assert cell_output(nb, "missing-metric").startswith(
        "`nextpnr.fmx`: the flow `nextpnr` has no metric `fmx`. Its metrics are: fmax, "
    )
    work = work / "notebook-output"
    rows = (work / "trivium-sweep.csv").read_text().splitlines()
    assert len(rows) == 1 + 9
    # The sweep wrote one record of nextpnr for each point, in the run root of the notebook.
    assert len(list((work / "xeda_run" / "trivium").glob("nextpnr_*/results.json"))) == 9


def _generated_by(design_dir: Path, width: int, tmp_path: Path) -> bytes:
    """The file that the generator of the notebook writes for `width`, from a separate run."""
    storage = tmp_path / f"expected{width}"
    subprocess.run(
        [sys.executable, str(design_dir / "gen_counter.py"), str(width), str(storage)],
        check=True,
        cwd=tmp_path,
    )
    return (storage / "counter.v").read_bytes()


def test_the_generated_notebook_compares_variants_by_their_exact_bytes(executions, tmp_path):
    work, nb, again = executions("05-generated")
    out = work / "notebook-output"
    assert cell_output(nb, "sweep-counts") == "{'passed': 3}"
    expected = {w: _generated_by(out / "design", w, tmp_path) for w in (8, 16, 32)}
    assert len(set(expected.values())) == 3
    # What each run read: the exact bytes of its own storage, and no other variant's.
    for width, data in expected.items():
        (stored,) = (out / "xeda_run" / ".generated" / f"counter{width}").glob("*/counter.v")
        assert stored.read_bytes() == data
    # What the notebook printed for the bytes that `open()` gave.
    assert cell_output(nb, "variant-bytes").splitlines() == [
        f"counter{w} {len(d)} module counter (" for w, d in expected.items()
    ]
    # The delivery is the variant of width 16, byte for byte, and nothing else is delivered.
    assert [p.name for p in (out / "delivered").iterdir()] == ["counter.v"]
    assert (out / "delivered" / "counter.v").read_bytes() == expected[16]
    for run in (nb, again):
        assert cell_output(run, "delivered-bytes").splitlines() == ["True", "[('generator', True)]"]


def test_the_history_notebook_reads_past_runs(executions):
    work, nb, again = executions("06-qor-history")
    with (work / "notebook-output" / "history.csv").open(newline="") as file:
        rows = list(csv.DictReader(file))
    assert [(r["clocks.main_clock.period"], r["seed"]) for r in rows] == [
        ("12 ns", "1"),
        ("10 ns", "1"),
        ("8 ns", "1"),
        ("10 ns", "2"),
    ]
    assert all(
        r["outcome"] == "passed" and float(r["fmax"]) > 0 and int(r["lut"]) > 0 for r in rows
    )
    for run in (nb, again):
        assert cell_output(run, "empty-history").startswith("no runs yet")
        assert "nosuch.metric" in cell_output(run, "missing-metric")
        assert cell_output(run, "csv-rows") == "4"
    # The fourth run changes only the seed of nextpnr, so it reuses the yosys_fpga run of the
    # second. (The second execution is not checked for reuse: the harness gives each execution
    # its own IPYTHONDIR, and an inherited variable is evidence of a run, so it runs again.)
    assert cell_output(nb, "runs").splitlines()[3] == "10 ns 2 passed reused ran"


PLAN_OUTPUTS = {
    "fresh-plan": "yosys_fpga=fresh nextpnr=fresh\nsame identities: True",
    "changed-seed": "yosys_fpga fresh\nnextpnr stale settings changed: seed",
    "rebuild-plan": "[('yosys_fpga', 'stale', 'rebuild_all'), ('nextpnr', 'stale', 'rebuild_all')]\nran",
    "plan-options": "plan_options",
    "stale-plan": "stale_plan\nthe run root differs: the plan was made for",
    "canonical-quantity": "6 ns True",
    "flow-errors": "FlowNotFoundError flow_not_found\nChainError no_edge",
    "module-plan": "fresh",
}

#: The problems that each error cell prints, one line for each problem of each request.
PLAN_PROBLEMS = {
    "unscoped-setting": ["unscoped_setting node=None setting=seed"],
    "unknown-setting": ["unknown_setting node=nextpnr setting=sed"],
    "invalid-value": ["invalid_value node=nextpnr setting=seed"],
    "invalid-quantity": ["invalid_quantity node=nextpnr setting=hold_detour_max"] * 2,
    "node-and-key-errors": [
        "unknown_node node=None setting=nextpnr",
        "conflicting_keys node=None setting=nextpnr",
    ],
    "several-errors": [
        "unknown_setting node=nextpnr setting=sed",
        "invalid_value node=nextpnr setting=seed",
    ],
}


def _squeezed(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def test_the_plan_notebook_plans_runs_and_refuses(executions):
    # The second kernel has another environment, so it does not reuse the runs of the first.
    _, first, second = executions("04-plan")
    for nb in (first, second):
        for tag, text in PLAN_OUTPUTS.items():
            assert _squeezed(cell_output(nb, tag)).startswith(text), tag
        for tag, problems in PLAN_PROBLEMS.items():
            lines = cell_output(nb, tag).splitlines()
            assert [line for line in lines if line and not line[0].isspace()] == problems, tag
        assert cell_output(nb, "run-plan").startswith("passed True\nyosys_fpga  passed")
    assert cell_output(first, "first-plan").count("stale") == 2
    assert cell_output(first, "run-plan").endswith("nextpnr     passed  ran")
