"""OpenROAD prepares no output until run(), and keeps its own cell policy."""

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


def test_a_typed_netlist_displaces_synthesis(tmp_path, monkeypatch):
    from xeda.flow_runner import DefaultRunner
    from .tool_utils import use_fake_asic_tools

    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    netlist = tmp_path / "d.v"
    netlist.write_text("module d(input clk, output q); assign q=clk; endmodule\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [{"file": "d.v", "type": "VerilogNetlist"}], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    plan = runner.plan(Openroad, design, flow_settings=["platform=nangate45"])
    assert len(plan.nodes) == 1
    netlist_input, sdc = plan.node("openroad").inputs
    assert netlist_input.name == "netlist" and netlist_input.origin == "source"
    # the design lists no SDC source, so the optional input is empty
    assert sdc.name == "sdc" and sdc.origin == "none" and not sdc.sources
    flow = runner.launch_flow(Openroad, design, {"platform": "nangate45"})
    assert flow.succeeded
    assert not (flow.run_path.parent / "yosys").exists()
    assert (flow.run_path / "results" / "1_synth.v").read_bytes() == netlist.read_bytes()


def test_the_cli_sends_all_moved_settings_to_real_yosys(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    from .test_tool_input_equivalence import write_asic_design
    from .tool_utils import require_yosys, use_fake_asic_tools

    require_yosys()
    binary = use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    (binary / "yosys").unlink()
    write_asic_design(tmp_path)
    root = tmp_path / "runs"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "run",
            "openroad",
            str(tmp_path / "design.yaml"),
            "--run-root",
            str(root),
            "--json",
            "-s",
            "platform=nangate45",
            "clock.period=2.0",
            "flows.yosys.optimize=speed",
            "flows.yosys.abc_driver_cell=BUF_X4",
            "flows.yosys.abc_load_in_ff=2.5",
            "flows.yosys.black_box=mul8",
            "flows.yosys.post_synth_opt=false",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0 and document["success"], proc.stderr[-3000:] or document
    producer = root / "mac" / "yosys"
    settings = json.loads((producer / "settings.json").read_text())["effective_flow_settings"]
    assert settings["optimize"] == "speed"
    assert settings["abc_driver_cell"] == "BUF_X4"
    assert settings["abc_load_in_ff"] == 2.5
    assert settings["black_box"] == ["mul8"]
    assert settings["post_synth_opt"] is False
    script = (producer / "yosys_synth.ys").read_text()
    assert "&if,-g,-K,6" in script and "blackbox mul8" in script
    assert "opt -full -purge -sat" not in script
    assert (producer / "abc.constr").read_text() == "set_driving_cell BUF_X4\nset_load 2.5\n"
    assert (root / "mac" / "openroad" / "results" / "1_synth.v").read_bytes() == (
        producer / "netlist.v"
    ).read_bytes()


def test_the_design_s_sdc_sources_are_a_declared_input_read_before_the_generated_one(
    tmp_path, monkeypatch
):
    """OpenROAD reads the design's SDC files through its declared `sdc` input, in source order,
    then the constraints it generates from `clocks`, then `sdc_files`: it reads no design source
    of its own."""
    import json

    from xeda.flow_runner import DefaultRunner

    from .tool_utils import use_fake_asic_tools

    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    (tmp_path / "d.v").write_text("module d(input clk, output q); assign q=clk; endmodule\n")
    for name in ("a.sdc", "b.sdc", "extra.sdc"):
        (tmp_path / name).write_text("set_load 0.1 [all_outputs]\n")
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": ["b.sdc", "d.v", "a.sdc"], "top": "d"}
    )
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    settings = {"platform": "nangate45", "sdc_files": [str(tmp_path / "extra.sdc")]}
    plan = runner.plan(Openroad, design, flow_settings=settings)
    sdc = {resolved.name: resolved for resolved in plan.node("openroad").inputs}["sdc"]
    assert sdc.origin == "source"
    assert list(sdc.sources) == [tmp_path / "b.sdc", tmp_path / "a.sdc"]
    flow = runner.launch_flow(Openroad, design, settings)
    assert flow.succeeded
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["effective_flow_settings"]["sdc_files"] == [
        str(tmp_path / "b.sdc"),
        str(tmp_path / "a.sdc"),
        "clocks.sdc",
        str(tmp_path / "extra.sdc"),
    ]
