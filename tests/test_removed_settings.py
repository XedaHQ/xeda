"""A removed setting is an error that names what replaced it, for every flow."""

import pytest

from xeda.flow import FlowSettingsError, registered_flows

FLOWS = sorted({cls for _, cls in registered_flows.values()}, key=lambda c: c.name)


@pytest.mark.parametrize("flow", FLOWS, ids=lambda c: c.name)
def test_clean_is_no_longer_a_flow_setting(flow, tmp_path):
    with pytest.raises(FlowSettingsError, match="--clean"):
        flow.Settings.from_input({"clean": True}, design_root=tmp_path, runner_cwd=tmp_path)


def test_verilator_clean_before_run_names_the_option(tmp_path):
    verilator = registered_flows["verilator"][1]
    with pytest.raises(FlowSettingsError, match="--clean"):
        verilator.Settings.from_input(
            {"clean_before_run": True}, design_root=tmp_path, runner_cwd=tmp_path
        )


@pytest.mark.parametrize("flow_name", ["ghdl_sim", "ghdl_synth"])
def test_ghdl_clean_names_both_replacements(flow_name, tmp_path):
    """GHDL's old `clean` meant "run `ghdl remove` before analysis", not "empty the run
    directory": the error must point at `clean_before_analyze` as well as `--clean`, so a user
    who set `clean=False` to keep GHDL's incremental analysis is not steered into the option that
    does the opposite."""
    flow = registered_flows[flow_name][1]
    with pytest.raises(FlowSettingsError, match="clean_before_analyze") as exc_info:
        flow.Settings.from_input({"clean": False}, design_root=tmp_path, runner_cwd=tmp_path)
    assert "--clean" in str(exc_info.value)


@pytest.mark.parametrize("flow", FLOWS, ids=lambda c: c.name)
def test_no_flow_overrides_clean(flow):
    assert "clean" not in vars(flow), f"{flow.name} still defines clean()"
