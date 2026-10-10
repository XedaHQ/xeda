"""`xeda run` options for rebuilding, and the errors that replace the removed ones."""

import json
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow_runner import remote

pytestmark = pytest.mark.python_compat


SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"


@pytest.fixture
def sqrt(tmp_path, monkeypatch):
    """The example design, read in place; runs land in `tmp_path/xeda_run`."""
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)
    return SQRT / "sqrt.yaml"


def _run(*args):
    result = CliRunner().invoke(cli, ["run", "vivado_synth", *args], catch_exceptions=False)
    return result, json.loads(result.stdout) if "--json" in args else None


def test_a_second_run_reuses_by_default_and_says_so(sqrt):
    settings = ["-s", "fpga.part=xc7a12tcsg325-1"]
    _run(str(sqrt), *settings, "--json")
    result, document = _run(str(sqrt), *settings, "--json")
    assert result.exit_code == 0 and document["success"]
    assert [node["state"] for node in document["nodes"]] == ["fresh"]


@pytest.mark.parametrize("option", ["--rebuild-all", "--clean"])
def test_rebuild_all_and_clean_run_a_fresh_flow_again(sqrt, option):
    """`--clean` implies `--rebuild-all`."""
    settings = ["-s", "fpga.part=xc7a12tcsg325-1"]
    _run(str(sqrt), *settings, "--json")
    _, document = _run(str(sqrt), *settings, option, "--json")
    assert [node["state"] for node in document["nodes"]] == ["ran"]


def test_hashed_run_dirs_give_each_settings_variant_its_own_directory(sqrt, tmp_path):
    settings = ["-s", "fpga.part=xc7a12tcsg325-1"]
    _, stable = _run(str(sqrt), *settings, "--json")
    _, hashed = _run(str(sqrt), *settings, "--hashed-run-dirs", "--json")
    _, other = _run(str(sqrt), *settings, "clock.period=7", "--hashed-run-dirs", "--json")
    design_dir = (tmp_path / "xeda_run" / "sqrt").resolve()
    assert Path(stable["run_path"]) == design_dir / "vivado_synth"
    for document in (hashed, other):
        assert document["success"] and Path(document["run_path"]).parent == design_dir
        assert re.fullmatch("vivado_synth_[a-z0-9]{16}", Path(document["run_path"]).name)
    assert hashed["run_path"] != other["run_path"]
    assert [node["state"] for node in hashed["nodes"]] == ["ran"]  # not the stable directory's


def test_the_environment_sets_no_option(sqrt, monkeypatch):
    """No automatic `XEDA_<OPTION>` variables: a leftover `XEDA_CLEAN=1` would empty every run
    directory on every run, with nothing on the command line to turn it off."""
    settings = ["-s", "fpga.part=xc7a12tcsg325-1"]
    _run(str(sqrt), *settings, "--json")
    for name in ("CLEAN", "REBUILD_ALL", "HASHED_RUN_DIRS", "POST_CLEANUP_PURGE"):
        monkeypatch.setenv(f"XEDA_{name}", "1")
        monkeypatch.setenv(f"XEDA_RUN_{name}", "1")  # the prefix a subcommand would derive
    result, document = _run(str(sqrt), *settings, "--json")
    assert result.exit_code == 0
    assert [node["state"] for node in document["nodes"]] == ["fresh"]
    assert Path(document["run_path"]).name == "vivado_synth"
    assert Path(document["run_path"]).is_dir()


@pytest.mark.parametrize(
    "option, replacement",
    [
        (
            "--cached-dependencies",
            "the default, which reuses unchanged runs, and --hashed-run-dirs to keep settings "
            "variants side by side",
        ),
        ("--no-cached-dependencies", "--rebuild-all to run every flow"),
        (
            "--incremental",
            "the default behavior: run directories are always reused, and --clean empties them "
            "first",
        ),
        ("--no-incremental", "--clean to empty run directories before running"),
    ],
)
def test_removed_options_name_their_replacement(sqrt, option, replacement):
    result, document = _run(str(sqrt), option, "--json")
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["message"] == f"`{option}` was removed: use {replacement}"


CWD_REMOVED = (
    "`--cwd` was removed: use --outputs-to . to receive the outputs here; the run itself goes "
    "under the run root (./xeda_run)"
)


def test_cwd_was_removed_and_names_its_replacement(sqrt, tmp_path):
    result, document = _run(str(sqrt), "--cwd", "--json")
    assert result.exit_code != 0
    assert document == {
        "success": False,
        "error": {"type": "UsageError", "message": CWD_REMOVED},
    }
    assert not (tmp_path / "xeda_run").exists(), "nothing ran"


def test_cwd_was_removed_without_json_too(sqrt):
    result = CliRunner().invoke(cli, ["run", "vivado_synth", str(sqrt), "--cwd"])
    assert result.exit_code == 2 and CWD_REMOVED in result.output


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
    target = None  # what `RemoteRunner` records of the design it loaded
    design_name = None  # likewise

    def __init__(self, run_root, **settings):
        self.settings_seen.append(settings)

    def run_remote(self, *args, **kwargs):
        return {"success": True, "run_path": "/remote/run"}


def test_remote_mirrors_into_hashed_run_directories(sqrt, monkeypatch):
    """A remote run's results are mirrored in `<flow>_<flowrun_hash>`, the remote runner's own
    layout: the command line hands it none -- only where its outputs are delivered."""
    _RecordingRemoteRunner.settings_seen = []
    monkeypatch.setattr(remote, "RemoteRunner", _RecordingRemoteRunner)
    result, document = _run(str(sqrt), "--remote", "host", "--json")
    assert result.exit_code == 0 and document["success"]
    assert _RecordingRemoteRunner.settings_seen == [
        {"outputs_to": None, "overwrite_outputs": False, "rebuild_all": False, "debug": False}
    ]


def test_remote_rebuild_all_is_a_local_generator_escape(sqrt, monkeypatch):
    """The remote flow stays fresh, and the explicit flag also reaches local design loading."""
    _RecordingRemoteRunner.settings_seen = []
    monkeypatch.setattr(remote, "RemoteRunner", _RecordingRemoteRunner)
    result, document = _run(str(sqrt), "--remote", "host", "--rebuild-all", "--json")
    assert result.exit_code == 0 and document["success"]
    assert _RecordingRemoteRunner.settings_seen == [
        {"outputs_to": None, "overwrite_outputs": False, "rebuild_all": True, "debug": False}
    ]


@pytest.mark.parametrize("option", ["--clean", "--hashed-run-dirs"])
def test_remote_refuses_local_layout_flags(sqrt, monkeypatch, option):
    """These flags still conflict with the remote runner's fresh hashed layout."""
    _RecordingRemoteRunner.settings_seen = []
    monkeypatch.setattr(remote, "RemoteRunner", _RecordingRemoteRunner)
    result, document = _run(str(sqrt), "--remote", "host", option, "--json")
    assert result.exit_code != 0 and document["success"] is False
    assert document["error"]["message"] == (
        f"`{option}` is not supported with --remote: remote runs always run fresh, mirrored in "
        "hashed run directories"
    )
    assert _RecordingRemoteRunner.settings_seen == []
