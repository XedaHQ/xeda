"""`xeda dse`, end to end: a real design-space exploration on the fake Vivado.

The unit tests around it stop short of running a flow: `Optimizer` stubs that return no batch,
`FlowOutcome` pickled by hand. This runs the command as a user does -- a `pebble` process pool
launching real flow runs, the Fmax search, `best.json` written as the search improves, and the
`--json` document -- and checks what comes out is one consistent, re-runnable record.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from xeda import Design
from xeda.flows import VivadoSynth

TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"


def test_an_exploration_records_one_rerunnable_best_run(tmp_path):
    for name in ("sqrt.vhdl", "sqrt.toml", "tb_sqrt.py"):
        shutil.copy(SQRT / name, tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "dse",
            "vivado_synth",
            "--design",
            "sqrt.toml",
            "--max-workers",
            "2",
            "--init-freq-low",
            "100",
            "--init-freq-high",
            "300",
            "--dse-settings",
            "max_failed_iters=2",
            "--json",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    document = json.loads(proc.stdout)
    assert document["success"] is True
    best = document["best"]

    # The best run is a real run directory, holding the results it was chosen for.
    run_path = Path(best["run_path"])
    recorded_results = json.loads((run_path / "results.json").read_text())
    assert recorded_results["success"] is True
    assert best["results"]["Fmax"] == recorded_results["Fmax"]

    # The search's own record of it is the same view the CLI printed.
    (best_file,) = tmp_path.glob("fmax_sqrt_vivado_synth_*.json")
    recorded = json.loads(best_file.read_text())
    assert recorded["best"] == best

    # ... and both are inputs again: the best settings validate as the flow's settings, and the
    # design recorded beside them is the design that was explored.
    again = VivadoSynth.Settings.from_input(best["settings"], design_root=tmp_path)
    assert (
        again.main_clock
        and again.main_clock.period == best["settings"]["clocks"]["main_clock"]["period"]
    )
    explored = Design.from_file(tmp_path / "sqrt.toml")
    assert Design(**recorded["design"]).rtl_hash == explored.rtl_hash


def _dse(tmp_path, design_toml: str, *args):
    (tmp_path / "sqrt.vhdl").write_text((SQRT / "sqrt.vhdl").read_text())
    (tmp_path / "d.toml").write_text(design_toml)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "dse",
            "vivado_synth",
            "--design",
            "d.toml",
            *args,
            "--json",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, json.loads(proc.stdout)


BARE = 'name = "sqrt"\n[rtl]\nsources = ["sqrt.vhdl"]\ntop = "sqrt"\nclock.port = "clk"\n'


def test_a_search_without_its_bounds_names_them(tmp_path):
    """Without `--init-freq-low/high`, the CLI handed the optimizer `None` for both and the user
    got pydantic's `Input should be a valid number [type=float_type, input_value=None]`."""
    proc, document = _dse(tmp_path, BARE + '[flows.vivado_synth]\nfpga.part = "xc7a12tcsg325-1"\n')
    assert proc.returncode != 0 and document["success"] is False
    error = document["error"]
    assert error["type"] == "FlowSettingsError"
    assert "init_freq_low" in error["message"] and "init_freq_high" in error["message"]
    assert "None" not in error["message"] and "float_type" not in error["message"]


def test_a_search_without_a_device_says_so_before_starting_any_run(tmp_path):
    proc, document = _dse(tmp_path, BARE, "--init-freq-low", "100", "--init-freq-high", "300")
    assert proc.returncode != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowSettingsException"
    assert "`fpga`" in document["error"]["message"]
    assert not list(tmp_path.glob("xeda_run/**/settings.json")), "no run was started"
