from click.testing import CliRunner
from xeda.cli import cli

flows = ["ghdl_sim", "nextpnr", "vivado_sim", "vivado_synth"]


def test_cli_run_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "Usage: " in result.output


def test_cli_list_flows():
    runner = CliRunner()
    result = runner.invoke(cli, ["list-flows"])
    assert result.exit_code == 0
    for flow in flows:
        assert flow in result.output


def test_cli_list_settings():
    runner = CliRunner()
    for flow_name in flows:
        result = runner.invoke(cli, ["list-settings", flow_name])
        assert result.exit_code == 0
        # assert result.output.count("Usage: run") > 0
