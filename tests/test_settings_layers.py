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
from xeda.flow import FlowSettingsError
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
    from xeda.flow_runner import remote

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


def test_running_a_design_mapping_does_not_modify_the_callers_mapping(tmp_path, launched):
    design = {
        "name": "d",
        "rtl": {"sources": [], "top": "top"},
    }
    before = deepcopy(design)

    with pytest.raises(_Launched):
        DefaultRunner(tmp_path / "run").run(VivadoSynth, design, flow_settings={"fpga": "x"})

    assert design == before
