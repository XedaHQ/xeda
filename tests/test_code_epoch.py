"""A process records and reuses only results of the code that it imported (`code_epoch`).

A process that imported Xeda runs that code until it ends, also after the files change on disk.
So every entry that plans or launches checks the files first and refuses with `stale_runtime`, a
launch checks them again before it writes a trace or accepts a reuse, and only a new process runs
the new code.

The oracles run in child interpreters on a copy of the `xeda` package under `tmp_path`, which is
first on `sys.path` of the child. Two versions of the copy (A and B) differ in a sentinel module
whose text a plugin flow writes. Nothing patches the check, except in the self-test, which turns it
off in the child to show that the oracle catches the stale reuse.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from xeda._internal import cli, workspace
from xeda._internal.flow_runner import default_runner
from xeda._internal.flow_runner.dse import dse_runner
from xeda._internal.package_paths import IMPL
from xeda._internal.remote import client, launch_entry

from .source_layout import SRC_XEDA, layout_script

pytestmark = pytest.mark.python_compat

# ---------------------------------------------------------------- every entry checks the code

#: The public methods of the entry classes that neither plan nor launch.
NOT_LAUNCHING = {
    "Workspace": {"load_record", "records", "scrub"},
    "FlowLauncher": {"run_root", "launch_id", "xeda_run_dir", "run_path_of", "get_flow_run_path"},
    "Dse": set(),
}

#: The functions that check the code before the first call of one of the names given: the
#: commands of the CLI before they read the project, the remote client before it plans, and the
#: agent's launch before it plans.
CHECKED_BEFORE = [
    (cli.run, "_selected_project"),
    (cli.dse, "load_optimizer_class"),
    (client._run, "_plan"),
    (launch_entry.execute, "parse_request"),
]


def _function(function: object) -> ast.FunctionDef:
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(function)))  # type: ignore[arg-type]
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _is_check(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "verify"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "code_epoch"
    )


def _first_statement(function: ast.FunctionDef) -> ast.stmt:
    body = function.body
    has_docstring = isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
    return body[1] if has_docstring else body[0]


@pytest.mark.parametrize(
    "cls",
    [workspace.Workspace, default_runner.FlowLauncher, dse_runner.Dse],
    ids=lambda c: c.__name__,
)
def test_every_public_entry_that_plans_or_launches_checks_the_code_first(cls):
    """A new public method fails here until it checks the code or is listed as not launching."""
    unchecked = []
    for name, value in vars(cls).items():
        if name.startswith("_") or not inspect.isfunction(value):
            continue
        if name in NOT_LAUNCHING[cls.__name__]:
            continue
        first = _first_statement(_function(value))
        if not (isinstance(first, ast.Expr) and _is_check(first.value)):
            unchecked.append(name)
    assert unchecked == [], f"{cls.__name__} entries without `code_epoch.verify()` first"


@pytest.mark.parametrize(
    "function, before", CHECKED_BEFORE, ids=["cli.run", "cli.dse", "client._run", "execute"]
)
def test_the_cli_and_the_remote_entries_check_the_code_before_they_plan(function, before):
    node = _function(getattr(function, "callback", function))
    checks = [n.lineno for n in ast.walk(node) if _is_check(n)]
    calls = [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == before
    ]
    assert checks and calls and min(checks) < min(calls)


# ---------------------------------------------------------------- the oracles, in child processes

SENTINEL = "sentinel_text"

PLUGIN = layout_script("""
    import subprocess
    import sys
    from typing import ClassVar

    from xeda.flow import Flow
    from xeda._internal import sentinel_text


    class SentinelToy(Flow):
        '''Writes the text of the sentinel module of Xeda.'''

        results_description: ClassVar[dict[str, str]] = {}
        design_source_types = frozenset()

        def run(self) -> None:
            (self.run_path / "out.txt").write_text(sentinel_text.TEXT)


    class BarrierToy(Flow):
        '''Runs a tool that waits for the test to change the code, then writes the sentinel.'''

        results_description: ClassVar[dict[str, str]] = {}
        design_source_types = frozenset()

        def run(self) -> None:
            tool = "import sys; print('started', flush=True); sys.stdin.readline()"
            subprocess.run([sys.executable, "-c", tool], cwd=self.run_path, check=True)
            (self.run_path / "out.txt").write_text(sentinel_text.TEXT)
""")

#: The start of every child: the copy of the package comes first on `sys.path`.
PRELUDE = layout_script("""
    import contextlib, io, json, sys
    from pathlib import Path

    code, plugin, work = sys.argv[1:4]
    sys.path[:0] = [code, plugin]
    import xeda
    assert Path(xeda.__file__).is_relative_to(code), xeda.__file__
    from xeda import Design, Spec, Workspace
    from xeda.errors import StaleRuntimeError
    from xeda.flow_runner import DefaultRunner
    import sentinel_flow

    work = Path(work)
    design = Design(name="toy", rtl={"sources": ["a.v"], "top": "a"}, root=work)


    def launch(flow, run_root):
        result = Workspace(run_root).run(Spec(flow, design))
        node = result.nodes[flow]
        out = node.run_dir / "out.txt"
        return {
            "outcome": str(result.outcome),
            "disposition": str(node.disposition),
            "code": None if node.error is None else node.error.code,
            "out": out.read_text() if out.is_file() else None,
            "trace": (node.run_dir / "trace.json").is_file(),
        }


    def tell(document):
        print(json.dumps(document), flush=True)
""")

#: A process that lives on while its code changes: it launches, waits, and tries every entry.
STALE = PRELUDE + layout_script("""
    run_root = work / "xeda_run"
    spec = Spec("sentinel_toy", design)
    first, second = launch("sentinel_toy", run_root), launch("sentinel_toy", run_root)
    plan = Workspace(run_root).plan(spec)
    tell({"first": first, "second": second})
    mode = sys.stdin.readline().strip()  # the test changes the copy now, or not
    if mode == "disabled":
        from xeda._internal import code_epoch

        # The check is off: the record is taken again from the files on disk.
        code_epoch._XEDA = code_epoch.CodeRoot(code_epoch._XEDA.label, code_epoch._XEDA._list_files)
        tell({"after": launch("sentinel_toy", run_root)})
        sys.exit(0)
    import importlib
    from xeda._internal import code_epoch

    importlib.reload(code_epoch)  # a reload keeps the record that the process took first
    importlib.reload(xeda)
    refusals = {}


    def attempt(name, call):
        try:
            call()
        except StaleRuntimeError as error:
            refusals[name] = error.code
        else:
            refusals[name] = None


    workspace = Workspace(run_root)
    attempt("Workspace.plan", lambda: workspace.plan(spec))
    attempt("Workspace.run", lambda: workspace.run(spec))
    attempt("Workspace.run(plan)", lambda: workspace.run(plan))
    attempt("Workspace.sweep", lambda: workspace.sweep([spec]))
    attempt("xeda.run", lambda: xeda.run(spec, run_root=run_root))
    attempt("xeda.plan", lambda: xeda.plan(spec, run_root=run_root))
    attempt(
        "FlowLauncher.launch_flow",
        lambda: DefaultRunner(run_root, display_results=False).launch_flow(
            sentinel_flow.SentinelToy, design, {}
        ),
    )
    from xeda.cli import cli

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        try:
            cli.main(
                ["run", "sentinel_toy", str(work / "design.yaml"), "--run-root", str(run_root),
                 "--no-config", "--json"],
                standalone_mode=True,
            )
        except SystemExit as end:
            status = end.code
    document = json.loads(stdout.getvalue())
    tell({"refusals": refusals, "cli": [status, document["outcome"], document["error"]]})
""")

#: A new process: it imports the code that is on disk now and launches once.
FRESH = PRELUDE + layout_script("""
    tell(launch("sentinel_toy", Path(sys.argv[4])))
""")

#: A process whose code changes while its node runs: the tool of the node waits for the change.
MID_LAUNCH = PRELUDE + layout_script("""
    tell(launch("barrier_toy", work / "xeda_run"))
""")

#: A sweep in worker processes: the test changes the code of the flow after the sweep planned and
#: before any worker starts, while the sweep tells its first event.
SWEEP = PRELUDE + layout_script("""
    from xeda._internal import sweep as sweep_module

    run_root = work / "xeda_run"
    moment = sys.argv[4]  # where the sweep waits for the test: "started" or "generated"
    told = []


    def wait(where):
        if where == moment:
            tell({"at": where})
            sys.stdin.readline()


    def on_event(event):
        if not told:  # the first event, before the sweep generates
            told.append(event)
            wait("started")


    real_generate = sweep_module.generate_points


    def generate_then_wait(*args, **kwargs):
        errors = real_generate(*args, **kwargs)
        wait("generated")  # after the last check of the parent, before any worker starts
        return errors


    sweep_module.generate_points = generate_then_wait
    swept = Workspace(run_root).sweep(
        [Spec("sentinel_toy", design)], max_workers=2, on_event=on_event
    )
    (point,) = swept.points
    error = point.result.nodes["sentinel_toy"].error or point.result.error
    tell({
        "outcome": str(point.result.outcome),
        "code": None if error is None else error.code,
        "message": None if error is None else error.message,
        "traces": [str(path) for path in run_root.rglob("trace.json")],
    })
""")

#: A process that plans, and keeps the plan as a pickle and as a document.
PLAN = PRELUDE + layout_script("""
    import pickle

    plan = Workspace(work / "xeda_run").plan(Spec("sentinel_toy", design))
    (work / "plan.pickle").write_bytes(pickle.dumps(plan))
    (work / "plan.json").write_text(json.dumps(plan.to_dict()))
    tell({"planned": True})
""")

#: A new process that runs the plans of `PLAN`, and then one of its own.
RUN_PLAN = PRELUDE + layout_script("""
    import pickle
    from xeda.plan import plan_from_dict

    run_root = work / "xeda_run"
    workspace = Workspace(run_root)
    spec = Spec("sentinel_toy", design)
    document = json.loads((work / "plan.json").read_text())
    plans = {
        "pickle": pickle.loads((work / "plan.pickle").read_bytes()),
        "document": plan_from_dict(document, spec=spec, workspace=None),
    }
    refusals = {}
    for name, plan in plans.items():
        try:
            refusals[name] = str(workspace.run(plan).outcome)
        except StaleRuntimeError as error:
            refusals[name] = [error.code, error.message]
    traces = [str(path) for path in run_root.rglob("trace.json")]
    own = workspace.run(workspace.plan(spec))
    tell({"refusals": refusals, "traces": traces, "own": str(own.outcome)})
""")


class Copy:
    """A copy of the `xeda` package with a sentinel module, a plugin flow, and a design."""

    def __init__(self, tmp_path: Path) -> None:
        self.work = tmp_path
        self.code = tmp_path / "code"
        shutil.copytree(
            SRC_XEDA, self.code / "xeda", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        self.sentinel = self.code.joinpath(*IMPL.split("."), f"{SENTINEL}.py")
        self.version(A)
        self.plugin = tmp_path / "plugin"
        self.plugin.mkdir()
        (self.plugin / "sentinel_flow.py").write_text(textwrap.dedent(PLUGIN))
        (tmp_path / "a.v").write_text("module a; endmodule\n")
        (tmp_path / "design.yaml").write_text("name: toy\nrtl: {sources: [a.v], top: a}\n")

    def version(self, text: str) -> None:
        self.sentinel.write_text(f"TEXT = {text!r}\n")

    def start(self, script: str, *extra: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(script), str(self.code), str(self.plugin)]
            + [str(self.work), *extra],
            cwd=self.work,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def read(self, child: subprocess.Popen[str]) -> dict:
        assert child.stdout is not None
        line = child.stdout.readline()
        if not line:
            _, stderr = child.communicate()
            raise AssertionError(f"the child ended without a result:\n{stderr}")
        return json.loads(line)

    def finish(self, child: subprocess.Popen[str], answer: str | None = None) -> list[dict]:
        stdout, stderr = child.communicate(None if answer is None else answer + "\n", timeout=300)
        assert child.returncode == 0, stderr
        return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]

    def fresh(self, run_root: Path) -> dict:
        (document,) = self.finish(self.start(FRESH, str(run_root)))
        return document

    def run_ids(self) -> dict[Path, str]:
        return {p: json.loads(p.read_text())["run_id"] for p in self.traces()}

    def traces(self) -> dict[Path, bytes]:
        return {p: p.read_bytes() for p in (self.work / "xeda_run").rglob("trace.json")}


#: The two versions. They have other sizes: Python trusts bytecode by size and mtime in seconds.
A, B = "A", "BB"


def test_a_stale_process_refuses_every_entry_and_a_new_one_runs_the_new_code(tmp_path):
    copy = Copy(tmp_path)
    child = copy.start(STALE)
    started = copy.read(child)
    assert started["first"]["out"] == A and started["first"]["disposition"] == "ran"
    assert started["second"]["disposition"] == "reused"
    traces = copy.traces()
    copy.version(B)
    (done,) = copy.finish(child, "check")
    assert done["refusals"] == dict.fromkeys(done["refusals"], "stale_runtime")
    assert len(done["refusals"]) == 7
    status, outcome, error = done["cli"]
    assert (status, outcome, error["code"]) == (2, None, "stale_runtime")
    assert "Restart the Python process" in error["message"]
    assert copy.traces() == traces, "a refusal writes no trace"
    # A new process runs B on the same run root, as a new process on a clean one does.
    again, control = copy.fresh(tmp_path / "xeda_run"), copy.fresh(tmp_path / "control")
    assert again["disposition"] == "ran" and again["out"] == B
    assert again["out"] == control["out"]
    assert copy.fresh(tmp_path / "xeda_run")["disposition"] == "reused"


def test_the_oracle_sees_the_stale_reuse_when_the_check_is_off(tmp_path):
    """The self-test of the oracle above: without the check, the stale process records the
    output of A under the digest of B, and a new B process reuses it."""
    copy = Copy(tmp_path)
    child = copy.start(STALE)
    copy.read(child)
    copy.version(B)
    (done,) = copy.finish(child, "disabled")
    assert done["after"]["disposition"] == "ran" and done["after"]["out"] == A
    again, control = copy.fresh(tmp_path / "xeda_run"), copy.fresh(tmp_path / "control")
    assert again["disposition"] == "reused" and again["out"] == A
    assert again["out"] != control["out"]


def test_without_a_change_every_entry_runs_and_reuses_as_before(tmp_path):
    """The negative control: the same process and entries, and no change of the code."""
    copy = Copy(tmp_path)
    child = copy.start(STALE)
    copy.read(child)
    runs = copy.run_ids()
    (done,) = copy.finish(child, "check")
    assert done["refusals"] == dict.fromkeys(done["refusals"], None)
    status, outcome, error = done["cli"]
    assert (status, outcome, error) == (0, "passed", None)
    # A reuse may refresh the records of a trace, but no launch ran the node again.
    assert copy.run_ids() == runs, "every launch reused the run"


def test_a_change_while_a_node_runs_fails_the_node_and_keeps_no_trace(tmp_path):
    copy = Copy(tmp_path)
    child = copy.start(MID_LAUNCH)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "started"  # the tool of the node waits now
    copy.version(B)
    (done,) = copy.finish(child, "go")
    assert (done["outcome"], done["code"], done["trace"]) == ("failed", "stale_runtime", False)
    (results,) = (tmp_path / "xeda_run").rglob("results.json")
    record = json.loads(results.read_text())
    assert (record["outcome"], record["error"]["code"]) == ("failed", "stale_runtime")


@pytest.mark.parametrize(
    "moment, changed",
    [("started", True), ("generated", True), ("generated", False)],
    ids=["changed before the sweep generates", "changed before a worker starts", "unchanged"],
)
def test_a_change_after_a_sweep_planned_fails_its_points_as_values(tmp_path, moment, changed):
    """The sweep checks the code when it generates, and a worker compares the code that it
    imports when it starts with the code that planned the sweep. Either way a change fails the
    point, and the sweep still ends with its result."""
    copy = Copy(tmp_path)
    child = copy.start(SWEEP, moment)
    assert copy.read(child) == {"at": moment}  # no worker has started yet
    if changed:
        flow = copy.plugin / "sentinel_flow.py"
        flow.write_text(flow.read_text() + "# B\n")
    (done,) = copy.finish(child, "go")
    if not changed:
        assert (done["outcome"], done["code"]) == ("passed", None)
        return
    assert (done["outcome"], done["code"], done["traces"]) == ("failed", "stale_runtime", [])
    assert "Plan again" not in done["message"]
    if moment == "generated":  # the worker found it
        assert "the flow `sentinel_toy` differs" in done["message"]


@pytest.mark.parametrize("changed", [True, False], ids=["changed", "unchanged"])
def test_a_plan_made_by_other_code_is_refused_before_its_launch_starts(tmp_path, changed):
    copy = Copy(tmp_path)
    assert copy.finish(copy.start(PLAN)) == [{"planned": True}]
    if changed:
        copy.version(B)
    (done,) = copy.finish(copy.start(RUN_PLAN))
    if changed:
        for code, message in done["refusals"].values():
            assert code == "stale_runtime" and "the code of Xeda differs" in message
        assert done["traces"] == [], "a refused plan runs nothing"
    else:
        assert done["refusals"] == {"pickle": "passed", "document": "passed"}
    assert done["own"] == "passed"


# ---------------------------------------------------------------- the code of the error


def test_a_failure_while_the_code_of_xeda_changed_is_stale_runtime(monkeypatch):
    """A refusal keeps its code, and any other failure checks the code first: the old code may
    have failed on the new files, so the cause is the change, not a bug."""
    from xeda.errors import StaleRuntimeError
    from xeda._internal import code_epoch
    from xeda._internal.flow_runner.error_codes import error_info, public_error

    stale = StaleRuntimeError("Xeda's code changed", code="stale_runtime")
    assert error_info(stale, node="n").code == "stale_runtime"
    assert public_error(stale) is stale
    assert error_info(RuntimeError("a bug"), node="n").code == "internal_error"

    def changed() -> None:
        raise stale

    monkeypatch.setattr(code_epoch, "verify", changed)
    info = error_info(RuntimeError("a bug"), node="n")
    assert info.code == "stale_runtime" and "a bug" in info.message
    assert public_error(RuntimeError("a bug")).code == "stale_runtime"
