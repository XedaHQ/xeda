import json
import os
import tempfile
from pathlib import Path

from xeda import Design
from xeda.flow import FPGA
from xeda.flow_runner import DefaultRunner
from xeda.flows import IseSynth
from xeda.flows.ise import format_value

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

os.environ["PATH"] += os.pathsep + os.path.join(TESTS_DIR, "fake_tools")


def test_ise_synth_py() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    # Append to PATH so if the actual tool exists, would take precedences.
    assert path.exists()
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(IseSynth, design, settings)
        assert flow is not None, "run_flow returned None"
        assert flow.run_path is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        # assert flow.succeeded

        recorded = json.loads(settings_json.read_text())["flow_settings"]
        # Project properties are recorded exactly as written. ISE's own quoting is applied by
        # the template, because quoting is not idempotent: a validator that quoted on the way in
        # turned "High" into ""High"" on every re-validation, including reloading this file.
        assert recorded["synthesis_options"]["Optimization Effort"] == "High"

        script = (flow.run_path / "ise_synth.tcl").read_text()
        assert 'project set "Optimization Effort" "High" -process "Synthesize - XST"' in script
        assert (
            'project set "Optimize Instantiated Primitives" TRUE -process "Synthesize - XST"'
            in script
        )


def test_ise_project_options_are_quoted_exactly_once() -> None:
    """Every option group renders through `format_value`, `translate_options` included.

    `translate_options` was left out of the validator that quoted the other four, so a string
    given there reached `project set` bare while the same string elsewhere was quoted.
    """
    settings = IseSynth.Settings(  # type: ignore[call-arg]
        fpga=FPGA("xc7a12tcsg325-1"),
        clock_period=5.5,
        translate_options={"Allow Unmatched LOC Constraints": "true"},
    )
    assert settings.translate_options["Allow Unmatched LOC Constraints"] == "true"
    assert format_value(settings.translate_options["Allow Unmatched LOC Constraints"]) == '"true"'

    reloaded = IseSynth.Settings.model_validate(settings.model_dump())
    assert reloaded.model_dump() == settings.model_dump()


if __name__ == "__main__":
    test_ise_synth_py()
