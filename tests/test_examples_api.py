"""The Python examples use the public API, run from any directory and fail with their run.

An example is the code a user copies. It imports only `xeda`, it finds its design beside its own
file, it writes to the directory it runs in, and its exit status says whether every point passed.
`tox -e consumer-typing` type-checks the examples that import `xeda` against the built wheel.
"""

import ast
import configparser
import re
import subprocess
import sys
from pathlib import Path

import pytest

from . import tool_utils
from .tool_utils import HANG_GUARD

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
SYNTHESIZE = EXAMPLES / "vhdl/pipeline/synthesize.py"
SQRT = EXAMPLES / "vhdl/sqrt/test.py"
REMOVED_OPTIONS = {"--design", "--flow-settings", "--dockerized", "--xeda-run-dir"}


def _scripts() -> list[Path]:
    return sorted(EXAMPLES.rglob("*.py"))


def _imports_xeda(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "xeda" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "xeda":
            return True
    return False


def test_every_example_that_imports_xeda_is_type_checked_in_the_consumer_environment() -> None:
    tox = configparser.ConfigParser(interpolation=None)
    tox.read(ROOT / "tox.ini")
    commands = tox["testenv:consumer-typing"]["commands"]
    listed = {ROOT / name for name in re.findall(r"\{toxinidir\}/(examples/\S+\.py)", commands)}
    assert listed == {path for path in _scripts() if _imports_xeda(path)}


def test_the_type_check_does_not_run_in_the_source_environment() -> None:
    tox = configparser.ConfigParser(interpolation=None)
    tox.read(ROOT / "tox.ini")
    assert "examples" not in tox["testenv:mypy"]["commands"]


@pytest.mark.parametrize("path", _scripts(), ids=lambda path: str(path.relative_to(EXAMPLES)))
def test_no_example_names_a_removed_command_line_option(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert not strings & REMOVED_OPTIONS


def _run(
    script: Path, cwd: Path, *args: str, timeout: int = HANG_GUARD
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _without_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_the_synthesis_example_runs_from_any_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_utils.use_fake_tools(monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    done = _run(SYNTHESIZE, work, "--no-docker")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "quartus: passed" in done.stdout
    assert (work / "xeda_run/pipelined_adder/quartus/results.json").is_file()


def test_the_synthesis_example_fails_when_its_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_tools(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    done = _run(SYNTHESIZE, work, "--no-docker")
    assert done.returncode == 1
    assert "executable_not_found" in done.stderr


def test_the_sqrt_example_fails_when_a_point_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_tools(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    done = _run(SQRT, work)
    assert done.returncode == 1
    assert done.stdout.count("executable_not_found") == 3  # two simulations and the synthesis


def test_the_sqrt_example_ignores_the_long_test_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_tools(tmp_path, monkeypatch)
    monkeypatch.setenv("LONG_TEST", "1")
    work = tmp_path / "work"
    work.mkdir()
    assert _run(SQRT, work).stdout.count("ghdl_sim, width") == 2


@pytest.mark.real_tool
def test_the_sqrt_example_passes_from_any_directory(tmp_path: Path) -> None:
    tool_utils.require_ghdl()
    tool_utils.require_cocotb()
    tool_utils.require_yosys_ghdl_plugin()
    work = tmp_path / "work"
    work.mkdir()
    done = _run(SQRT, work)
    assert done.returncode == 0, done.stdout + done.stderr
    points = [line for line in done.stdout.splitlines() if line.startswith(("ghdl_sim,", "yosys_"))]
    assert [line.split()[-1] for line in points] == ["passed"] * 3
    assert (work / "xeda_run/sqrt").is_dir()
