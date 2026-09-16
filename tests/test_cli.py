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
