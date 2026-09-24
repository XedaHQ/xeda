"""How a flow's settings layers combine (`xeda.flow_runner.settings_layers`).

Lowest precedence first: the flow's defaults, the project's `flows` section, the design's
`[flows.<flow>]` section, the command line's `-s`. Layers merge deeply -- `-s yosys.flatten=true`
changes one setting of a nested section instead of replacing it -- and a local run, a remote run
and a dependency's settings all combine them the same way.
"""

import textwrap
from copy import deepcopy
from types import MappingProxyType

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError, flowrun_hash
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import dependency_settings
from xeda.flow_runner.settings_layers import merge_flow_sections, merge_layers
from xeda.flows import GhdlSim, Nextpnr, VivadoSynth, YosysFpga
from xeda.xedaproject import XedaProject

# ---------------------------------------------------------------------------------------------
# The merge itself
# ---------------------------------------------------------------------------------------------


def test_a_higher_layer_refines_a_nested_section_key_by_key():
    design = {"fpga": {"part": "LFE5U-25F-6BG381C"}, "yosys": {"abc9": True, "flatten": False}}

    merged = merge_layers(design, ["yosys.flatten=true"])

    assert merged == {
        "fpga": {"part": "LFE5U-25F-6BG381C"},
        "yosys": {"abc9": True, "flatten": "true"},
    }


def test_later_layers_win_and_lists_are_replaced_whole():
    merged = merge_layers(
        {"seed": 1, "xdc_files": ["a.xdc", "b.xdc"]},
        {"seed": 2},
        {"xdc_files": ["c.xdc"]},
    )
    assert merged == {"seed": 2, "xdc_files": ["c.xdc"]}


def test_mapping_inputs_follow_the_mapping_api_contract():
    project = MappingProxyType({"yosys": {"flatten": True}})
    command_line = MappingProxyType({"yosys.abc9": False})

    assert merge_layers(project, command_line) == {"yosys": {"flatten": True, "abc9": False}}


def test_layers_may_be_dotted_mappings_or_key_value_strings():
    assert merge_layers({"yosys.flatten": True}, ["yosys.abc9=true"]) == {
        "yosys": {"flatten": True, "abc9": "true"}
    }


def test_merging_leaves_every_layer_unchanged():
    design = {"yosys": {"abc9": True}}
    merge_layers(design, {"yosys": {"flatten": True}})
    assert design == {"yosys": {"abc9": True}}


def test_a_design_section_refines_the_projects_section_for_the_same_flow():
    project = {"nextpnr": {"seed": 1, "yosys": {"abc9": True}}, "yosys_fpga": {"flatten": True}}
    design = {"nextpnr": {"fpga": {"part": "LFE5U-25F-6BG381C"}, "yosys": {"flatten": False}}}

    assert merge_flow_sections(project, design) == {
        "nextpnr": {
            "seed": 1,
            "yosys": {"abc9": True, "flatten": False},
            "fpga": {"part": "LFE5U-25F-6BG381C"},
        },
        "yosys_fpga": {"flatten": True},
    }


def test_setting_aliases_are_one_key_across_precedence_layers():
    merged = merge_layers(
        {"ncpus": 1, "yosys": {"ncpus": 2}},
        {"nthreads": 3, "yosys": {"nthreads": 4}},
        settings_cls=Nextpnr.Settings,
    )

    assert merged["nthreads"] == 3
    assert merged["yosys"]["nthreads"] == 4
    assert "ncpus" not in merged and "ncpus" not in merged["yosys"]


def test_two_names_for_one_setting_in_one_layer_are_still_an_error():
    merged = merge_layers({"ncpus": 1, "nthreads": 2}, settings_cls=YosysFpga.Settings)

    with pytest.raises(FlowSettingsError, match="nthreads"):
        YosysFpga.Settings.from_input(merged)


@pytest.mark.parametrize(
    ("lower", "higher", "expected_period"),
    [
        ({"clock": {"period": 5.0}}, {"clock_period": 4.0}, 4.0),
        ({"clock_period": 5.0}, {"clock": {"freq": 250.0}}, 4.0),
        (
            {"clocks": {"main_clock": {"port": "clk_i", "period": 5.0}}},
            {"clock_period": 4.0},
            4.0,
        ),
    ],
)
def test_clock_spellings_are_one_concept_across_precedence_layers(lower, higher, expected_period):
    merged = merge_layers(lower, higher, settings_cls=VivadoSynth.Settings)
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a100t", **merged})

    assert list(settings.clocks) == ["main_clock"]
    assert settings.main_clock is not None
    assert settings.main_clock.period == pytest.approx(expected_period)


def test_layered_clock_period_matches_direct_construction(tmp_path):
    """`clock_period` normalized through `merge_layers` must name the clock the same way
    `SynthFlow.Settings._synthflow_settings_root_validator` does when settings are constructed
    directly -- otherwise the two paths disagree on `clocks["main_clock"].name` and thus on
    `flowrun_hash`, even though the settings mean the same thing."""
    merged = merge_layers(
        {"fpga": "xc7a12tcsg325-1"}, ["clock_period=5"], settings_cls=VivadoSynth.Settings
    )
    layered = VivadoSynth.Settings.from_input(merged, design_root=tmp_path, runner_cwd=tmp_path)
    direct = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a12tcsg325-1", "clock_period": 5},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )

    assert layered.clocks["main_clock"].name == "main_clock"
    assert flowrun_hash("vivado_synth", layered) == flowrun_hash("vivado_synth", direct)


def test_unnamed_singular_override_redirected_onto_a_named_clock_keeps_its_identity():
    """A bare `clock_period=5` refining an existing, differently-named lower-layer clock must
    not stamp that clock's identity to `"main_clock"` -- it targets the existing clock (by the
    `singular_clock == "main_clock"` redirect in `_merge_settings_layer`), and only a genuinely
    *new* clock entry gets the synthesized `"main_clock"` name."""
    merged = merge_layers(
        {"clocks": {"clk": {"name": "clk", "period": 10, "port": "clk_i"}}},
        ["clock_period=5"],
        settings_cls=VivadoSynth.Settings,
    )

    assert merged["clocks"] == {"clk": {"name": "clk", "port": "clk_i", "period": "5"}}


def test_unnamed_singular_override_redirected_onto_an_unnamed_clock_stays_unnamed():
    """Same redirect, but the lower layer's sole clock never had an explicit `name` either --
    the override must not invent one."""
    merged = merge_layers(
        {"clocks": {"clk": {"period": 10, "port": "clk_i"}}},
        ["clock.period=5"],
        settings_cls=VivadoSynth.Settings,
    )

    assert merged["clocks"] == {"clk": {"port": "clk_i", "period": "5"}}
    assert merged["clocks"]["clk"].get("name") != "main_clock"


def test_higher_clock_timing_replaces_alternate_spelling_and_preserves_other_attributes():
    merged = merge_layers(
        {
            "clock": {
                "port": "clk_i",
                "period": 5.0,
                "uncertainty": 0.1,
                "duty_cycle": 0.4,
            }
        },
        {"clock": {"freq": 250.0}},
        settings_cls=VivadoSynth.Settings,
    )
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a100t", **merged})

    assert settings.main_clock is not None
    assert settings.main_clock.period == pytest.approx(4.0)
    assert settings.main_clock.port == "clk_i"
    assert settings.main_clock.uncertainty == pytest.approx(0.1)
    assert settings.main_clock.duty_cycle == pytest.approx(0.4)


def test_clock_spelling_precedence_is_applied_recursively_to_dependency_settings():
    merged = merge_layers(
        {"yosys": {"clock_period": 10.0}},
        {"yosys": {"clock": {"freq": 200.0, "port": "clk_i"}}},
        settings_cls=Nextpnr.Settings,
    )
    settings = Nextpnr.Settings.from_input({"fpga": "LFE5U-25F-6BG381C", **merged})

    assert settings.yosys.main_clock is not None
    assert settings.yosys.main_clock.period == pytest.approx(5.0)
    assert settings.yosys.main_clock.port == "clk_i"


def test_mixed_clock_spellings_in_one_layer_are_still_an_error():
    merged = merge_layers(
        {"clock": {"period": 5.0}, "clock_period": 4.0},
        settings_cls=VivadoSynth.Settings,
    )

    with pytest.raises(FlowSettingsError, match="cannot be combined"):
        VivadoSynth.Settings.from_input({"fpga": "xc7a100t", **merged})


def test_single_clock_override_targets_the_deterministic_main_of_a_multi_clock_layer():
    merged = merge_layers(
        {"clocks": {"clk_a": {"period": 5.0}, "clk_b": {"period": 10.0}}},
        {"clock_period": 4.0},
        settings_cls=VivadoSynth.Settings,
    )
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a100t", **merged})

    assert settings.clocks["clk_a"].period == pytest.approx(4.0)
    assert settings.clocks["clk_b"].period == pytest.approx(10.0)


def test_flow_sections_keep_single_clock_semantics_until_after_layering():
    merged = merge_flow_sections(
        {"vivado_synth": {"clocks": {"system": {"port": "clk_i", "period": 5.0}}}},
        {"vivado_synth": {"clock_period": 4.0}},
        flow_class_for=lambda name: VivadoSynth if name == "vivado_synth" else None,
    )
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a100t", **merged["vivado_synth"]})

    assert list(settings.clocks) == ["system"]
    assert settings.clocks["system"].period == pytest.approx(4.0)
    assert settings.clocks["system"].port == "clk_i"


def test_flow_aliases_are_one_section_across_precedence_layers():
    merged = merge_flow_sections(
        {"ghdl": {"warn_error": False}},
        {"ghdl_sim": {"warn_error": True}},
        flow_class_for=lambda name: GhdlSim if name in {"ghdl", "ghdl_sim"} else None,
    )

    assert merged == {"ghdl_sim": {"werror": True}}


def test_two_names_for_one_flow_in_one_section_are_an_error():
    with pytest.raises(ValueError, match="given twice"):
        merge_flow_sections(
            {"ghdl": {}, "ghdl_sim": {}},
            flow_class_for=lambda name: GhdlSim if name in {"ghdl", "ghdl_sim"} else None,
        )


# ---------------------------------------------------------------------------------------------
# The runners use it
# ---------------------------------------------------------------------------------------------


class _Launched(Exception):
    pass


@pytest.fixture
def launched(monkeypatch):
    """What `DefaultRunner.run` would launch, without launching it."""
    calls = {}

    def run_flow(self, flow_class, design, flow_settings, run_path=None, all_flows_settings=None):
        calls.update(flow=flow_class.name, settings=flow_settings, all_flows=all_flows_settings)
        raise _Launched

    monkeypatch.setattr(DefaultRunner, "run_flow", run_flow)
    return calls


def _write(path, text):
    path.write_text(textwrap.dedent(text))
    return path


def test_a_local_run_layers_project_design_and_command_line(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    _write(
        tmp_path / "xedaproject.toml",
        """
        [flows.nextpnr]
        seed = 1
        ncpus = 1
        yosys.abc9 = true
        yosys.ncpus = 2
        [flows.yosys_fpga]
        flatten = true
        """,
    )
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.v"]
        top = "top"
        [flows.nextpnr]
        fpga.part = "LFE5U-25F-6BG381C"
        nthreads = 3
        yosys.flatten = false
        yosys.nthreads = 4
        """,
    )

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["ncpus=5", "yosys.abc9=false"]
        )

    assert launched["settings"] == {
        "seed": 1,
        "nthreads": "5",
        "yosys": {"abc9": "false", "nthreads": 4, "flatten": False},
        "fpga": {"part": "LFE5U-25F-6BG381C"},
    }
    assert launched["all_flows"]["yosys_fpga"] == {"flatten": True}


@pytest.mark.parametrize(
    ("design_clock", "override", "expected"),
    [
        ("clock.period = 5.0", "clock_period=4.0", {"period": "4.0"}),
        ("clock_period = 5.0", "clock.freq=250.0", {"freq": "250.0"}),
    ],
)
def test_local_run_allows_higher_clock_spelling_to_override_design_layer(
    tmp_path, monkeypatch, launched, design_clock, override, expected
):
    """Exercise both reviewer-reported CLI combinations through the real runner layering."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    design = _write(
        tmp_path / "d.toml",
        f"""
        name = "d"
        [rtl]
        sources = ["top.v"]
        top = "top"
        clock = {{ port = "clk" }}
        [flows.vivado_synth]
        fpga.part = "xc7a100t"
        {design_clock}
        """,
    )

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("vivado_synth", design, flow_settings=[override])

    assert launched["settings"]["clocks"] == {"main_clock": {**expected, "name": "main_clock"}}


def test_an_embedded_project_design_refines_project_flow_settings(tmp_path, monkeypatch, launched):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project = _write(
        tmp_path / "xedaproject.toml",
        """
        [flows.nextpnr]
        seed = 1
        yosys.abc9 = true

        [[design]]
        name = "d"
        [design.rtl]
        sources = ["top.v"]
        top = "top"
        [design.flows.nextpnr]
        seed = 2
        yosys.flatten = false
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("nextpnr", "d", xedaproject=str(project))

    assert launched["settings"] == {
        "seed": 2,
        "yosys": {"abc9": True, "flatten": False},
    }


def test_embedded_project_design_paths_are_relative_to_the_project_file(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project_path = _write(
        tmp_path / "xedaproject.toml",
        """
        [[design]]
        name = "d"
        [design.rtl]
        sources = ["top.v"]
        top = "top"
        """,
    )

    project = XedaProject.from_file(project_path)
    design = project.get_design("d")

    assert design is not None
    assert design.root_path == tmp_path.resolve()
    assert design.rtl.sources[0].path == (tmp_path / "top.v").resolve()


def test_a_flow_section_written_with_an_alias_is_applied(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.vhd").write_text("entity top is end; architecture rtl of top is begin end;\n")
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.vhd"]
        top = "top"
        [flows.ghdl]
        warn_error = true
        """,
    )

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("ghdl", design)

    assert launched["flow"] == "ghdl_sim"
    assert launched["settings"] == {"werror": True}
    assert launched["all_flows"] == {"ghdl_sim": {"werror": True}}


def test_a_remote_run_lets_the_command_line_win_over_the_design_file(tmp_path, monkeypatch):
    """The remote runner merged the other way round: the design file beat `-s`."""
    from xeda.flow_runner import remote

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.v"]
        top = "top"
        [flows.nextpnr]
        fpga.part = "LFE5U-25F-6BG381C"
        yosys.abc9 = true
        yosys.flatten = false
        """,
    )
    composed = {}

    def capture(flow_name, settings):
        composed.update(settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "flow_run_hash", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner().run_remote(
            design, "nextpnr", "host", flow_settings=["yosys.flatten=true"]
        )

    assert (composed["settings"].yosys.abc9, composed["settings"].yosys.flatten) == (True, True)


def test_a_remote_run_canonicalizes_the_flow_name_and_does_not_mutate_the_design(
    tmp_path, monkeypatch
):
    from xeda.flow_runner import remote

    (tmp_path / "top.vhd").write_text("entity top is end; architecture rtl of top is begin end;\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["top.vhd"], "top": "top"},
        flows={"ghdl": {"warn_error": True}},
    )
    before = deepcopy(design.flow)
    captured = {}

    def capture(flow_name, settings):
        captured.update(flow_name=flow_name, settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "flow_run_hash", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner().run_remote(design, "ghdl", "host", flow_settings=["warn_error=false"])

    assert captured["flow_name"] == "ghdl_sim"
    assert captured["settings"].werror is False
    assert design.flow == before


def test_a_remote_run_layers_project_design_and_command_line(tmp_path, monkeypatch):
    """A remote run layers project design and command line."""
    from xeda.flow_runner import remote

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project = _write(
        tmp_path / "xedaproject.toml",
        """
        [flows.nextpnr]
        fpga.part = "LFE5U-25F-6BG381C"
        seed = 1
        yosys.abc9 = true

        [[design]]
        name = "d"
        [design.rtl]
        sources = ["top.v"]
        top = "top"
        [design.flows.nextpnr]
        seed = 2
        yosys.flatten = false
        """,
    )
    captured = {}

    def capture(flow_name, settings):
        captured.update(flow_name=flow_name, settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "flow_run_hash", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner().run_remote(
            "d",
            "nextpnr",
            "host",
            xedaproject=project,
            flow_settings=["yosys.abc9=false"],
        )

    settings = captured["settings"]
    assert captured["flow_name"] == "nextpnr"
    assert settings.seed == 2
    assert settings.yosys.abc9 is False
    assert settings.yosys.flatten is False


def test_a_dependency_refines_the_design_section_for_its_flow():
    """`[flows.yosys_fpga]` is the base; what `nextpnr` hands its yosys dependency wins."""
    given = YosysFpga.Settings(flatten=False, fpga="LFE5U-25F-6BG381C")
    depender = Nextpnr.Settings(verbose=2, debug=True)

    settings = dependency_settings(
        YosysFpga, given, depender, {"yosys_fpga": {"flatten": True, "abc9": True}}
    )

    assert (settings.flatten, settings.abc9) == (False, True)
    assert settings.fpga is not None and settings.fpga.part == "LFE5U-25F-6BG381C"
    assert (settings.debug, settings.verbose) == (True, 2)


def test_dse_does_not_reapply_or_remove_design_settings(tmp_path, monkeypatch):
    from xeda.flow_runner.dse.dse_runner import Dse, Optimizer

    class NoopOptimizer(Optimizer):
        default_variations = {"vivado_synth": {}}

        def next_batch(self):
            return None

    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["top.v"], "top": "top"},
        flows={
            "vivado_synth": {
                "clock_period": 5.0,
                "fpga": "xc7a100t",
            }
        },
    )
    before = deepcopy(design.flow)
    dse = Dse(
        NoopOptimizer,
        {},
        tmp_path / "run",
        variations={},
        max_workers=1,
    )

    dse.run("vivado_synth", design, flow_settings=["clock_period=4.0"])

    assert dse.optimizer.base_settings.clock_period == 4.0
    assert design.flow == before


def test_dse_candidates_keep_all_flow_sections_for_dependencies(tmp_path):
    from types import SimpleNamespace

    from xeda.flow import Flow
    from xeda.flow_runner.dse.dse_runner import Executioner

    class Launcher:
        def launch_flow(self, flow_class, design, flow_settings, **kwargs):
            assert kwargs["all_flows_settings"] == {"yosys_fpga": {"flatten": False, "abc9": True}}
            return SimpleNamespace(
                settings=Flow.Settings(),
                results=Flow.Results(),
                timestamp=None,
                run_path=None,
            )

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "top"})
    outcome, index = Executioner(
        Launcher(),
        design,
        Nextpnr,
        {"yosys_fpga": {"flatten": False, "abc9": True}},
    )((3, {"fpga": "LFE5U-25F-6BG381C"}))

    assert outcome is not None
    assert index == 3


def test_fmax_variations_refine_nested_base_settings_instead_of_replacing_them():
    from xeda.flow_runner.dse.fmax import FmaxOptimizer

    optimizer = FmaxOptimizer(
        max_workers=1,
        settings=FmaxOptimizer.Settings(init_freq_low=100.0, init_freq_high=200.0),
    )
    optimizer.flow_class = VivadoSynth
    optimizer.base_settings = VivadoSynth.Settings(
        fpga="xc7a100t",
        synth={"steps": {"SYNTH_DESIGN": {"ARGS": {"CUSTOM": "yes"}}}},
    )
    optimizer.variations = {"synth.strategy": ["Flow_PerfOptimized_high"]}

    batch = optimizer.next_batch()

    assert batch
    assert batch[0]["synth"]["strategy"] == "Flow_PerfOptimized_high"
    assert batch[0]["synth"]["steps"]["SYNTH_DESIGN"]["ARGS"]["CUSTOM"] == "yes"


def test_fmax_variations_change_only_the_target_clock_period():
    from xeda.flow_runner.dse.fmax import FmaxOptimizer

    optimizer = FmaxOptimizer(
        max_workers=1,
        settings=FmaxOptimizer.Settings(init_freq_low=100.0, init_freq_high=200.0),
    )
    optimizer.flow_class = VivadoSynth
    optimizer.base_settings = VivadoSynth.Settings(
        fpga="xc7a100t",
        clocks={
            "main_clock": {
                "name": "main_clock",
                "port": "clk_i",
                "period": 10.0,
                "rise": 0.25,
                "duty_cycle": 0.4,
                "uncertainty": 0.1,
                "skew": 0.02,
            },
            "aux": {
                "name": "aux",
                "port": "aux_i",
                "period": 20.0,
                "rise": 0.5,
                "duty_cycle": 0.6,
                "uncertainty": 0.2,
                "skew": 0.03,
            },
        },
    )
    optimizer.variations = {}

    batch = optimizer.next_batch()

    assert batch
    candidate = VivadoSynth.Settings.from_input(batch[0])
    assert set(candidate.clocks) == {"main_clock", "aux"}
    assert candidate.main_clock is not None
    assert candidate.main_clock.period == pytest.approx(5.0)
    assert candidate.main_clock.port == "clk_i"
    assert candidate.main_clock.rise == pytest.approx(0.25)
    assert candidate.main_clock.duty_cycle == pytest.approx(0.4)
    assert candidate.main_clock.uncertainty == pytest.approx(0.1)
    assert candidate.main_clock.skew == pytest.approx(0.02)
    assert (
        candidate.clocks["aux"].model_dump() == optimizer.base_settings.clocks["aux"].model_dump()
    )


def test_fmax_uses_the_deterministic_main_when_multiple_clocks_are_not_named_main_clock():
    from xeda.flow_runner.dse.fmax import FmaxOptimizer

    optimizer = FmaxOptimizer(
        max_workers=1,
        settings=FmaxOptimizer.Settings(init_freq_low=100.0, init_freq_high=200.0),
    )
    optimizer.flow_class = VivadoSynth
    optimizer.base_settings = VivadoSynth.Settings(
        fpga="xc7a100t",
        clocks={
            "clk_a": {"name": "clk_a", "port": "a", "period": 10.0},
            "clk_b": {"name": "clk_b", "port": "b", "period": 20.0},
        },
    )
    optimizer.variations = {}

    batch = optimizer.next_batch()

    assert batch
    candidate = VivadoSynth.Settings.from_input(batch[0])
    assert candidate.clocks["clk_a"].period == pytest.approx(5.0)
    assert candidate.clocks["clk_b"].period == pytest.approx(20.0)


def test_running_a_design_mapping_does_not_modify_the_callers_mapping(tmp_path, launched):
    design = {
        "name": "d",
        "rtl": {"sources": [], "top": "top"},
    }
    before = deepcopy(design)

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(VivadoSynth, design, flow_settings={"fpga": "x"})

    assert design == before


# ---------------------------------------------------------------------------------------------
# Every spelling of a setting is the same setting
# ---------------------------------------------------------------------------------------------


def test_every_accepted_spelling_of_a_setting_gets_the_same_input_conveniences(tmp_path):
    """The layer merge and a flow's input conveniences resolve spellings with one table: when
    they had one each, only the merge knew `validation_alias` choices, so a setting given by one
    of them was merged right but skipped `$DESIGN_ROOT` expansion and comma-separated lists."""
    from pathlib import Path
    from typing import List

    from xeda.dataclass import AliasChoices, Field
    from xeda.flow import Flow

    class Settings(Flow.Settings):
        script: Path = Field(
            Path("x"), validation_alias=AliasChoices("script", "tcl"), description="a path"
        )
        files: List[str] = Field(
            [], validation_alias=AliasChoices("files", "srcs"), description="a list"
        )

    given = Settings.from_input(
        {"tcl": "$DESIGN_ROOT/run.tcl", "srcs": "a, b"}, design_root=tmp_path
    )
    assert given.script == tmp_path / "run.tcl"
    assert given.files == ["a", "b"]
    assert merge_layers({"tcl": "lower"}, {"script": "higher"}, settings_cls=Settings) == {
        "script": "higher"
    }


def test_a_remote_run_composes_a_dependency_s_own_section_too(tmp_path, monkeypatch):
    """The remote runner composes a flow's settings as the local launcher does: the device given
    only in `[flows.yosys_fpga]` reaches `nextpnr` (and gets it past its required settings)."""
    from xeda.flow_runner import remote

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.v"]
        top = "top"
        [flows.yosys_fpga]
        fpga.part = "LFE5U-25F-6BG381C"
        """,
    )
    composed = {}

    def capture(flow_name, settings):
        composed.update(settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "flow_run_hash", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner().run_remote(design, "nextpnr", "host")
    assert composed["settings"].yosys.fpga.part == "LFE5U-25F-6BG381C"
