"""PCD9: OpenROAD prepares no output until run(), and keeps its own cell policy."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flows import Openroad
from xeda.utils import WorkingDirectory


@pytest.mark.parametrize("copy_platform_files", [False, True])
def test_init_and_prepare_inputs_write_nothing(tmp_path, copy_platform_files):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    run = tmp_path / "run"
    run.mkdir()
    flow = Openroad(
        {"platform": "nangate45", "copy_platform_files": copy_platform_files}, design, run
    )
    with WorkingDirectory(run):
        flow.init()
        flow.prepare_inputs()
    assert list(run.iterdir()) == []


def test_the_template_global_keeps_the_union_without_storing_it(tmp_path, monkeypatch):
    from .tool_utils import use_fake_asic_tools
    from xeda.flow_runner import DefaultRunner
    from xeda.utils import unique

    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    (tmp_path / "d.v").write_text("module d(input clk, output q); assign q=clk; endmodule\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["d.v"], "top": "d"})
    flow = DefaultRunner(tmp_path / "runs", display_results=False).launch_flow(
        Openroad, design, {"platform": "nangate45", "dont_use_cells": ["AND2_X2"]}
    )
    assert flow.succeeded
    assert flow.settings.dont_use_cells == ["AND2_X2"]
    policy = flow.jinja_env.globals["dont_use_cells"]()
    assert policy == unique(flow.settings.platform.dont_use_cells + ["AND2_X2"])
    script = (flow.run_path / "orflow_0_11.tcl").read_text()
    lines = [line for line in script.splitlines() if line.startswith("set_dont_use ")]
    expected = "set_dont_use " + flow.jinja_env.filters["tcl_list"](policy)
    assert lines == [expected] * 4


def test_an_incomplete_footprint_is_refused_before_producers_run():
    import pytest
    from xeda.flow import FlowSettingsError

    settings = Openroad.Settings(platform="nangate45", footprint=Path("pads.strategy"))
    before = settings.model_dump()
    with pytest.raises(FlowSettingsError, match="sig_map_file"):
        Openroad.check_settings_supported(settings)
    assert settings.model_dump() == before
