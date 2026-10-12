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
from xeda.flow import FlowSettingsError, NoReplacement
from xeda.dataclass import input_names
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nvc, Openroad, VivadoAltSynth, VivadoProject, VivadoSynth, YosysFpga

from .settings_samples import flow_classes, minimal_settings
from .test_vivado_step_tables import PART, _design, _run, _script
from .test_openroad import _mac_design
from .tool_utils import fake_calls, require_nvc_evidence, use_fake_asic_tools

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)

SYNTHESIS = {"fpga": PART, "clock": {"period": 5.5}}
PRODUCT_NAMES = {name for cls, name in flow_classes() if cls.__module__.startswith("xeda.")} & {
    cls.name for _, cls in registered_flows.values()
}

#: flow, a setting the flow does not have, a value for it, and what the error says to use. Every
#: setting that a flow lost is here, so a removed field without an entry in `removed_settings`
#: fails.
REFUSED = [
    ("vivado_project", "vcd", True, "flows.vivado_sim.vcd"),
    ("vivado_project", "waveform", "wave", "flows.vivado_sim.vcd"),
    ("vivado_project", "stop_time", 10, "flows.vivado_sim.stop_time"),
    ("vivado_project", "fail_severity", "error", "flows.vivado_sim.fail_severity"),
    ("vivado_project", "saif", "a.saif", "flows.vivado_sim.saif"),
    ("vivado_project", "optimization_flags", ["-O3"], "flows.vivado_sim.optimization_flags"),
    ("vivado_project", "initialize_zeros", True, "delete it"),
    ("vivado_project", "read_oneshot", True, "delete it"),
    ("vivado_sim", "initialize_zeros", True, "delete it"),
    ("vivado_sim", "read_oneshot", True, "delete it"),
    ("vivado_sim", "suppress_msgs", [], "delete it"),
    ("vivado_postsynth_sim", "initialize_zeros", True, "delete it"),
    ("vivado_postsynth_sim", "read_oneshot", True, "delete it"),
    ("vivado_postsynth_sim", "suppress_msgs", [], "delete it"),
    ("vivado_power", "suppress_msgs", [], "delete it"),
    ("yosys_sim", "netlist", "out.v", "delete it"),
    ("yosys_sim", "netlist_json", "out.json", "delete it"),
    ("yosys_sim", "splitnets", True, "delete it"),
    ("yosys_sim", "abc_script", "+strash", "delete it"),
    ("yosys_sim", "sta", True, "delete it"),
    ("yosys_sim", "nosynth", True, "delete it"),
    ("yosys_sim", "retime", True, "delete it"),
    ("yosys_sim", "netlist_verilog_extmem", [], "delete it"),
    ("yosys", "retime", True, "yosys_fpga"),
    ("yosys", "netlist_verilog_extmem", [], "delete it"),
    ("yosys_fpga", "nosynth", True, "delete it"),
    ("yosys_fpga", "netlist_verilog_extmem", [], "delete it"),
    ("ghdl_synth", "compiler_flags", ["-O"], "flows.ghdl_sim.compiler_flags"),
    ("ghdl_synth", "assembler_flags", ["-O"], "flows.ghdl_sim.assembler_flags"),
    ("ghdl_synth", "linker_flags", ["-L"], "flows.ghdl_sim.linker_flags"),
    *[
        ("dc", name, value, use)
        for name, value, use in (
            ("platform", "nangate45", "target_libraries"),
            ("extra_link_libraries", ["a.db"], "delete it"),
            ("mw_ref_lib", "lib", "delete it"),
            ("mw_tf", "x.tf", "delete it"),
            ("alib_dir", "alib", "delete it"),
            ("additional_search_path", "dir", "delete it"),
        )
    ],
    ("quartus", "synthesis_effort", "fast", "delete it"),
    ("vcs", "work_dir", "work", "delete it"),
    ("vcs", "optimization_flags", ["-O"], "delete it"),
    ("modelsim", "optimization_flags", ["-O"], "delete it"),
    ("nvc", "optimization_flags", ["-O"], "delete it"),
    ("verilator", "optimization_flags", ["-O"], "delete it"),
    ("yosys_sim", "optimization_flags", ["-O"], "delete it"),
    # removed from every flow
    *[
        (flow, name, value, use)
        for flow in sorted(PRODUCT_NAMES)
        for name, value, use in (
            ("timeout_seconds", 5, "`timeout`"),
            ("no_console", True, "delete it"),
        )
    ],
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
    assert "use no setting" not in str(refused.value)


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


# ----------------------------------------------------------------------- the removal sweeps


#: What this change removed from each flow (`timeout_seconds` and `no_console` from every one),
#: taken from the settings of the flows on origin/main: each name was a setting, or an
#: alias of one, of that flow there. A removal entry on a class that never had the setting would
#: refuse an unknown name as a removed one.
FORMER_FIELDS = {
    "dc": {
        "additional_search_path",
        "alib_dir",
        "extra_link_libraries",
        "mw_ref_lib",
        "mw_tf",
        "platform",
    },
    "ghdl_synth": {"assembler_flags", "compiler_flags", "linker_flags"},
    "modelsim": {"optimization_flags"},
    "nvc": {"optimization_flags"},
    "quartus": {"synthesis_effort"},
    "vcs": {"optimization_flags", "work_dir"},
    "verilator": {"optimization_flags"},
    "vivado_postsynth_sim": {"initialize_zeros", "read_oneshot", "suppress_msgs"},
    "vivado_power": {"suppress_msgs"},
    "vivado_sim": {"initialize_zeros", "read_oneshot", "suppress_msgs"},
    "yosys": {"netlist_verilog_extmem", "retime"},
    "yosys_fpga": {"netlist_verilog_extmem", "nosynth"},
    "yosys_sim": {
        *("abc_constr", "abc_dff", "abc_flags", "abc_script", "json_netlist", "ltp", "netlist"),
        *("netlist_attrs", "netlist_blackboxes", "netlist_dec", "netlist_expr", "netlist_graph"),
        *("netlist_graph_flags", "netlist_hex", "netlist_json", "netlist_simple_lhs"),
        *("netlist_src_attrs", "netlist_unset_attributes", "netlist_verilog"),
        *("netlist_verilog_extmem", "netlist_verilog_flags", "noabc", "nosynth"),
        *("optimization_flags", "post_synth_opt", "post_synth_rename", "retime", "rmports"),
        *("splitnets", "splitnets_driver", "splitnets_ports", "sta", "synth_flags", "write_blif"),
    },
    "vivado_project": {
        *("analyze_flags", "cocotb", "debug_traces", "elab_debug", "elab_flags", "fail_severity"),
        *("initialize_zeros", "optimization_flags", "prerun_time", "read_oneshot", "saif", "sdf"),
        *("sim_flags", "stop_time", "timeout", "vcd", "vcd_level", "vcd_scope", "waveform"),
        *("work_lib", "xelab_log"),
    },
}

#: The removed settings that origin/main already had, by flow, besides `clean`.
EARLIER_REMOVALS = {
    "dc": {"clock_period"},
    "diamond_synth": {"clock_period"},
    "fpga_pack": {"clock_period"},
    "ghdl_synth": {"clock_period"},
    "ise_synth": {"clock_period"},
    "nextpnr": {"clock_period", "lpf_cfg", "pcf_cfg", "pdc_cfg", "yosys"},
    "nvc": {"exit_severity", "wave", "wave_format"},
    "openfpgaloader": {"bitstream_file", "clock_period", "nextpnr", "packer_args"},
    "openroad": {"abc_driver_cell", "abc_load_in_ff", "blocks", "clock_period", "optimize"},
    "quartus": {"clock_period"},
    "verilator": {"clean_before_run"},
    "vivado_alt_synth": {"clock_period"},
    "vivado_impl": {"clock_period"},
    "vivado_postsynth_sim": {"clock_period", "synth"},
    "vivado_power": {
        *("clock_period", "elab_debug", "fail_severity", "postsynthsim", "prerun_time", "saif"),
        *("stop_time", "timeout", "timing_sim"),
    },
    "vivado_project": {"clock_period"},
    "vivado_synth": {"clock_period"},
    "yosys": {"clock_period"},
    "yosys_fpga": {"clock_period"},
}
EVERYWHERE = {"clean", "timeout_seconds", "no_console"}


@pytest.mark.parametrize("flow", sorted(PRODUCT_NAMES))
def test_every_removed_setting_of_a_flow_is_refused_as_removed(flow) -> None:
    """A key in `removed_settings` is no field and no alias of the flow, and giving it says it
    was removed (never `Extra inputs are not permitted`). A removal with a replacement says "use
    <replacement>"; one without says why and to delete the setting, never "use no setting"."""
    cls = registered_flows[flow][1]
    names = input_names(cls.Settings)
    for key, replacement in cls.Settings.removed_setting_replacements().items():
        assert key not in names, f"{flow}: {key} is a setting and was removed"
        with pytest.raises(FlowSettingsError) as refused:
            cls.Settings.from_input({**minimal_settings(cls), key: "x"})
        message = str(refused.value)
        if isinstance(replacement, NoReplacement):
            assert f"`{key}` was removed: {replacement}; delete it" in message
        else:
            assert f"`{key}` was removed: use " in message
        assert "use no setting" not in message and "no setting:" not in message


@pytest.mark.parametrize("flow", sorted(PRODUCT_NAMES))
def test_a_removal_entry_is_on_a_flow_that_had_the_setting(flow) -> None:
    """The keys of `removed_settings` are what the flow had on origin/main (`FORMER_FIELDS`) or
    already refused there (`EARLIER_REMOVALS`). A flow that never had a name refuses it as an
    unknown setting, not as a removed one."""
    cls = registered_flows[flow][1]
    known = EVERYWHERE | FORMER_FIELDS.get(flow, set()) | EARLIER_REMOVALS.get(flow, set())
    removed = set(cls.Settings.removed_setting_replacements())
    assert removed == known, f"{flow}: {sorted(removed ^ known)}"


def test_vivado_project_refuses_every_setting_of_a_simulation() -> None:
    from xeda.flows import VivadoSim

    for spelling, field in input_names(VivadoSim.Settings).items():
        if field in VivadoSynth.Settings.model_fields:
            continue
        with pytest.raises(FlowSettingsError) as refused:
            VivadoProject.Settings.from_input({**SYNTHESIS, spelling: None})
        assert f"`{spelling}` was removed: " in str(refused.value)
