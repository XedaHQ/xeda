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
from xeda.flow_runner.settings_layers import merge_flow_sections, merge_layers
from xeda.flows import GhdlSim, Nextpnr, VivadoPostsynthSim, VivadoSynth, YosysFpga
from xeda.xedaproject import XedaProject

from .project_files import PROJECT_FILE

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
    project = {
        "vivado_postsynth_sim": {"vcd_level": 1},
        "vivado_synth": {"fail_timing": True, "out_of_context": True},
    }
    design = {"vivado_synth": {"fpga": {"part": "xc7a100t"}, "out_of_context": False}}
    assert merge_flow_sections(project, design) == {
        "vivado_postsynth_sim": {"vcd_level": 1},
        "vivado_synth": {
            "fail_timing": True,
            "out_of_context": False,
            "fpga": {"part": "xc7a100t"},
        },
    }


def test_setting_aliases_are_one_key_across_precedence_layers():
    merged = merge_layers({"ncpus": 1}, {"nthreads": 3}, settings_cls=VivadoPostsynthSim.Settings)
    assert merged == {"nthreads": 3}
    producer = merge_layers({"ncpus": 2}, {"nthreads": 4}, settings_cls=VivadoSynth.Settings)
    assert producer == {"nthreads": 4}


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


def test_clock_spelling_precedence_is_applied_to_the_producers_own_settings():
    merged = merge_layers(
        {"clock_period": 10.0},
        {"clock": {"freq": 200.0, "port": "clk_i"}},
        settings_cls=VivadoSynth.Settings,
    )
    settings = VivadoSynth.Settings.from_input({"fpga": "xc7a100t", **merged})
    assert settings.main_clock is not None
    assert settings.main_clock.period == pytest.approx(5.0)
    assert settings.main_clock.port == "clk_i"


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
    """A reported error, like every other mistake in a `flows` table: the command line shows it
    without a traceback."""
    with pytest.raises(FlowSettingsError, match="given twice") as raised:
        merge_flow_sections(
            {"ghdl": {}, "ghdl_sim": {}},
            flow_class_for=lambda name: GhdlSim if name in {"ghdl", "ghdl_sim"} else None,
            location="the design file d.yaml",
        )
    assert raised.value.errors == [
        (
            "flows.ghdl_sim",
            "the design file d.yaml: `flows.ghdl_sim` is given twice in one `flows` section, as "
            "'ghdl' and 'ghdl_sim': keep one",
            None,
            "duplicate_flow",
        )
    ]


def test_a_flows_table_that_is_no_mapping_is_an_error_naming_it_and_its_origin():
    with pytest.raises(FlowSettingsError) as raised:
        merge_flow_sections({"verilator": {}}, [1], location="the project file p.yaml")
    assert raised.value.errors == [
        (
            "flows",
            "the project file p.yaml: `flows` must be a mapping of flow names to their "
            "settings, not [1]",
            None,
            "dict_type",
        )
    ]


def test_every_flow_section_that_is_no_mapping_is_reported_at_once():
    with pytest.raises(FlowSettingsError) as raised:
        merge_flow_sections({"verilator": 3, "ghdl_sim": {}, "nvc": "x", "yosys": None})
    assert [(path, kind) for path, _message, _context, kind in raised.value.errors] == [
        ("flows.verilator", "dict_type"),
        ("flows.nvc", "dict_type"),
    ]
    assert all("must be a mapping of settings" in error[1] for error in raised.value.errors)


def test_an_absent_table_or_section_is_empty():
    assert merge_flow_sections(None, {"verilator": None}) == {"verilator": {}}


def test_a_section_may_be_key_value_text_as_a_layer_is():
    """Code gives a layer of settings as `KEY=VALUE` text, a list or a tuple of it, empty too."""
    merged = merge_flow_sections(
        {"verilator": ["timing=true", "compile_args=-O3"], "ghdl": (), "nvc": []}
    )

    assert merged == {"verilator": {"timing": "true", "compile_args": "-O3"}, "ghdl": {}, "nvc": {}}


@pytest.mark.parametrize("section", ["x", 3, True, [1], ["x"], [{"a": 1}], ["a=1", 2]], ids=repr)
def test_no_other_section_is_a_layer(section):
    with pytest.raises(FlowSettingsError, match="`flows.verilator` must be a mapping"):
        merge_flow_sections({"verilator": section})


# ---------------------------------------------------------------------------------------------
# The runners use it
# ---------------------------------------------------------------------------------------------


class _Launched(Exception):
    pass


@pytest.fixture
def launched(monkeypatch):
    """What `DefaultRunner.run` would launch, without launching it."""
    calls = {}

    def run_flow(self, flow_class, design, flow_settings, all_flows_settings=None, **kwargs):
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
        tmp_path / PROJECT_FILE,
        """
        flows:
          vivado_postsynth_sim: {vcd_level: 1, ncpus: 1}
          vivado_synth: {fail_timing: true, ncpus: 2, out_of_context: true}
    """,
    )
    design = _write(
        tmp_path / "d.yaml",
        """
        name: d
        rtl: {sources: ["top.v"], top: top}
        flows:
          vivado_postsynth_sim: {nthreads: 3}
          vivado_synth: {fpga: {part: xc7a100t}, out_of_context: false, nthreads: 4}
    """,
    )
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            "vivado_postsynth_sim",
            design,
            flow_settings=["ncpus=5", "flows.vivado_synth.fail_timing=false"],
        )
    assert launched["settings"] == {"vcd_level": 1, "nthreads": "5"}
    assert launched["all_flows"]["vivado_synth"] == {
        "fail_timing": "false",
        "nthreads": 4,
        "out_of_context": False,
        "fpga": {"part": "xc7a100t"},
    }


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
    """Exercise both CLI combinations through the real runner layering."""
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
        tmp_path / PROJECT_FILE,
        """
        flows:
          vivado_postsynth_sim:
            vcd_level: 1
          vivado_synth: {fail_timing: true}
        design:
          - name: d
            rtl: {sources: [top.v], top: top}
            flows:
              vivado_postsynth_sim:
                vcd_level: 2
              vivado_synth: {fpga: {part: xc7a100t}, out_of_context: false}
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("vivado_postsynth_sim", "d", xedaproject=str(project))

    assert launched["settings"] == {"vcd_level": 2}
    assert launched["all_flows"]["vivado_synth"] == {
        "fail_timing": True,
        "out_of_context": False,
        "fpga": {"part": "xc7a100t"},
    }


def test_embedded_project_design_paths_are_relative_to_the_project_file(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project_path = _write(
        tmp_path / PROJECT_FILE,
        """
        design:
          - name: d
            rtl: {sources: [top.v], top: top}
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
        [flows.vivado_synth]
        fpga.part = "xc7a100t"
        fail_timing = true
        out_of_context = false
        """,
    )
    composed = {}

    def capture(settings):
        composed.update(settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "written_path_problems", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner(tmp_path / "xeda_run").run_remote(
            design, "vivado_synth", "host", flow_settings=["out_of_context=true"]
        )

    assert (composed["settings"].fail_timing, composed["settings"].out_of_context) == (
        True,
        True,
    )


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

    def capture(settings):
        captured.update(settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "written_path_problems", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner(tmp_path / "xeda_run").run_remote(
            design, "ghdl", "host", flow_settings=["warn_error=false"]
        )

    assert type(captured["settings"]).__qualname__ == "GhdlSim.Settings"
    assert captured["settings"].werror is False
    assert design.flow == before


def test_a_remote_run_layers_project_design_and_command_line(tmp_path, monkeypatch):
    """A remote run layers project design and command line."""
    from xeda.flow_runner import remote

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project = _write(
        tmp_path / PROJECT_FILE,
        """
        flows:
          vivado_synth:
            fpga: {part: xc7a100t}
            fail_timing: true
            ncpus: 1
        design:
          - name: d
            rtl: {sources: [top.v], top: top}
            flows:
              vivado_synth:
                ncpus: 2
                out_of_context: false
        """,
    )
    captured = {}

    def capture(settings):
        captured.update(settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "written_path_problems", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner(tmp_path / "xeda_run").run_remote(
            "d",
            "vivado_synth",
            "host",
            xedaproject=project,
            flow_settings=["fail_timing=false"],
        )

    settings = captured["settings"]
    assert type(settings).__qualname__ == "VivadoSynth.Settings"
    assert settings.nthreads == 2
    assert settings.fail_timing is False
    assert settings.out_of_context is False


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

    class Launcher(DefaultRunner):
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
        Launcher(tmp_path / "run"),
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
    only in `[flows.vivado_synth]` reaches `vivado_postsynth_sim` (and gets it past its required settings).
    """
    from xeda.flow_runner import remote

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.v"]
        top = "top"
        [flows.vivado_synth]
        fpga.part = "xc7a100t"
        """,
    )
    composed = {}

    def capture(settings):
        composed.update(settings=settings)
        raise _Launched

    monkeypatch.setattr(remote, "written_path_problems", capture)
    with pytest.raises(_Launched):
        remote.RemoteRunner(tmp_path / "xeda_run").run_remote(
            design, "vivado_postsynth_sim", "host"
        )
    assert composed["settings"].fpga.part == "xc7a100t"


def _one_design(tmp_path, flows_toml: str):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    return _write(
        tmp_path / "d.toml",
        f"""
        name = "d"
        [rtl]
        sources = ["top.v"]
        top = "top"
        {flows_toml}
        """,
    )


def test_a_design_files_own_producer_section_beats_the_project(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / PROJECT_FILE, "flows:\n  vivado_synth: {out_of_context: true}\n")
    design = _one_design(
        tmp_path, '[flows.vivado_synth]\nfpga.part = "xc7a100t"\nout_of_context = false\n'
    )
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("vivado_postsynth_sim", design)
    assert launched["all_flows"]["vivado_synth"]["out_of_context"] is False
    assert "synth" not in launched["settings"]


def test_removed_nesting_cannot_override_the_producers_own_section(tmp_path):
    design = _one_design(
        tmp_path,
        "[flows.vivado_postsynth_sim]\nsynth.out_of_context = true\n"
        '[flows.vivado_synth]\nfpga.part = "xc7a100t"\nout_of_context = false\n',
    )
    with pytest.raises(FlowSettingsError, match="was removed"):
        DefaultRunner(tmp_path / "run").plan("vivado_postsynth_sim", design)


def test_run_flow_composes_the_sections_it_is_given(tmp_path, monkeypatch):
    """The API keeps the consumer's settings and producer's section separate."""
    seen = {}

    def launch_flow(self, flow_class, design, flow_settings, **kwargs):
        seen.update(settings=flow_settings, sections=kwargs["all_flows_settings"])
        raise _Launched

    monkeypatch.setattr(DefaultRunner, "launch_flow", launch_flow)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run_flow(
            VivadoPostsynthSim,
            design,
            {"vcd_level": 2},
            all_flows_settings={"vivado_synth": {"fpga": "xc7a100t", "out_of_context": True}},
        )
    assert seen["settings"] == {"vcd_level": 2}
    assert seen["sections"]["vivado_synth"]["out_of_context"] is True


def test_composing_twice_changes_nothing(tmp_path):
    """`run()` composes, then `run_flow` composes its result again with the merged sections."""
    from xeda.flow_runner.settings_layers import compose_flow_settings

    project = {"nextpnr": {"yosys": {"flatten": True}, "seed": 1}}
    design = {"yosys_fpga": {"flatten": False, "abc9": True}}
    once = compose_flow_settings(Nextpnr, [project, design], {"seed": 3})
    merged = merge_flow_sections(project, design)
    assert compose_flow_settings(Nextpnr, [merged], once) == once


def test_dash_s_flows_node_key_sets_a_dependencys_setting(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    design = _one_design(
        tmp_path,
        '[flows.vivado_synth]\nfpga.part = "xc7a100t"\nout_of_context = true\n',
    )
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            "vivado_postsynth_sim",
            design,
            flow_settings=["flows.vivado_synth.out_of_context=false", "vcd_level=3"],
        )
    assert (
        launched["all_flows"]["vivado_synth"]["out_of_context"] == "false"
    )  # command line beats the design file
    assert launched["settings"]["vcd_level"] == "3"
    assert launched["all_flows"]["vivado_synth"]["out_of_context"] == "false"


def test_dash_s_key_and_flows_requested_key_are_one_leaf(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["seed=3", "flows.nextpnr.seed=3"]
        )
    assert launched["settings"]["seed"] == "3"
    with pytest.raises(FlowSettingsError, match=r"seed.*flows\.nextpnr\.seed"):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["seed=3", "flows.nextpnr.seed=4"]
        )


def test_no_flow_has_a_setting_named_flows():
    """`flows` is the command line's reserved prefix for `-s flows.<node>.key`."""
    from xeda.flow import registered_flows

    assert not [
        cls.name for _, cls in registered_flows.values() if "flows" in cls.Settings.model_fields
    ]


def test_dash_s_flows_must_name_a_flow_of_the_run(tmp_path, monkeypatch):
    """A typo in `-s flows.<name>` is an error with suggestions, never silently ignored."""
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError, match=r"yosys_fpg.*yosys_fpga"):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["flows.yosys_fpg.flatten=true"]
        )


def test_a_command_line_dependency_setting_beats_the_producers_file_value(
    tmp_path, monkeypatch, launched
):
    monkeypatch.chdir(tmp_path)
    design = _one_design(
        tmp_path,
        '[flows.vivado_synth]\nfpga.part = "xc7a100t"\nfail_timing = true\n',
    )
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            "vivado_postsynth_sim", design, flow_settings=["flows.vivado_synth.fail_timing=false"]
        )
    assert launched["all_flows"]["vivado_synth"]["fail_timing"] == "false"


def test_an_unknown_setting_in_a_lower_layer_is_still_reported(tmp_path, monkeypatch):
    """The merge keeps every name, so a higher layer never hides an unknown one."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / PROJECT_FILE, "flows:\n  nextpnr:\n    no_such_setting: 1\n")
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError, match="no_such_setting"):
        DefaultRunner(tmp_path / "run").run("nextpnr", design, flow_settings=["seed=3"])


def test_a_misdirected_setting_suggests_the_node_it_belongs_to(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError, match=r"-s flows\.yosys_fpga\.flatten"):
        DefaultRunner(tmp_path / "run").run("nextpnr", design, flow_settings=["flatten=true"])


def test_an_alias_and_its_setting_are_one_leaf_across_the_two_spellings(
    tmp_path, monkeypatch, launched
):
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError, match=r"ncpus.*flows\.nextpnr\.nthreads"):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["ncpus=3", "flows.nextpnr.nthreads=4"]
        )
