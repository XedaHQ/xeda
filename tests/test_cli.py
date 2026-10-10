import json
import os
from pathlib import Path

import pytest

import click
from click.testing import CliRunner

from xeda.cli import cli

pytestmark = pytest.mark.python_compat


flows = ["ghdl_sim", "yosys", "nextpnr", "vivado_sim", "vivado_synth"]


def _output(result) -> str:
    """Help screens are ANSI-styled, and click-extra keeps the styling whenever a color
    environment variable (`CLICOLOR`, `FORCE_COLOR`, ...) is set -- which depends on the
    developer's shell. Assert on the text, not on whether that shell had colors on."""
    return click.unstyle(result.output)


def test_cli_run_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "Usage: " in _output(result)


def test_cli_help_subcommand():
    runner = CliRunner()
    result = runner.invoke(cli, ["help", "run"])
    assert result.exit_code == 0
    assert "Usage: " in _output(result)


def test_cli_list_flows():
    runner = CliRunner()
    result = runner.invoke(cli, ["list-flows"])
    assert result.exit_code == 0
    output = _output(result)
    for flow in flows:
        assert flow in output


def test_cli_list_settings():
    runner = CliRunner()
    for flow_name in flows:
        result = runner.invoke(cli, ["list-settings", flow_name])
        assert result.exit_code == 0, f"Failed: CLI list-settings {flow_name}"


def test_machine_readable_mode_does_not_leak_between_invocations():
    """`--json` redirects module-global state; it must not outlive the command that set it.

    `machine_readable_mode()` points the rich console and the tool-output stream at
    `sys.stderr`. Both are module-global, so a `--json` command used to leave every later
    command in the same process writing to a stream belonging to the finished one -- under
    `CliRunner` the next human-facing command produced no output at all. A one-shot `xeda`
    process never noticed, which is why this only shows up in-process.
    """
    from xeda import proc_utils
    from xeda.console import console_target

    runner = CliRunner()
    # `scrub --json` is an executional command, so it calls machine_readable_mode()
    first = runner.invoke(cli, ["scrub", "vivado_synth", "no_such_design", "--json"])
    assert first.exit_code == 0, first.output

    assert console_target() is None, "the rich console is still redirected"
    assert proc_utils.tool_output_redirect() is None, "tool output is still redirected"

    second = runner.invoke(cli, ["list-flows"])
    assert second.exit_code == 0
    assert "vivado_synth" in _output(second), "human-facing output went to the previous stream"


SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"


def _fake_vivado(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(tmp_path)


def test_dash_s_stops_before_the_design_file(tmp_path, monkeypatch):
    _fake_vivado(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli, ["run", "vivado_synth", "-s", "fpga.part=xc7a12tcsg325-1", str(SQRT), "--json"]
    )
    document = json.loads(result.stdout)
    assert result.exit_code == 0, document
    assert document["design"] == "sqrt"


def test_dash_s_keeps_a_value_that_contains_equals_signs():
    """Only the first `=` splits key from value; the list still ends at a bare token."""
    from xeda.cli_utils import OptionEatAll

    seen = {}

    @click.command()
    @click.option("-s", "settings", cls=OptionEatAll, type=tuple)
    @click.argument("design", required=False)
    def command(settings, design):
        seen.update(settings=settings, design=design)

    result = CliRunner().invoke(command, ["-s", "a=b=c", "x=1", "design.toml"])
    assert result.exit_code == 0, result.output
    assert seen == {"settings": ("a=b=c", "x=1"), "design": "design.toml"}


def test_a_missing_design_after_dash_s_is_reported_as_missing(tmp_path, monkeypatch):
    _fake_vivado(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli, ["run", "vivado_synth", "-s", "fpga.part=xc7a12tcsg325-1", "missing.toml", "--json"]
    )
    document = json.loads(result.stdout)
    assert not document["success"]
    assert "missing.toml" in document["error"]["message"]


def test_dash_s_needs_key_value_items(tmp_path, monkeypatch):
    """A bare first token is an error naming it, never silently taken as the design."""
    _fake_vivado(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli, ["run", "vivado_synth", "-s", "missing.toml", "--json"])
    document = json.loads(result.stdout)
    assert not document["success"]
    message = document["error"]["message"]
    assert "KEY=VALUE" in message and "missing.toml" in message


def _eat_all(args):
    from xeda.cli_utils import OptionEatAll

    seen = {}

    @click.command()
    @click.option("-s", "settings", cls=OptionEatAll, type=tuple)
    @click.argument("design", required=False)
    def command(settings, design):
        seen.update(settings=settings, design=design)

    result = CliRunner().invoke(command, args)
    assert result.exit_code == 0, result.output
    return seen


def test_dash_s_ends_at_a_path_that_contains_equals_signs():
    assert _eat_all(["-s", "x=1", "dir/a=b.toml"]) == {
        "settings": ("x=1",),
        "design": "dir/a=b.toml",
    }
    assert _eat_all(["-s", "x=1", "./a=b/x.toml"]) == {
        "settings": ("x=1",),
        "design": "./a=b/x.toml",
    }


def test_double_dash_ends_the_settings_list():
    assert _eat_all(["-s", "x=1", "--", "a=b.toml"]) == {"settings": ("x=1",), "design": "a=b.toml"}


def test_dash_s_keys_may_have_brackets_and_dots():
    assert _eat_all(["-s", "lib_paths[0]=x", "y.z=1"]) == {
        "settings": ("lib_paths[0]=x", "y.z=1"),
        "design": None,
    }
