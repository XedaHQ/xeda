"""xeda's space: a run root xeda created and marked (D21, rule R1). A directory that holds files
and no marker is not xeda's, and is refused before anything runs."""

import json
import logging
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow_runner import DefaultRunner
from xeda.run_root import (
    CACHEDIR_TAG,
    RUN_ROOT_MARKER,
    RunRootError,
    ensure_run_root,
    is_run_root,
)

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"


def _entries(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def test_a_new_run_root_is_created_and_marked(tmp_path):
    root = DefaultRunner(tmp_path / "runs").run_root
    assert root == (tmp_path / "runs").resolve()
    assert is_run_root(root)
    marker = json.loads((root / RUN_ROOT_MARKER).read_text())
    assert marker["format"] == 1 and marker["created_by"].startswith("xeda ")
    assert (root / ".gitignore").read_text().splitlines()[-1] == "*"
    assert (root / "CACHEDIR.TAG").read_text() == CACHEDIR_TAG
    assert CACHEDIR_TAG.startswith("Signature: 8a477f597d28d172789f06886806bc55")


def test_an_empty_directory_becomes_a_run_root(tmp_path):
    (tmp_path / "runs").mkdir()
    assert is_run_root(DefaultRunner(tmp_path / "runs").run_root)


def test_a_marked_run_root_is_used_as_it_is(tmp_path):
    root = DefaultRunner(tmp_path / "runs").run_root
    (root / "d" / "f").mkdir(parents=True)
    (root / ".gitignore").write_text("*\n# edited\n")
    before = _entries(root)
    DefaultRunner(root)
    assert _entries(root) == before


def test_a_touched_marker_hands_a_directory_to_xeda(tmp_path):
    runs = tmp_path / "runs"
    (runs / "old").mkdir(parents=True)
    (runs / RUN_ROOT_MARKER).touch()
    assert DefaultRunner(runs).run_root == runs.resolve()


@pytest.mark.parametrize("where", ["elsewhere", "start"])
def test_a_directory_holding_files_is_refused_and_left_alone(tmp_path, monkeypatch, where):
    """Named explicitly -- a directory of the user's, or the start directory itself."""
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    mine = start if where == "start" else tmp_path / "mine"
    mine.mkdir(exist_ok=True)
    (mine / "notes.txt").write_text("mine\n")
    before = _entries(mine)
    with pytest.raises(RunRootError, match=f"no {RUN_ROOT_MARKER}"):
        DefaultRunner(mine)
    assert _entries(mine) == before


def test_an_earlier_xedas_default_run_root_is_adopted(tmp_path, monkeypatch, caplog):
    """Only `./xeda_run` at the start directory, the default an earlier xeda made."""
    (tmp_path / "xeda_run" / "sqrt" / "vivado_synth").mkdir(parents=True)
    (tmp_path / "xeda_run" / "sqrt" / "vivado_synth" / "results.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO):
        root = DefaultRunner().run_root
    assert root == (tmp_path / "xeda_run").resolve() and is_run_root(root)
    assert (root / "sqrt" / "vivado_synth" / "results.json").read_text() == "{}"
    assert "keep nothing of yours in it" in caplog.text


@pytest.mark.parametrize("name", ["xeda_run_dse", "xeda_run_fmax_optimizer", "runs"])
def test_any_other_unmarked_run_root_holding_files_is_refused(tmp_path, monkeypatch, name):
    """Beside the default, at the start directory too, and whether named by an option, the
    environment or the API: refused, naming the directory and the fix."""
    (tmp_path / name / "sqrt").mkdir(parents=True)
    (tmp_path / name / "sqrt" / "results.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RunRootError) as refused:
        DefaultRunner(tmp_path / name)
    message = str(refused.value)
    assert str((tmp_path / name).resolve()) in message and "--run-root" in message
    assert RUN_ROOT_MARKER in message
    assert not (tmp_path / name / RUN_ROOT_MARKER).exists()


def test_an_unmarked_xeda_run_elsewhere_is_not_adopted(tmp_path, monkeypatch):
    (tmp_path / "other" / "xeda_run" / "old").mkdir(parents=True)
    (tmp_path / "start").mkdir()
    monkeypatch.chdir(tmp_path / "start")
    with pytest.raises(RunRootError):
        ensure_run_root(tmp_path / "other" / "xeda_run")


def test_a_file_is_no_run_root(tmp_path):
    (tmp_path / "runs").write_text("a file\n")
    with pytest.raises(RunRootError, match="is not a directory"):
        ensure_run_root(tmp_path / "runs")


def test_an_absent_root_is_not_created_when_asked_not_to(tmp_path):
    assert ensure_run_root(tmp_path / "runs", create=False) is None
    assert not (tmp_path / "runs").exists()


@pytest.fixture
def sqrt(tmp_path, monkeypatch):
    """A copy of the example design; xeda started in its directory, with the fake tools."""
    work = tmp_path / "work"
    shutil.copytree(SQRT, work, ignore=shutil.ignore_patterns("xeda_run*"))
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(work)
    return work


def test_a_run_adds_only_its_run_root_to_the_start_directory(sqrt):
    before = sorted(p.name for p in sqrt.iterdir())
    result = CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "sqrt.toml", "-s", "fpga.part=xc7a12tcsg325-1", "--json"],
        catch_exceptions=False,
    )
    assert json.loads(result.stdout)["success"], result.stdout
    assert sorted(p.name for p in sqrt.iterdir()) == sorted([*before, "xeda_run"])
    assert is_run_root(sqrt / "xeda_run")


@pytest.mark.parametrize("command", ["run", "dse", "scrub"])
def test_an_unmarked_run_root_is_refused_with_a_json_document(sqrt, tmp_path, command):
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.txt").write_text("mine\n")
    args = {
        "run": ["run", "vivado_synth", "sqrt.toml", "-s", "fpga.part=xc7a12tcsg325-1"],
        "dse": [
            "dse",
            "vivado_synth",
            "--design",
            "sqrt.toml",
            "--init-freq-low",
            "100",
            "--init-freq-high",
            "200",
        ],
        "scrub": ["scrub", "vivado_synth", "sqrt"],
    }[command]
    result = CliRunner().invoke(cli, [*args, "--run-root", str(mine), "--json"])
    document = json.loads(result.stdout)
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "RunRootError"
    assert sorted(p.name for p in mine.iterdir()) == ["notes.txt"]


@pytest.mark.parametrize("remote", [False, True], ids=["local", "remote"])
def test_a_run_that_fails_at_input_creates_no_run_root(sqrt, remote):
    """A design that does not load (here, a name that is no directory name) fails before any
    flow launches: the run root is created when a flow first needs it, so none is left behind."""
    toml = (sqrt / "sqrt.toml").read_text().replace('name = "sqrt"', 'name = ".."')
    (sqrt / "sqrt.toml").write_text(toml)
    before = sorted(p.name for p in sqrt.iterdir())
    extra = ["--remote", "nowhere.invalid"] if remote else []
    result = CliRunner().invoke(cli, ["run", "vivado_synth", "sqrt.toml", *extra, "--json"])
    document = json.loads(result.stdout)
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["type"] == "DesignValidationError", document
    assert sorted(p.name for p in sqrt.iterdir()) == before
