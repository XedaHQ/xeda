"""How a flow's settings layers combine (`xeda.flow_runner.settings_layers`).

Lowest precedence first: the flow's defaults, the `flows` table of the host profile of a remote
run, the project's `flows` section, the design's
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
from xeda.flow_runner.settings_layers import (
    command_line_table,
    compose_flow_settings,
    merge_flow_sections,
    merge_layers,
)
from xeda.flows import Nextpnr, VivadoPostsynthSim, VivadoSynth
from xeda.project import Project

from .project_files import PROJECT_FILE
from .remote_fixtures import fake_remote, fake_ssh, remote_host, remote_request

FIXTURES = (fake_ssh, fake_remote, remote_host)

pytestmark = pytest.mark.python_compat


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


def test_a_host_profile_section_is_the_lowest_origin_of_a_flow():
    host = {"vivado_synth": {"nthreads": 8, "fail_timing": True}}
    project = {"vivado_synth": {"nthreads": 4}}
    design = {"vivado_synth": {"fail_timing": False}}

    assert compose_flow_settings(VivadoSynth, [host]) == host["vivado_synth"]
    assert compose_flow_settings(VivadoSynth, [host, project]) == {
        "nthreads": 4,
        "fail_timing": True,
    }
    assert compose_flow_settings(VivadoSynth, [host, project, design]) == {
        "nthreads": 4,
        "fail_timing": False,
    }


def test_a_design_section_refines_the_projects_section_for_the_same_flow():
    project = {
        "vivado_postsynth_sim": {"timeout": "1 s"},
        "vivado_synth": {"fail_timing": True, "out_of_context": True},
    }
    design = {"vivado_synth": {"fpga": {"part": "xc7a100t"}, "out_of_context": False}}
    assert merge_flow_sections(project, design) == {
        "vivado_postsynth_sim": {"timeout": "1 s"},
        "vivado_synth": {
            "fail_timing": True,
            "out_of_context": False,
            "fpga": {"part": "xc7a100t"},
        },
    }


def test_layered_clock_period_matches_direct_construction(tmp_path):
    """`clocks.main_clock.period` given as a layer must make the same settings as the clock given
    whole -- otherwise the two paths disagree on `flowrun_hash`."""
    merged = merge_layers(
        {"fpga": {"part": "xc7a12tcsg325-1"}},
        ["clocks.main_clock.period=5ns"],
        settings_cls=VivadoSynth.Settings,
    )
    layered = VivadoSynth.Settings.from_input(merged, design_root=tmp_path, runner_cwd=tmp_path)
    direct = VivadoSynth.Settings.from_input(
        {"fpga": {"part": "xc7a12tcsg325-1"}, "clocks": {"main_clock": {"period": "5 ns"}}},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )

    assert flowrun_hash("vivado_synth", layered) == flowrun_hash("vivado_synth", direct)


def test_higher_clock_timing_replaces_alternate_spelling_and_preserves_other_attributes():
    merged = merge_layers(
        {
            "clocks": {
                "main_clock": {
                    "port": "clk_i",
                    "period": "5 ns",
                    "uncertainty": "0.1 ns",
                    "duty_cycle": 0.4,
                }
            }
        },
        {"clocks": {"main_clock": {"freq": "250 MHz"}}},
        settings_cls=VivadoSynth.Settings,
    )
    settings = VivadoSynth.Settings.from_input({"fpga": {"part": "xc7a100t"}, **merged})

    assert settings.main_clock is not None
    assert settings.main_clock.period.to("ns") == pytest.approx(4.0)
    assert settings.main_clock.port == "clk_i"
    assert settings.main_clock.uncertainty.to("ns") == pytest.approx(0.1)
    assert settings.main_clock.duty_cycle == pytest.approx(0.4)


def test_clock_precedence_is_applied_to_the_producers_own_settings():
    merged = merge_layers(
        {"clocks": {"main_clock": {"period": "10 ns"}}},
        {"clocks": {"main_clock": {"freq": "200 MHz", "port": "clk_i"}}},
        settings_cls=VivadoSynth.Settings,
    )
    settings = VivadoSynth.Settings.from_input({"fpga": {"part": "xc7a100t"}, **merged})
    assert settings.main_clock is not None
    assert settings.main_clock.period.to("ns") == pytest.approx(5.0)
    assert settings.main_clock.port == "clk_i"


@pytest.mark.parametrize(
    "layers",
    [
        [{"clock_period": 4.0}],
        [{"clocks": {"main_clock": {"period": "5 ns"}}}, {"clock_period": 4.0}],
        [{"clock_period": 5.0}, {"clocks": {"main_clock": {"freq": "250 MHz"}}}],
        [{"clocks": {"main_clock": {"period": "5 ns"}}, "clock_period": 4.0}],
        [["clock_period=4.0"]],
    ],
)
def test_the_removed_clock_period_survives_layering_and_is_refused(layers):
    merged = merge_layers(*layers, settings_cls=VivadoSynth.Settings)

    with pytest.raises(FlowSettingsError, match="`clock_period` was removed"):
        VivadoSynth.Settings.from_input({"fpga": {"part": "xc7a100t"}, **merged})


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
        {"verilator": ["timing=true", "compile_args=-O3"], "ghdl_sim": (), "nvc": []}
    )

    assert merged == {
        "verilator": {"timing": "true", "compile_args": "-O3"},
        "ghdl_sim": {},
        "nvc": {},
    }


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
        designs: [{file: d.yaml}]
        flows:
          vivado_postsynth_sim: {timeout: 1 s, nthreads: 1}
          vivado_synth: {fail_timing: true, nthreads: 2, out_of_context: true}
    """,
    )
    _write(
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
            "d",
            xedaproject=str(tmp_path / PROJECT_FILE),
            flow_settings=["nthreads=5", "flows.vivado_synth.fail_timing=false"],
        )
    assert launched["settings"] == {"timeout": "1 s", "nthreads": 5}
    assert launched["all_flows"]["vivado_synth"] == {
        "fail_timing": False,
        "nthreads": 4,
        "out_of_context": False,
        "fpga": {"part": "xc7a100t"},
    }


@pytest.mark.parametrize(
    ("design_clock", "override", "expected"),
    [
        ('clocks.main_clock.period = "5 ns"', "clocks.main_clock.period=4ns", {"period": "4ns"}),
        ('clocks.main_clock.period = "5 ns"', "clocks.main_clock.freq=250MHz", {"freq": "250MHz"}),
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
        clocks = [{{ port = "clk" }}]
        [flows.vivado_synth]
        fpga.part = "xc7a100t"
        {design_clock}
        """,
    )

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("vivado_synth", design, flow_settings=[override])

    assert launched["settings"]["clocks"] == {"main_clock": expected}


def test_an_embedded_project_design_refines_project_flow_settings(tmp_path, monkeypatch, launched):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project = _write(
        tmp_path / PROJECT_FILE,
        """
        flows:
          vivado_postsynth_sim:
            timeout: 1 s
          vivado_synth: {fail_timing: true}
        designs:
          - name: d
            rtl: {sources: [top.v], top: top}
            flows:
              vivado_postsynth_sim:
                timeout: 2 s
              vivado_synth: {fpga: {part: xc7a100t}, out_of_context: false}
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("vivado_postsynth_sim", "d", xedaproject=str(project))

    assert launched["settings"] == {"timeout": "2 s"}
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
        designs:
          - name: d
            rtl: {sources: [top.v], top: top}
        """,
    )

    design = Project.load(project_path).design("d")

    assert design.root == tmp_path.resolve()
    assert design.rtl.sources[0].path == (tmp_path / "top.v").resolve()


def test_a_flow_section_is_applied_to_its_flow(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.vhd").write_text("entity top is end; architecture rtl of top is begin end;\n")
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.vhd"]
        top = "top"
        [flows.ghdl_sim]
        werror = true
        """,
    )

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run("ghdl_sim", design)

    assert launched["flow"] == "ghdl_sim"
    assert launched["settings"] == {"werror": True}
    assert launched["all_flows"] == {"ghdl_sim": {"werror": True}}


@pytest.fixture
def sent(monkeypatch, remote_host):
    """What `run_remote` hands to packaging, without running anything: the merged flow sections
    and the flows of the plan."""
    from xeda.remote import client

    captured = {}

    def package_inputs(design, sections, planned, **kwargs):
        captured.update(design=design, sections=sections, planned=planned)
        raise _Launched

    monkeypatch.setattr(client, "package_inputs", package_inputs)
    return captured


def _composed(sent, name, design_root):
    """The settings of the flow `name` that the remote composes from the sections it receives."""
    from pathlib import Path

    from xeda.flow_runner import get_flow_class

    return get_flow_class(name).Settings.from_input(
        sent["sections"][name], design_root=design_root, runner_cwd=Path.cwd()
    )


def test_a_remote_run_lets_the_command_line_win_over_the_design_file(tmp_path, sent):
    """The remote runner merged the other way round: the design file beat `-s`."""
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
    outcome = remote_request(
        tmp_path, design, "vivado_synth", flow_settings=["out_of_context=true"]
    )
    assert isinstance(outcome.error, _Launched), outcome.document
    settings = _composed(sent, "vivado_synth", tmp_path)
    assert (settings.fail_timing, settings.out_of_context) == (True, True)


def test_a_remote_run_composes_the_section_of_the_flow(tmp_path, sent):
    (tmp_path / "top.vhd").write_text("entity top is end; architecture rtl of top is begin end;\n")
    design = _write(
        tmp_path / "d.toml",
        """
        name = "d"
        [rtl]
        sources = ["top.vhd"]
        top = "top"
        [flows.ghdl_sim]
        werror = true
        """,
    )
    outcome = remote_request(tmp_path, design, "ghdl_sim", flow_settings=["werror=false"])
    assert isinstance(outcome.error, _Launched), outcome.document
    settings = _composed(sent, "ghdl_sim", tmp_path)
    assert type(settings).__qualname__ == "GhdlSim.Settings"
    assert settings.werror is False


def test_a_remote_run_layers_project_design_and_command_line(tmp_path, sent):
    """A remote run layers project design and command line."""
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    project = _write(
        tmp_path / PROJECT_FILE,
        """
        flows:
          vivado_synth:
            fpga: {part: xc7a100t}
            fail_timing: true
            nthreads: 1
        designs:
          - name: d
            rtl: {sources: [top.v], top: top}
            flows:
              vivado_synth:
                nthreads: 2
                out_of_context: false
        """,
    )
    outcome = remote_request(
        tmp_path,
        "d",
        "vivado_synth",
        xedaproject=project,
        flow_settings=["fail_timing=false"],
    )
    assert isinstance(outcome.error, _Launched), outcome.document
    settings = _composed(sent, "vivado_synth", tmp_path)
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
        root=tmp_path,
        rtl={"sources": ["top.v"], "top": "top", "clocks": ["clk"]},
        flows={
            "vivado_synth": {
                "clocks": {"main_clock": {"period": "5 ns"}},
                "fpga": {"part": "xc7a100t"},
            }
        },
    )
    before = deepcopy(design.flows)
    dse = Dse(
        NoopOptimizer,
        {},
        tmp_path / "run",
        variations={},
        max_workers=1,
    )

    dse.run("vivado_synth", design, flow_settings=["clocks.main_clock.period=4ns"])

    assert dse.optimizer.base_settings.main_clock.period.to("ns") == 4.0
    assert design.flows == before


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

    (tmp_path / "top.v").write_text("module top; endmodule\n")  # `yosys_fpga` reads a source
    design = Design(name="d", root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    outcome, index = Executioner(
        Launcher(tmp_path / "run"),
        design,
        Nextpnr,
        {"yosys_fpga": {"flatten": False, "abc9": True}},
    )((3, {"fpga": {"part": "LFE5U-25F-6BG381C"}}))

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
        fpga={"part": "xc7a100t"},
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
        fpga={"part": "xc7a100t"},
        clocks={
            "main_clock": {
                "name": "main_clock",
                "port": "clk_i",
                "period": "10 ns",
                "rise": "0.25 ns",
                "duty_cycle": 0.4,
                "uncertainty": "0.1 ns",
                "skew": "0.02 ns",
            },
            "aux": {
                "name": "aux",
                "port": "aux_i",
                "period": "20 ns",
                "rise": "0.5 ns",
                "duty_cycle": 0.6,
                "uncertainty": "0.2 ns",
                "skew": "0.03 ns",
            },
        },
    )
    optimizer.variations = {}

    batch = optimizer.next_batch()

    assert batch
    candidate = VivadoSynth.Settings.from_input(batch[0])
    assert set(candidate.clocks) == {"main_clock", "aux"}
    assert candidate.main_clock is not None
    assert candidate.main_clock.period.to("ns") == pytest.approx(5.0)
    assert candidate.main_clock.port == "clk_i"
    assert candidate.main_clock.rise.to("ns") == pytest.approx(0.25)
    assert candidate.main_clock.duty_cycle == pytest.approx(0.4)
    assert candidate.main_clock.uncertainty.to("ns") == pytest.approx(0.1)
    assert candidate.main_clock.skew.to("ns") == pytest.approx(0.02)
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
        fpga={"part": "xc7a100t"},
        clocks={
            "clk_a": {"name": "clk_a", "port": "a", "period": "10 ns"},
            "clk_b": {"name": "clk_b", "port": "b", "period": "20 ns"},
        },
    )
    optimizer.variations = {}

    batch = optimizer.next_batch()

    assert batch
    candidate = VivadoSynth.Settings.from_input(batch[0])
    assert candidate.clocks["clk_a"].period.to("ns") == pytest.approx(5.0)
    assert candidate.clocks["clk_b"].period.to("ns") == pytest.approx(20.0)


def test_running_a_design_mapping_does_not_modify_the_callers_mapping(tmp_path, launched):
    (tmp_path / "top.v").write_text("module top; endmodule\n")  # `vivado_synth` reads a source
    design = {
        "name": "d",
        "root": tmp_path,
        "rtl": {"sources": [str(tmp_path / "top.v")], "top": "top"},
    }
    before = deepcopy(design)

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            VivadoSynth, design, flow_settings={"fpga": {"part": "x"}}
        )

    assert design == before


# ---------------------------------------------------------------------------------------------
# Every spelling of a setting is the same setting
# ---------------------------------------------------------------------------------------------


def test_a_remote_run_composes_a_dependency_s_own_section_too(tmp_path, sent):
    """The remote runner composes a flow's settings as the local launcher does: the device given
    only in `[flows.vivado_synth]` reaches `vivado_postsynth_sim` (and gets it past its required
    settings, so that the plan is accepted and the sections go to the remote)."""
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
    outcome = remote_request(tmp_path, design, "vivado_postsynth_sim")
    assert isinstance(outcome.error, _Launched), outcome.document
    assert "vivado_postsynth_sim" in sent["planned"] and "vivado_synth" in sent["planned"]
    assert _composed(sent, "vivado_synth", tmp_path).fpga.part == "xc7a100t"


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
    design = Design(name="d", root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run_flow(
            VivadoPostsynthSim,
            design,
            {"timeout": "2 s"},
            all_flows_settings={
                "vivado_synth": {"fpga": {"part": "xc7a100t"}, "out_of_context": True}
            },
        )
    assert seen["settings"] == {"timeout": "2 s"}
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
            flow_settings=["flows.vivado_synth.out_of_context=false", "timeout=3s"],
        )
    assert (
        launched["all_flows"]["vivado_synth"]["out_of_context"] == False
    )  # command line beats the design file
    assert launched["settings"]["timeout"] == "3s"
    assert launched["all_flows"]["vivado_synth"]["out_of_context"] == False


def test_dash_s_key_and_flows_requested_key_are_one_leaf(tmp_path, monkeypatch, launched):
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["seed=3", "flows.nextpnr.seed=3"]
        )
    assert launched["settings"]["seed"] == 3
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
    assert launched["all_flows"]["vivado_synth"]["fail_timing"] == False


def test_an_unknown_setting_in_a_lower_layer_is_still_reported(tmp_path, monkeypatch):
    """The merge keeps every name, so a higher layer never hides an unknown one."""
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    _write(
        tmp_path / PROJECT_FILE,
        f"designs: [{{file: {design.name}}}]\nflows:\n  nextpnr:\n    no_such_setting: 1\n",
    )
    with pytest.raises(FlowSettingsError, match="no_such_setting"):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr",
            design.stem,
            xedaproject=str(tmp_path / PROJECT_FILE),
            flow_settings=["seed=3"],
        )


def test_a_misdirected_setting_suggests_the_node_it_belongs_to(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError, match=r"-s yosys_fpga\.flatten"):
        DefaultRunner(tmp_path / "run").run("nextpnr", design, flow_settings=["flatten=true"])


def test_an_alias_and_its_setting_are_one_leaf_across_the_two_spellings(
    tmp_path, monkeypatch, launched
):
    monkeypatch.chdir(tmp_path)
    design = _one_design(tmp_path, '[flows.nextpnr]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError, match=r"nthreads.*flows\.nextpnr\.nthreads"):
        DefaultRunner(tmp_path / "run").run(
            "nextpnr", design, flow_settings=["nthreads=3", "flows.nextpnr.nthreads=4"]
        )


def test_the_command_line_table_holds_the_settings_by_node():
    """The items of `-s` are the `spec.settings` that the run document shows."""
    table = command_line_table(
        ["nextpnr.clock.period=5ns", "nextpnr.clock.port=clk", "yosys_fpga.abc9=true"]
    )
    assert table == {
        "nextpnr": {"clock": {"period": "5ns", "port": "clk"}},
        "yosys_fpga": {"abc9": "true"},
    }
