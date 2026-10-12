"""What a flow does with a setting that it ignored before.

`test_unread_settings.py` finds a setting that nothing reads. Each setting it found was either
applied or taken out of the flow. These tests show both: the flows refuse the settings they do not
use and name where the setting belongs, and the flows that apply the others render the tool's
command.
"""

import re
import shutil

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nvc, Openroad, VivadoAltSynth, VivadoProject, VivadoSynth, YosysFpga

from .settings_samples import minimal_settings
from .test_vivado_step_tables import PART, _design, _run, _script
from .test_openroad import _mac_design
from .tool_utils import fake_calls, require_nvc_evidence, use_fake_asic_tools

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)

SYNTHESIS = {"fpga": PART, "clock": {"period": 5.5}}

#: flow, a setting the flow does not have, a value for it, and what the error says to use
REFUSED = [
    ("vivado_project", "vcd", True, "flows.vivado_sim.vcd"),
    ("vivado_project", "waveform", "wave", "flows.vivado_sim.vcd"),
    ("vivado_project", "stop_time", 10, "flows.vivado_sim.stop_time"),
    ("vivado_project", "fail_severity", "error", "flows.vivado_sim.fail_severity"),
    ("vivado_project", "saif", "a.saif", "flows.vivado_sim.saif"),
    ("vivado_project", "optimization_flags", ["-O3"], "flows.vivado_sim.optimization_flags"),
    ("vivado_sim", "initialize_zeros", True, "no setting"),
    ("vivado_sim", "read_oneshot", True, "no setting"),
    ("vivado_sim", "suppress_msgs", [], "no setting"),
    ("vivado_postsynth_sim", "suppress_msgs", [], "no setting"),
    ("vivado_power", "suppress_msgs", [], "no setting"),
    ("yosys_sim", "netlist", "out.v", "no setting"),
    ("yosys_sim", "netlist_json", "out.json", "no setting"),
    ("yosys_sim", "splitnets", True, "no setting"),
    ("yosys_sim", "abc_script", "+strash", "no setting"),
    ("yosys_sim", "sta", True, "no setting"),
    ("yosys_sim", "nosynth", True, "no setting"),
    ("yosys_sim", "retime", True, "no setting"),
    ("yosys_sim", "netlist_verilog_extmem", [], "no setting"),
    ("yosys", "retime", True, "yosys_fpga"),
    ("yosys", "netlist_verilog_extmem", [], "no setting"),
    ("yosys_fpga", "nosynth", True, "no setting"),
    ("yosys_fpga", "netlist_verilog_extmem", [], "no setting"),
    ("ghdl_synth", "compiler_flags", ["-O"], "flows.ghdl_sim.compiler_flags"),
    ("ghdl_synth", "linker_flags", ["-L"], "flows.ghdl_sim.linker_flags"),
    ("dc", "platform", "nangate45", "target_libraries"),
    ("dc", "mw_ref_lib", "lib", "no setting"),
    ("quartus", "synthesis_effort", "fast", "no setting"),
    ("vcs", "work_dir", "work", "no setting"),
    ("vcs", "optimization_flags", ["-O"], "no setting"),
    ("modelsim", "optimization_flags", ["-O"], "no setting"),
    ("nvc", "optimization_flags", ["-O"], "no setting"),
    ("verilator", "optimization_flags", ["-O"], "no setting"),
    ("yosys_sim", "optimization_flags", ["-O"], "no setting"),
    ("bsc", "timeout_seconds", 5, "no setting"),
    ("vivado_synth", "no_console", True, "no setting"),
]


@pytest.mark.parametrize(
    ("flow", "setting", "value", "use"), REFUSED, ids=[f"{f}-{s}" for f, s, _, _ in REFUSED]
)
def test_a_flow_refuses_a_setting_it_does_not_use_and_says_where_it_belongs(
    flow, setting, value, use
) -> None:
    cls = registered_flows[flow][1]
    with pytest.raises(FlowSettingsError) as refused:
        cls.Settings.from_input({**minimal_settings(cls), setting: value})
    assert f"`{setting}` was removed: " in str(refused.value)
    assert use in str(refused.value)


@pytest.mark.parametrize("flow", ["bsc_sim", "ghdl_sim", "vivado_sim"])
def test_the_simulators_that_pass_optimization_flags_still_take_them(flow) -> None:
    cls = registered_flows[flow][1]
    cls.Settings.from_input({**minimal_settings(cls), "optimization_flags": ["-O1"]})


# ------------------------------------------------------------------------------ vivado_project


@needs_tclsh
def test_vivado_project_sets_the_properties_of_its_runs(tmp_path, monkeypatch) -> None:
    flow = _run(
        VivadoProject,
        tmp_path,
        _design(),
        {
            **SYNTHESIS,
            "set_synth_properties": {"flow": "Vivado Synthesis 2024"},
            "set_impl_properties": {"report_strategy": True},
        },
        monkeypatch,
    )
    calls = fake_calls(flow.run_path)
    assert ["set_property", "flow", "Vivado Synthesis 2024", "synth_1"] in calls
    assert ["set_property", "report_strategy", "true", "impl_1"] in calls


@needs_tclsh
@pytest.mark.parametrize("shown", [False, True])
def test_vivado_project_shows_the_strategies_when_asked(shown, tmp_path, monkeypatch) -> None:
    flow = _run(
        VivadoProject,
        tmp_path,
        _design(),
        {**SYNTHESIS, "show_available_strategies": shown},
        monkeypatch,
    )
    asked = [call for call in fake_calls(flow.run_path) if call[0] == "list_property_value"]
    assert asked == ([["list_property_value", "strategy", "synth_1"]] * shown) + (
        [["list_property_value", "strategy", "impl_1"]] * shown
    )


@needs_tclsh
@pytest.mark.parametrize("flow_class", [VivadoSynth, VivadoProject])
@pytest.mark.parametrize("extra", [False, True])
def test_extra_reports_are_written_after_routing_when_asked(
    flow_class, extra, tmp_path, monkeypatch
) -> None:
    flow = _run(flow_class, tmp_path, _design(), {**SYNTHESIS, "extra_reports": extra}, monkeypatch)
    hook = _script(flow, "post_route_design_hook.tcl")
    assert ("report_clock_utilization" in hook) is extra
    assert ("report_methodology" in hook) is extra


@needs_tclsh
@pytest.mark.parametrize("power", [False, True])
def test_vivado_alt_synth_writes_a_power_estimate_when_asked(power, tmp_path, monkeypatch) -> None:
    flow = _run(
        VivadoAltSynth, tmp_path, _design(), {**SYNTHESIS, "report_power": power}, monkeypatch
    )
    assert ("power.xml" in _script(flow, "vivado_alt_synth.tcl")) is power


# ------------------------------------------------------------------------------------ yosys


def test_yosys_fpga_splits_nets_the_way_it_is_asked(tmp_path) -> None:
    from .test_yosys_templates import _fpga_settings, _render

    for script_format, command in (("ys", "splitnets"), ("tcl", "yosys splitnets")):
        for flags, expected in (
            ({"splitnets_driver": True, "splitnets_ports": True}, " -driver -ports"),
            ({}, ""),
        ):
            root = tmp_path / f"{script_format}{bool(flags)}"
            root.mkdir()
            script = _render(
                YosysFpga,
                _fpga_settings(script_format=script_format, splitnets=True, **flags),
                root,
            )
            assert re.search(rf"^{command}{expected}$", script, re.MULTILINE), script


# ------------------------------------------------------------------------------------- nvc

NVC_WARNING = """
entity w is port (a, b : in bit; y : out bit); end;
architecture rtl of w is begin
  process (a) begin y <= a and b; end process;
end;
"""
NVC_TESTBENCH = """
entity tb is end;
architecture sim of tb is begin
  process begin std.env.finish; wait; end process;
end;
"""


def _nvc_design(root) -> Design:
    (root / "w.vhd").write_text(NVC_WARNING)
    (root / "tb.vhd").write_text(NVC_TESTBENCH)
    return Design(
        name="w",
        design_root=root,
        rtl={"sources": ["w.vhd"], "top": "w"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )


def test_nvc_treats_an_analysis_warning_as_an_error_when_asked(tmp_path) -> None:
    """The unreferenced signal `b` makes `--check-synthesis` warn, so `-Werror` fails the run."""
    require_nvc_evidence()
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    warned = runner.run_flow(Nvc, _nvc_design(tmp_path), {"werror": False})
    assert warned is not None and warned.succeeded
    refused = runner.run_flow(Nvc, _nvc_design(tmp_path), {"werror": True, "one_shot": False})
    assert refused is not None and not refused.succeeded
    assert "Werror" in str(refused.results.get("error", {}).get("message", ""))


# ------------------------------------------------------------------------------------ openroad


def test_openroad_sources_the_floorplan_script_and_hands_the_macro_placer_its_config(
    tmp_path, monkeypatch
) -> None:
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    floorplan, config = tmp_path / "floorplan.tcl", tmp_path / "rtlmp config.cfg"
    floorplan.write_text("puts hello\n")
    config.write_text("x\n")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.launch_flow(
        Openroad,
        _mac_design(tmp_path),
        {
            "platform": "nangate45",
            "clock": {"period": 2.0},
            "floorplan_tcl": str(floorplan),
            "rtlmp_flow": True,
            "rtlmp_config_file": str(config),
        },
    )
    assert flow.succeeded
    script = (flow.run_path / "orflow_0_11.tcl").read_text()
    assert f'source "{floorplan}"' in script
    assert f'-config_file [list "{config}"]' in script
    assert script.count("-config_file") == 1, "the setting replaces the environment variable's"
