import click
from click.testing import CliRunner

from xeda.cli import cli

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
