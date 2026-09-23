"""Tests for `xeda scrub` / `scrub_runs`.

Ground truth (see CLAUDE.md's run-directory table, and `DefaultRunner.get_flow_run_path`): a
flow's run directory hash suffix only appears with `--cached-dependencies`. The default,
unhashed `<design>/<flow>/` directory that every ordinary run creates must still be removable by
`xeda scrub`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.console import console
from xeda.flow_runner.default_runner import scrub_runs

TESTS_DIR = Path(__file__).parent.absolute()


def _output(result) -> str:
    """Help screens are ANSI-styled depending on the developer's shell; assert on plain text."""
    return click.unstyle(result.output)


def _make_flow_dirs(base: Path) -> dict:
    """Populate `base` (a design directory) with the run-directory shapes scrub must tell apart."""
    names = [
        "vivado_synth",  # default, unhashed run dir -- must be scrubbed
        "vivado_synth_0123456789abcdef",  # --cached-dependencies run dir -- must be scrubbed
        "vivado_synth_other",  # not a 16-char hash suffix -- must survive
        "vivado_synth.backup_x",  # a backup dir, not an underscore suffix -- must survive
        "yosys_fpga",  # a different flow that merely starts with a similar prefix pattern
    ]
    dirs = {}
    for name in names:
        d = base / name
        d.mkdir(parents=True)
        (d / "results.json").write_text("{}")
        dirs[name] = d
    # A plain file whose name looks like a hashed run dir must never be mistaken for one.
    (base / "vivado_synth_abcdefabcdefabcd").write_text("not a directory")
    return dirs


# ------------------------------------------------------------------ unit tests for `scrub_runs`


def test_scrub_runs_removes_unhashed_and_hashed_dirs_only(tmp_path, monkeypatch):
    base = tmp_path / "foo"
    base.mkdir()
    dirs = _make_flow_dirs(base)
    monkeypatch.setattr(console, "input", lambda *a, **kw: "yes")

    removed = scrub_runs("vivado_synth", base, exclude=[])

    assert removed is True
    assert not dirs["vivado_synth"].exists()
    assert not dirs["vivado_synth_0123456789abcdef"].exists()
    assert dirs["vivado_synth_other"].exists()
    assert dirs["vivado_synth.backup_x"].exists()
    assert dirs["yosys_fpga"].exists()
    assert (base / "vivado_synth_abcdefabcdefabcd").exists()


def test_scrub_runs_respects_exclude(tmp_path, monkeypatch):
    """`_prepare_run_path` calls `scrub_runs` with the current run dir excluded."""
    base = tmp_path / "foo"
    base.mkdir()
    dirs = _make_flow_dirs(base)
    monkeypatch.setattr(console, "input", lambda *a, **kw: "yes")

    removed = scrub_runs("vivado_synth", base, exclude=[dirs["vivado_synth"]])

    assert removed is True
    assert dirs["vivado_synth"].exists(), "excluded directory must survive"
    assert not dirs["vivado_synth_0123456789abcdef"].exists()


def test_scrub_runs_declines_without_confirmation(tmp_path, monkeypatch):
    base = tmp_path / "foo"
    base.mkdir()
    dirs = _make_flow_dirs(base)
    monkeypatch.setattr(console, "input", lambda *a, **kw: "no")

    removed = scrub_runs("vivado_synth", base, exclude=[])

    assert removed is False
    assert dirs["vivado_synth"].exists()
    assert dirs["vivado_synth_0123456789abcdef"].exists()


def test_scrub_runs_does_not_match_longer_flow_name(tmp_path, monkeypatch):
    """`yosys` must not sweep up `yosys_fpga`'s run directory."""
    base = tmp_path / "foo"
    base.mkdir()
    dirs = _make_flow_dirs(base)
    monkeypatch.setattr(console, "input", lambda *a, **kw: "yes")

    scrub_runs("yosys", base, exclude=[])

    assert dirs["yosys_fpga"].exists()


# ------------------------------------------------------------------------------- CLI-level tests


def _run_xeda(*args: str, cwd=None, input_text: str = "yes\n") -> subprocess.CompletedProcess:
    env = dict(os.environ, COLUMNS="80")
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_scrub_removes_unhashed_and_hashed_dirs_only(tmp_path):
    xeda_run_dir = tmp_path / "xeda_run"
    base = xeda_run_dir / "foo"
    base.mkdir(parents=True)
    dirs = _make_flow_dirs(base)

    proc = _run_xeda(
        "scrub",
        "vivado_synth",
        "foo",
        "--xeda-run-dir",
        str(xeda_run_dir),
        "--json",
    )

    assert proc.returncode == 0, proc.stderr
    try:
        document = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"stdout was not a single JSON document ({e})\nstdout:\n{proc.stdout}")
    assert document["success"] is True

    assert not dirs["vivado_synth"].exists()
    assert not dirs["vivado_synth_0123456789abcdef"].exists()
    assert dirs["vivado_synth_other"].exists()
    assert dirs["vivado_synth.backup_x"].exists()
    assert dirs["yosys_fpga"].exists()
    assert (base / "vivado_synth_abcdefabcdefabcd").exists()


# ---------------------------------------------------------------------------------- stale help text


def test_run_help_does_not_mention_flow_settings_hash():
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    text = " ".join(_output(result).split())
    assert "flow_settings_hash" not in text
    # ... and says what the directories are named by instead
    assert "a hash of design and/or flow settings" in text
    assert "the design directory name also gets a design-hash suffix" in text


def test_scrub_help_does_not_mention_flow_settings_hash():
    runner = CliRunner()
    result = runner.invoke(cli, ["scrub", "--help"])
    assert result.exit_code == 0
    text = " ".join(_output(result).split())
    assert "flow_settings_hash" not in text
    assert "not only <design_name>_<design_hash> ones" in text
