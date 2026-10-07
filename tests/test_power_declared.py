"""Declared timing activity, removed propagation and the power-only verdict.

Power no longer belongs to the simulation-evidence sweep: its producer must succeed
before the launcher hands over activity. The reporter checks only its own power report.
"""

import json
import subprocess
import sys

from pathlib import Path

import pytest

from xeda.flow import FlowSettingsError, SimFlow
from xeda.flow.io import declared_inputs, declared_outputs, output_enabled
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoPower

from .test_tool_input_equivalence import VIVADO_SETTINGS, write_vivado_design

REMOVED = {
    "postsynthsim": ({}, "flows.vivado_postsynth_sim.<key>"),
    "elab_debug": ("typical", "flows.vivado_postsynth_sim.elab_debug"),
    "saif": ("activity.saif", "flows.vivado_postsynth_sim.saif"),
    "stop_time": ("10ns", "flows.vivado_postsynth_sim.stop_time"),
    "prerun_time": ("1ns", "flows.vivado_postsynth_sim.prerun_time"),
    "timeout": (10.0, "flows.vivado_postsynth_sim.timeout"),
    "fail_severity": ("error", "flows.vivado_postsynth_sim.fail_severity"),
    "timing_sim": (
        True,
        "switches `flows.vivado_postsynth_sim.timing_sim` on itself, so remove it",
    ),
}


def test_power_is_a_reporter_with_only_declared_producers():
    assert not issubclass(VivadoPower, SimFlow)
    assert not any(key.startswith("sim.") for key in VivadoPower.results_description)
    declarations = declared_inputs(VivadoPower)
    assert set(declarations) == {"activity", "checkpoint"}
    assert declarations["activity"].producer == "vivado_postsynth_sim"
    assert declarations["activity"].output == "timing_saif"
    assert declarations["checkpoint"].producer == "vivado_synth"
    assert declarations["checkpoint"].output == "checkpoint_route"
    assert all(declaration.required for declaration in declarations.values())
    assert not set(REMOVED) & VivadoPower.Settings.model_fields.keys()


def test_power_demands_timing_activity_without_configuring_the_producer(tmp_path):
    write_vivado_design(tmp_path)
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
        "vivado_power", tmp_path / "design.yaml", flow_settings=VIVADO_SETTINGS
    )
    assert [node.name for node in plan.nodes] == [
        "vivado_synth",
        "vivado_postsynth_sim",
        "vivado_power",
    ]
    simulation = plan.node("vivado_postsynth_sim")
    assert simulation.settings.timing_sim is True
    assert simulation.settings.saif == Path("outputs/sim.saif")
    assert "timing_saif" in simulation.switched_on
    synth = plan.node("vivado_synth")
    assert {"netlist", "netlist_timing", "sdf", "checkpoint_route"} <= {
        name
        for name, declaration in declared_outputs(synth.flow_class).items()
        if output_enabled(synth.settings, declaration)
    }
    assert synth.settings.write_netlist and synth.settings.write_timing_netlist
    assert synth.settings.write_checkpoint


@pytest.mark.parametrize("name", REMOVED)
@pytest.mark.parametrize("origin", ["design", "project", "command line", "API"])
def test_removed_power_setting_names_its_replacement_from_every_origin(tmp_path, name, origin):
    import yaml

    write_vivado_design(tmp_path)
    given, replacement = REMOVED[name]
    if origin == "API":
        with pytest.raises(FlowSettingsError) as exc:
            VivadoPower.Settings.from_input({name: given}, design_root=tmp_path)
        message = str(exc.value)
    else:
        command = [
            sys.executable,
            "-m",
            "xeda",
            "run",
            "vivado_power",
            str(tmp_path / "design.yaml"),
            "--dry-run",
            "--json",
        ]
        section = yaml.safe_dump({"flows": {"vivado_power": {name: given}}})
        if origin == "design":
            with (tmp_path / "design.yaml").open("a") as stream:
                stream.write(section)
        elif origin == "project":
            project = tmp_path / "project.yaml"
            project.write_text(section)
            command.extend(["--xedaproject", str(project)])
        else:
            value = "{}" if name == "postsynthsim" else str(given).lower()
            command.extend(["-s", f"flows.vivado_power.{name}={value}"])
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
        document = json.loads(proc.stdout)
        assert proc.returncode != 0 and not document["success"]
        message = document["error"]["message"]
    assert "was removed" in message and replacement in message


def test_failed_activity_prevents_power_reporter_execution(tmp_path, monkeypatch):
    """The verdict stays at the producer; a silent simulator cannot report power."""
    from xeda.flow import FlowDependencyFailure
    from .tool_utils import use_fake_tools

    write_vivado_design(tmp_path)
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_XSIM_STATE", "silent")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowDependencyFailure):
        runner.run("vivado_power", tmp_path / "design.yaml", flow_settings=VIVADO_SETTINGS)
    assert not (tmp_path / "run/sim/vivado_power/vivado_power.tcl").exists()
    result = json.loads((tmp_path / "run/sim/vivado_power/results.json").read_text())
    assert not result["success"]
