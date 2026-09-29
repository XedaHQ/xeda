"""`xeda run` options for rebuilding, and the errors that replace the removed ones."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow_runner import remote

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"


@pytest.fixture
def sqrt(tmp_path, monkeypatch):
    """The example design, read in place; runs land in `tmp_path/xeda_run`."""
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)
    return SQRT / "sqrt.toml"


def _run(*args):
    result = CliRunner().invoke(cli, ["run", "vivado_synth", *args], catch_exceptions=False)
    return result, json.loads(result.stdout) if "--json" in args else None


def test_a_second_run_reuses_by_default_and_says_so(sqrt):
    settings = ["-s", "fpga.part=xc7a12tcsg325-1"]
    _run(str(sqrt), *settings, "--json")
    result, document = _run(str(sqrt), *settings, "--json")
    assert result.exit_code == 0 and document["success"]
    assert [node["state"] for node in document["nodes"]] == ["fresh"]


def test_rebuild_all_runs_again(sqrt):
    settings = ["-s", "fpga.part=xc7a12tcsg325-1"]
    _run(str(sqrt), *settings, "--json")
    _, document = _run(str(sqrt), *settings, "--rebuild", "all", "--json")
    assert [node["state"] for node in document["nodes"]] == ["ran"]


@pytest.mark.parametrize(
    "option, replacement",
    [
        ("--cached-dependencies", "--rebuild stale"),
        ("--no-cached-dependencies", "--rebuild all"),
        ("--incremental", "run directories are always reused"),
        ("--no-incremental", "--clean"),
    ],
)
def test_removed_options_name_their_replacement(sqrt, option, replacement):
    result, document = _run(str(sqrt), option, "--json")
    assert result.exit_code != 0
    assert document["success"] is False and replacement in document["error"]["message"]


def test_clean_with_cwd_is_refused(sqrt):
    result, document = _run(str(sqrt), "--cwd", "--clean", "--json")
    assert result.exit_code != 0 and "--cwd" in document["error"]["message"]


def test_scrub_removed_incremental_names_its_replacement(sqrt):
    """`xeda scrub` dropped `--incremental/--no-incremental` the same way `run` did: run
    directories are always `<design>/<flow>`, never `<design>_<hash>/<flow>`."""
    result = CliRunner().invoke(
        cli,
        ["scrub", "vivado_synth", "sqrt", "--incremental", "--json"],
        catch_exceptions=False,
    )
    document = json.loads(result.stdout)
    assert result.exit_code != 0
    assert document["success"] is False
    assert "scrub always looks in <design>/" in document["error"]["message"]


class _RecordingRemoteRunner:
    """Stands in for `RemoteRunner`: records the launcher settings instead of connecting."""

    settings_seen: list = []

    def __init__(self, xeda_run_dir, **settings):
        self.settings_seen.append(settings)

    def run_remote(self, *args, **kwargs):
        return {"success": True, "run_path": "/remote/run"}


@pytest.mark.parametrize("given, expected", [([], "stable"), (["--run-dirs", "hashed"], "hashed")])
def test_remote_mirrors_into_the_chosen_run_directories(sqrt, monkeypatch, given, expected):
    _RecordingRemoteRunner.settings_seen = []
    monkeypatch.setattr(remote, "RemoteRunner", _RecordingRemoteRunner)
    result, document = _run(str(sqrt), "--remote", "host", *given, "--json")
    assert result.exit_code == 0 and document["success"]
    assert _RecordingRemoteRunner.settings_seen == [{"run_dirs": expected}]


@pytest.mark.parametrize("option", [["--rebuild", "all"], ["--rebuild", "stale"], ["--clean"]])
def test_remote_refuses_rebuild_options(sqrt, monkeypatch, option):
    """A remote run always runs fresh: an explicit --rebuild or --clean would be ignored."""
    _RecordingRemoteRunner.settings_seen = []
    monkeypatch.setattr(remote, "RemoteRunner", _RecordingRemoteRunner)
    result, document = _run(str(sqrt), "--remote", "host", *option, "--json")
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["message"] == (
        f"`{option[0]}` is not supported with --remote: remote runs always run fresh"
    )
    assert _RecordingRemoteRunner.settings_seen == []
