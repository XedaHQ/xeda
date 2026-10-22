"""The library never prompts. An unanswered confirmation of scrub answers "no".

Only the command line asks the user, through the confirmation it passes (`ScrubConfirmation`).
A caller without a terminal (a notebook, a CI job, a pipe) removes nothing, and the command line
refuses `--scrub` with `--remote`, since the host cannot ask.
"""

import ast
import io
import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.console import console
from xeda.flow_runner import DefaultRunner, scrub_design, scrub_runs
from xeda.run_root import ensure_run_root

from .flow_registry import preserved_flow_registry
from .io_flows import _Maker

FLOW = "vivado_synth"
SRC = Path(__file__).parent.parent / "src" / "xeda"


@pytest.fixture(autouse=True)
def private_registry():
    with preserved_flow_registry():
        yield


@pytest.fixture
def no_terminal(monkeypatch):
    """Close stdin, and fail the test if anything asks on the console."""
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr("sys.stdin", closed)

    def asked(*args, **kwargs):
        raise AssertionError(f"the library asked the user: {args!r}")

    monkeypatch.setattr(console, "input", asked)


def _run_dirs(design_dir: Path) -> list[Path]:
    directories = [design_dir / FLOW, design_dir / f"{FLOW}_0123456789abcdef"]
    for directory in directories:
        directory.mkdir(parents=True)
        (directory / "results.json").write_text("{}")
    return directories


@pytest.fixture
def tree(tmp_path):
    run_root = ensure_run_root(tmp_path / "xeda_run")
    assert run_root is not None
    design_dir = run_root / "sqrt"
    return run_root, design_dir, _run_dirs(design_dir)


def _all_exist(directories) -> bool:
    return all(directory.is_dir() for directory in directories)


def test_scrub_design_without_a_confirmation_removes_nothing(tree, no_terminal, caplog):
    run_root, design_dir, directories = tree
    with caplog.at_level(logging.INFO):
        result = scrub_design(FLOW, design_dir, run_root=run_root)
    assert result.removed == []
    assert _all_exist(directories)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Not removing 2 run directories" in warnings[0].getMessage()


def test_scrub_runs_without_a_confirmation_removes_nothing(tree, no_terminal):
    run_root, design_dir, directories = tree
    assert scrub_runs(FLOW, design_dir, run_root=run_root) is False
    assert _all_exist(directories)


def test_scrub_with_nothing_to_remove_says_nothing(tmp_path, no_terminal, caplog):
    run_root = ensure_run_root(tmp_path / "xeda_run")
    assert run_root is not None
    (run_root / "sqrt").mkdir()
    with caplog.at_level(logging.INFO):
        assert scrub_design(FLOW, run_root / "sqrt", run_root=run_root).removed == []
    assert not caplog.records


def test_a_confirmation_that_says_yes_removes(tree):
    run_root, design_dir, directories = tree
    asked = []

    def confirm(listed):
        asked.append(sorted(listed))
        return True

    result = scrub_design(FLOW, design_dir, run_root=run_root, confirm=confirm)
    assert asked == [sorted(directories)]
    assert sorted(result.removed) == sorted(directories)
    assert not any(directory.exists() for directory in directories)


def test_a_confirmation_that_says_no_removes_nothing(tree):
    run_root, design_dir, directories = tree
    asked = []
    result = scrub_design(
        FLOW, design_dir, run_root=run_root, confirm=lambda listed: asked.append(listed) or False
    )
    assert len(asked) == 1
    assert result.removed == []
    assert _all_exist(directories)


def _launch(tmp_path: Path, text: str, *, scrub_old_runs: bool, confirm=None) -> Path:
    design = tmp_path / "design"
    design.mkdir(exist_ok=True)
    (design / "d.yaml").write_text('name: d\nrtl: {sources: [], top: "t"}\n')
    launcher = DefaultRunner(
        tmp_path / "xeda_run",
        hashed_run_dirs=True,
        scrub_old_runs=scrub_old_runs,
        display_results=False,
    )
    launcher.confirm_scrub = confirm
    flow = launcher.launch_flow(_Maker, Design.from_file(design / "d.yaml"), {"text": text})
    assert flow.succeeded
    return flow.run_path


def test_a_launch_with_scrub_old_runs_and_no_confirmation_removes_nothing(
    tmp_path, no_terminal, caplog
):
    old = _launch(tmp_path, "one\n", scrub_old_runs=False)
    with caplog.at_level(logging.INFO):
        new = _launch(tmp_path, "two\n", scrub_old_runs=True)
    assert new != old and old.is_dir() and new.is_dir()
    assert sum("Not removing 1 run directory" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("answer", [True, False])
def test_a_launch_with_scrub_old_runs_follows_its_confirmation(tmp_path, answer):
    old = _launch(tmp_path, "one\n", scrub_old_runs=False)
    new = _launch(tmp_path, "two\n", scrub_old_runs=True, confirm=lambda listed: answer)
    assert new.is_dir() and old.is_dir() is (not answer)


def _invoke_scrub(run_root: Path, text: str):
    return CliRunner().invoke(
        cli, ["scrub", FLOW, "sqrt", "--run-root", str(run_root), "--json"], input=text
    )


def test_the_command_removes_on_yes(tree):
    run_root, _design_dir, directories = tree
    result = _invoke_scrub(run_root, "yes\n")
    assert json.loads(result.stdout)["success"] is True
    assert not any(directory.exists() for directory in directories)


@pytest.mark.parametrize("text", ["", "no\n"])
def test_the_command_removes_nothing_without_a_yes(tree, text):
    """No input at all (stdin at its end) is a no, not an error. The one JSON document says that
    nothing was scrubbed."""
    run_root, _design_dir, directories = tree
    result = _invoke_scrub(run_root, text)
    document = json.loads(result.stdout)
    assert document["success"] is True and document["scrubbed"] == []
    assert _all_exist(directories)


@pytest.mark.parametrize(
    "error", [ValueError("I/O operation on closed file"), OSError("bad file descriptor")], ids=repr
)
def test_the_command_removes_nothing_when_the_input_cannot_be_read(tree, monkeypatch, error):
    """A closed or broken standard input is a no, as an input at its end is."""
    run_root, _design_dir, directories = tree

    def unreadable(*args, **kwargs):
        raise error

    monkeypatch.setattr(console, "input", unreadable)
    result = _invoke_scrub(run_root, "")
    document = json.loads(result.stdout)
    assert document["success"] is True and document["scrubbed"] == []
    assert _all_exist(directories)


class _ClosedInput(io.BytesIO):
    """A binary input that was closed after click took it: any read raises `ValueError`, as a
    read of a closed file does."""

    def read(self, size=-1):
        if size == 0:  # click probes a stream by reading no bytes
            return b""
        raise ValueError("I/O operation on closed file")

    read1 = readline = readinto = read


def test_the_command_removes_nothing_when_standard_input_is_closed(tree):
    run_root, _design_dir, directories = tree
    result = CliRunner().invoke(
        cli,
        ["scrub", FLOW, "sqrt", "--run-root", str(run_root), "--json"],
        input=_ClosedInput(),
    )
    document = json.loads(result.stdout)
    assert document["success"] is True and document["scrubbed"] == []
    assert _all_exist(directories)


#: The modules of the command line, which are the only ones that may ask the user.
COMMAND_LINE_MODULES = {
    "cli.py",
    "cli_utils.py",
    "pdk_cli.py",
    "remote/commands.py",
}


def _prompts(path: Path) -> list[int]:
    """Return the lines of `path` that read an answer from the user."""
    tree = ast.parse(path.read_text())
    modules = {"click"}  # the names that stand for the click module
    functions = {"input", "getpass"}  # the names that ask when called
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "click"}
        elif isinstance(node, ast.ImportFrom) and node.module == "click":
            functions |= {a.asname or a.name for a in node.names if a.name in {"prompt", "confirm"}}
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        asks = False
        if isinstance(function, ast.Name):
            asks = function.id in functions
        elif isinstance(function, ast.Attribute):
            owner = function.value.id if isinstance(function.value, ast.Name) else ""
            asks = function.attr in {"input", "getpass", "ask", "readline"} or (
                owner in modules and function.attr in {"prompt", "confirm"}
            )
        if asks:
            lines.append(node.lineno)
    return lines


def test_only_the_command_line_asks_the_user():
    asking = {
        str(path.relative_to(SRC)): _prompts(path)
        for path in sorted(SRC.rglob("*.py"))
        if str(path.relative_to(SRC)) not in COMMAND_LINE_MODULES
    }
    assert {name: lines for name, lines in asking.items() if lines} == {}


def test_the_scan_sees_a_prompt(tmp_path):
    """The scan finds each way of asking, so a new one in the library does not pass."""
    source = tmp_path / "ask.py"
    source.write_text(
        "import click\n"
        "import click as c\n"
        "from click import prompt as ask\n"
        "from xeda.console import console\n"
        "def f():\n"
        "    input('a')\n"
        "    console.input('b')\n"
        "    click.confirm('c')\n"
        "    click.prompt('d')\n"
        "    Confirm.ask('e')\n"
        "    sys.stdin.readline()\n"
        "    ask('f')\n"
        "    c.confirm('g')\n"
    )
    assert sorted(_prompts(source)) == [6, 7, 8, 9, 10, 11, 12, 13]
