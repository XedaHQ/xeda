"""`design` and `design_file` in the JSON documents of `xeda run` and `xeda dse`.

`design` is always the design's name: the one the design file gives, or the one the request gave
for a design of a project. `design_file` is the design file the request named, as an absolute
path, and `null` when the request named none (a design of a project, or no design). A failure
that comes before the design is loaded has no name to report for a design file: `design` is then
`null`, and `design_file` still says which file was named. The two keys are in every document:
success, failure, dry run and `--remote`.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow_runner import remote as remote_module
from xeda.introspect import design_info

from .project_files import PROJECT_FILE

EXAMPLE = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"
SQRT_SETTINGS = ["-s", "fpga.part=xc7a12tcsg325-1"]

BLINK = "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A start directory with the sqrt example and a Verilog design, the fake Vivado on the PATH."""
    for name in ("sqrt.vhdl", "sqrt.yaml", "tb_sqrt.py"):
        shutil.copy(EXAMPLE / name, tmp_path)
    (tmp_path / "blink.v").write_text(BLINK)
    (tmp_path / "blink.toml").write_text(
        'name = "blink"\n[rtl]\nsources = ["blink.v"]\ntop = "blink"\n'
    )
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _json(*args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(cli, [*args, "--json"])
    return result.exit_code, json.loads(result.stdout)


def _is(path: str | None, expected: Path) -> bool:
    return path is not None and os.path.isabs(path) and Path(path).samefile(expected)


def test_a_run_reports_the_design_s_name_and_the_file_it_was_read_from(world):
    status, document = _json("run", "vivado_synth", "sqrt.yaml", *SQRT_SETTINGS)

    assert status == 0, document
    assert document["design"] == "sqrt"
    assert _is(document["design_file"], world / "sqrt.yaml")


def test_the_design_file_option_names_the_file_the_same_way(world):
    status, document = _json("run", "vivado_synth", "--design-file", "sqrt.yaml", *SQRT_SETTINGS)

    assert status == 0, document
    assert document["design"] == "sqrt"
    assert _is(document["design_file"], world / "sqrt.yaml")


def test_a_failure_after_the_design_loaded_still_names_it(world):
    status, document = _json("run", "vivado_synth", "sqrt.yaml", "-s", "no_such_setting=1")

    assert status == 1 and document["success"] is False
    assert document["error"]["type"] == "FlowSettingsError"
    assert document["design"] == "sqrt"
    assert _is(document["design_file"], world / "sqrt.yaml")


def test_a_design_file_that_does_not_load_has_no_name_but_the_file_is_named(world):
    (world / "bad.toml").write_text(
        'name = "bad"\n[rtl]\nsources = ["missing.vhdl"]\ntop = "bad"\n'
    )
    status, document = _json("run", "vivado_synth", "bad.toml", *SQRT_SETTINGS)

    assert status == 1 and document["error"]["type"] == "DesignValidationError"
    assert document["design"] is None
    assert _is(document["design_file"], world / "bad.toml")


def test_a_dry_run_reports_the_same_two_keys(world):
    status, document = _json("run", "verilator", "blink.toml", "--dry-run")

    assert status == 0, document
    assert document["dry_run"] is True
    assert document["design"] == "blink"
    assert _is(document["design_file"], world / "blink.toml")


def test_a_design_of_a_project_has_a_name_and_no_file(world):
    (world / PROJECT_FILE).write_text(
        "designs:\n  - name: blink\n    rtl:\n      sources: [blink.v]\n      top: blink\n"
    )
    status, document = _json("run", "verilator", "--design-name", "blink", "--dry-run")

    assert status == 0, document
    assert document["design"] == "blink"
    assert document["design_file"] is None


def test_a_design_of_a_project_that_is_not_there_is_named_as_asked(world):
    (world / PROJECT_FILE).write_text(
        "designs:\n  - name: blink\n    rtl:\n      sources: [blink.v]\n      top: blink\n"
    )
    status, document = _json("run", "verilator", "--design-name", "nosuch", "--dry-run")

    assert status == 1 and document["success"] is False
    assert document["design"] == "nosuch"
    assert document["design_file"] is None


def test_no_design_at_all_names_neither(world):
    status, document = _json("run", "verilator")

    assert status == 1
    assert document["error"]["type"] == "DesignNotSpecified"
    assert document["design"] is None and document["design_file"] is None


def test_a_remote_run_reports_the_same_two_keys(world, monkeypatch):
    """The design loads before anything connects, so the document has its name."""

    class Unreachable:
        def __init__(self, host, user=None, port=None):
            raise ConnectionRefusedError(f"connected to {host}")

    monkeypatch.setattr(remote_module, "Connection", Unreachable)
    status, document = _json("run", "verilator", "blink.toml", "--remote", "somewhere")

    assert status == 1 and document["success"] is False
    assert document["error"]["type"] == "ConnectionRefusedError"
    assert document["design"] == "blink"
    assert _is(document["design_file"], world / "blink.toml")


def test_a_remote_run_whose_design_does_not_load_has_no_name(world, monkeypatch):
    (world / "bad.toml").write_text(
        'name = "bad"\n[rtl]\nsources = ["missing.vhdl"]\ntop = "bad"\n'
    )
    status, document = _json("run", "verilator", "bad.toml", "--remote", "somewhere")

    assert status == 1 and document["success"] is False
    assert document["design"] is None
    assert _is(document["design_file"], world / "bad.toml")


def test_an_exploration_that_fails_after_loading_its_design_names_it(world):
    """No device is given, which the exploration finds out once it has the design."""
    status, document = _json(
        "dse",
        "vivado_synth",
        "--design",
        "blink.toml",
        "--init-freq-low",
        "100",
        "--init-freq-high",
        "300",
    )

    assert status == 1 and document["success"] is False
    assert "`fpga`" in document["error"]["message"]
    assert document["design"] == "blink"
    assert _is(document["design_file"], world / "blink.toml")


def test_an_exploration_that_fails_before_loading_its_design_has_no_name(world):
    status, document = _json(
        "dse", "vivado_synth", "--design", "sqrt.yaml", "--optimizer", "no_such_optimizer"
    )

    assert status == 1 and document["error"]["type"] == "OptimizerNotFound"
    assert document["design"] is None
    assert _is(document["design_file"], world / "sqrt.yaml")


@pytest.mark.parametrize(
    ("requested", "loaded", "name", "file"),
    [
        (None, None, None, None),
        ("sqrt.yaml", None, None, "sqrt.yaml"),
        ("sqrt.yaml", "sqrt", "sqrt", "sqrt.yaml"),
        ("dir/Sqrt.YAML", None, None, "dir/Sqrt.YAML"),  # a design-file suffix in any case
        (Path("/x/y.toml"), "y", "y", "/x/y.toml"),
        (Path("plain"), None, None, "plain"),  # a `Path` is always a file
        ("blink", None, "blink", None),  # a design of a project, by the name asked for
        ("blink", "blink", "blink", None),
    ],
)
def test_the_two_keys_follow_one_rule(requested, loaded, name, file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    document = design_info(requested, loaded)

    assert document == {"design": name, "design_file": os.path.abspath(file) if file else None}
