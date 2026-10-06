"""PC Task 3: activity switches, explicit wiring and the removed synthesis settings."""

import ast
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from xeda.flow import FlowSettingsError
from xeda.flow.io import declared_inputs, declared_outputs, output_enabled
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoPostsynthSim

from .test_pc_equivalence import REQUESTS, launch, write_vivado_design


@pytest.mark.parametrize("saif", [None, Path("chosen.saif")])
@pytest.mark.parametrize("timing_sim", [False, True])
def test_each_activity_output_has_exactly_its_own_switch(saif, timing_sim):
    declarations = declared_outputs(VivadoPostsynthSim)
    assert set(declarations) == {"saif", "timing_saif"}
    settings = VivadoPostsynthSim.Settings.from_input(
        {"saif": saif, "timing_sim": timing_sim, **_legacy_settings()}
    )
    assert declarations["saif"].enabled_by == "saif"
    assert declarations["timing_saif"].enabled_by == "timing_sim"
    assert output_enabled(settings, declarations["saif"]) is (saif is not None)
    assert output_enabled(settings, declarations["timing_saif"]) is timing_sim


def _legacy_settings():
    # Keep the RED tests on the old model focused on missing declarations, rather than its
    # required nested field. This branch disappears naturally once the field is removed.
    return {"synth": {}} if "synth" in VivadoPostsynthSim.Settings.model_fields else {}


@pytest.mark.parametrize("name", ["saif", "timing_saif"])
def test_a_consumer_can_enable_activity_without_giving_a_filename(name):
    settings = VivadoPostsynthSim.Settings.from_input(_legacy_settings())
    VivadoPostsynthSim.enable_output(settings, name)
    assert settings.saif == Path("activity.saif")
    assert settings.timing_sim is (name == "timing_saif")


def test_postsynth_has_only_declared_dependencies():
    assert VivadoPostsynthSim.Settings.dependency_settings == {}
    assert "synth" not in VivadoPostsynthSim.Settings.model_fields
    assert set(declared_inputs(VivadoPostsynthSim)) == {"netlist", "netlist_timing", "sdf"}
    for cls in VivadoPostsynthSim.__mro__:
        if "init" in vars(cls):
            tree = ast.parse(textwrap.dedent(inspect.getsource(cls.init)))
            assert not [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr == "add_dependency"
            ]


@pytest.mark.parametrize("all_three", [False, True])
def test_alternative_bindings_pin_the_partial_binding_hazard(tmp_path, all_three):
    write_vivado_design(tmp_path)
    names = ("netlist", "netlist_timing", "sdf") if all_three else ("netlist",)
    settings = [
        "flows.vivado_synth.fpga=xc7a12tcsg325-1",
        "flows.vivado_synth.clock.period=5.0",
        "flows.vivado_alt_synth.fpga=xc7a12tcsg325-1",
        "flows.vivado_alt_synth.clock.period=5.0",
        "timing_sim=true",
        *(f"inputs.{name}=vivado_alt_synth.{name}" for name in names),
    ]
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
        "vivado_postsynth_sim", tmp_path / "design.yaml", flow_settings=settings
    )
    assert plan is not None
    producers = [node.name for node in plan.nodes if node.name != "vivado_postsynth_sim"]
    assert producers.count("vivado_alt_synth") == 1
    assert producers.count("vivado_synth") == (0 if all_three else 1)
    node = plan.node("vivado_postsynth_sim")
    for selected in node.inputs:
        expected = "vivado_alt_synth" if selected.name in names else "vivado_synth"
        assert len(selected.references) == 1
        assert selected.references[0].node == expected
        assert selected.references[0].output == selected.name


@pytest.mark.parametrize("origin", ["design", "project", "command line", "API"])
def test_removed_synth_reports_its_replacement_from_every_origin(tmp_path, origin):
    write_vivado_design(tmp_path)
    given = {"write_checkpoint": True}
    if origin == "API":
        with pytest.raises(FlowSettingsError) as exc:
            VivadoPostsynthSim.Settings.from_input({"synth": given}, design_root=tmp_path)
        message = str(exc.value)
    else:
        command = [
            sys.executable,
            "-m",
            "xeda",
            "run",
            "vivado_postsynth_sim",
            str(tmp_path / "design.yaml"),
            "--dry-run",
            "--json",
        ]
        section = "flows:\n  vivado_postsynth_sim:\n    synth:\n      write_checkpoint: true\n"
        if origin == "design":
            with (tmp_path / "design.yaml").open("a") as stream:
                stream.write(section)
        elif origin == "project":
            project = tmp_path / "project.yaml"
            project.write_text(section)
            command.extend(["--xedaproject", str(project)])
        else:
            command.extend(["-s", "flows.vivado_postsynth_sim.synth.write_checkpoint=true"])
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
        document = json.loads(proc.stdout)
        assert proc.returncode != 0 and not document["success"]
        message = document["error"]["message"]
    assert "was removed" in message and "flows.vivado_synth.write_checkpoint" in message


@pytest.mark.parametrize("timing_sim", [False, True])
def test_results_record_both_enabled_activity_names(tmp_path, monkeypatch, timing_sim):
    from dataclasses import replace

    request = replace(
        REQUESTS["vivado_postsynth_sim_functional"],
        settings=(
            *REQUESTS["vivado_postsynth_sim_functional"].settings,
            "saif=activity.saif",
            f"timing_sim={'true' if timing_sim else 'false'}",
        ),
    )
    captured = launch(request, tmp_path, monkeypatch)
    results = captured["nodes"]["sim/vivado_postsynth_sim"]["results"]
    outputs = results["outputs"]
    assert set(outputs) == ({"saif", "timing_saif"} if timing_sim else {"saif"})
    assert outputs["saif"]["path"].endswith("activity.saif")
    assert len(outputs["saif"]["sha"]) == 32
    if timing_sim:
        assert outputs["timing_saif"] == outputs["saif"]


def test_three_alternative_bindings_run_only_the_alternative_and_relaunch_nothing(
    tmp_path, monkeypatch
):
    from .tool_utils import fake_calls, launch_until_fresh, use_fake_tools

    write_vivado_design(tmp_path)
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_XSIM_STATE", "finish5")
    settings = [
        "flows.vivado_alt_synth.fpga=xc7a12tcsg325-1",
        "flows.vivado_alt_synth.clock.period=5.0",
        "timing_sim=true",
        *(
            f"inputs.{name}=vivado_alt_synth.{name}"
            for name in ("netlist", "netlist_timing", "sdf")
        ),
    ]
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    launch_flow = lambda: runner.run(
        "vivado_postsynth_sim", tmp_path / "design.yaml", flow_settings=settings
    )
    flow = launch_flow()
    assert flow.succeeded
    assert [f.name for f in runner.launched] == ["vivado_alt_synth", "vivado_postsynth_sim"]
    assert flow.inputs.netlist_timing.name == "impl_timesim.v"
    assert flow.inputs.sdf.name == "impl_timesim.sdf"
    assert set(flow.results["outputs"]) == {"saif", "timing_saif"}
    launch_until_fresh(runner, launch_flow)
    before = {f.name: fake_calls(f.run_path) for f in runner.launched}
    entered = len(runner.launched)
    again = launch_flow()
    assert again.reused and all(f.reused for f in runner.launched[entered:])
    assert {f.name: fake_calls(f.run_path) for f in runner.launched} == before
