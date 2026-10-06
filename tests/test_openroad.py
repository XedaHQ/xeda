"""The OpenROAD flow's synthesis step: the abc script `optimize` selects.

`optimize` is a setting of the flow that acts on it, `yosys`: it is given as
`flows.yosys.optimize`, and reaches OpenROAD's synthesis through its `yosys` dependency."""

import contextlib
import gzip
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import get_args

import pytest

from xeda import Design
from xeda.dataclass import ValidationError
from xeda.flow import FlowException, FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows import Openroad, Yosys, YosysFpga
from xeda.flows import yosys as yosys_flows
from xeda.tool import ExecutableNotFound

from .tool_utils import launch_until_fresh, require_yosys

NANGATE45_LIB = (
    Path(__file__).parent.parent
    / "src/xeda/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib.gz"
)
OPTIMIZE_CHOICES = [
    c for a in get_args(Yosys.Settings.model_fields["optimize"].annotation) for c in get_args(a)
]


def test_every_optimize_choice_is_covered():
    """Every optimize choice is covered."""
    assert set(OPTIMIZE_CHOICES) == {"speed", "area"}


# One command only each target's abc script runs, to tell the two apart.
DISTINCTIVE_ABC_COMMAND = {"area": "map -B 0.9", "speed": "&if -g -K 6"}


def _mac_design(root: Path) -> Design:
    """Create a multiply-accumulate design for OpenROAD."""
    (root / "mac.v").write_text(
        "module mac(input clk, input rst, input [7:0] a, b, output reg [19:0] q);\n"
        "  always @(posedge clk) if (rst) q <= 0; else q <= q + a * b;\n"
        "endmodule\n"
    )
    return Design(
        name="mac",
        design_root=root,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
    )


def test_every_optimize_target_has_a_distinctive_abc_command():
    """Every optimize target has a distinctive abc command."""
    assert set(DISTINCTIVE_ABC_COMMAND) == set(OPTIMIZE_CHOICES)


@pytest.mark.parametrize("optimize", [None, *OPTIMIZE_CHOICES])
def test_optimize_selects_a_script_and_post_synthesis_optimization_together(
    optimize, tmp_path, monkeypatch
):
    """What OpenROAD's yosys dependency rendered into its script, given `flows.yosys.optimize`:
    the abc script `optimize` selects -- told apart by a command only that target's script runs
    -- together with post-synthesis optimization.

    No tool runs: this is about the settings and the script, so every command is recorded
    instead, and the run stops once yosys, having produced nothing, fails.
    """
    commands = []

    def record(executable, args=None, **kwargs):
        commands.append([str(executable), *map(str, args or [])])
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run").run_flow(
            Openroad,
            _mac_design(tmp_path),
            {"platform": "nangate45", "clock": {"period": 2.0}},
            all_flows_settings={"yosys": {"optimize": optimize}},
        )
    (settings_json,) = tmp_path.glob("xeda_run/mac/yosys*/settings.json")
    yosys_settings = json.loads(settings_json.read_text())["effective_flow_settings"]
    script = (settings_json.parent / "yosys_synth.ys").read_text()
    assert any(Path(cmd[0]).name == "yosys" for cmd in commands), "yosys was never launched"

    assert yosys_settings["optimize"] == optimize
    assert yosys_settings["post_synth_opt"] is (optimize is not None)
    assert ("opt -full -purge -sat" in script) is (optimize is not None)
    (abc,) = [line for line in script.splitlines() if line.startswith("abc ")]
    for target, command in DISTINCTIVE_ABC_COMMAND.items():
        selected = target == optimize
        # `Yosys.Settings` writes a script's commands comma-separated, as `abc -script` wants
        assert (command.replace(" ", ",") in (yosys_settings["abc_script"] or "")) is selected
        assert (command.replace(" ", ",") in abc) is selected
    assert ("-script" in abc) is (optimize is not None)


def test_optimize_is_a_setting_of_the_flow_that_acts_on_it(tmp_path, monkeypatch):
    """`optimize` acts on yosys's mapping, so it is yosys's setting; OpenROAD's was a vehicle
    for configuring its dependency and is gone, saying where it went. No setting is inert:
    yosys's is read where its script is rendered -- each target renders its own abc script."""
    assert "optimize" in Yosys.Settings.model_fields
    for flow in (Openroad, YosysFpga):
        assert "optimize" not in flow.Settings.model_fields
    with pytest.raises(
        FlowSettingsError, match="`optimize` was removed: use `flows.yosys.optimize`"
    ):
        Openroad.Settings.from_input({"optimize": "area"}, design_root=tmp_path)

    def record(executable, args=None, **kwargs):
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    abc_lines = {}
    for optimize in OPTIMIZE_CHOICES:
        run_root = tmp_path / optimize
        with contextlib.suppress(FlowException):
            DefaultRunner(run_root).run_flow(
                Yosys,
                _mac_design(tmp_path),
                {"platform": "nangate45", "clock": {"period": 2.0}, "optimize": optimize},
            )
        script = (run_root / "mac" / "yosys" / "yosys_synth.ys").read_text()
        (abc_lines[optimize],) = [line for line in script.splitlines() if line.startswith("abc ")]
        assert DISTINCTIVE_ABC_COMMAND[optimize].replace(" ", ",") in abc_lines[optimize]
    assert len(set(abc_lines.values())) == len(OPTIMIZE_CHOICES)


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_an_optimize_target_selects_an_abc_script_yosys_accepts(optimize):
    """`abc_opt_script` returned nothing at all, so `optimize` never reached abc; and a script
    must be the text `Yosys.Settings.abc_script` declares, which a list of commands is not."""
    script = yosys_flows.abc_opt_script(optimize)
    assert isinstance(script, str) and script.startswith("+")
    assert Yosys.Settings(abc_script=script).abc_script


def test_area_and_speed_select_different_scripts():
    """Area and speed select different scripts."""
    assert yosys_flows.abc_opt_script("area") != yosys_flows.abc_opt_script("speed")


def test_area_plus_speed_is_no_longer_a_target():
    """It selected exactly the area script, so it was "area" under another name."""
    with pytest.raises(ValidationError):
        Yosys.Settings(optimize="area+speed")


def _contains_run(sequence, run):
    """Check whether a generated script invokes a command."""
    return any(sequence[i : i + len(run)] == run for i in range(len(sequence) - len(run) + 1))


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_the_openroad_flow_synthesizes_with_the_selected_script(optimize, tmp_path):
    """End to end, through OpenROAD's yosys dependency and its own template: abc executes
    exactly the commands the selected script names, in order -- `upsize`/`dnsize` included,
    which yosys's default mapping never runs. Only the synthesis stage is checked: the
    place-and-route stage after it needs a working OpenROAD, and its outcome is not the point."""
    require_yosys()
    (tmp_path / "mac.v").write_text(
        "module mac(input clk, input rst, input [7:0] a, b, output reg [19:0] q);\n"
        "  always @(posedge clk) if (rst) q <= 0; else q <= q + a * b;\n"
        "endmodule\n"
    )
    design = Design(
        name="mac",
        design_root=tmp_path,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
    )
    with contextlib.suppress(ExecutableNotFound):
        DefaultRunner(tmp_path / "xeda_run").run_flow(
            Openroad,
            design,
            {"platform": "nangate45", "clock": {"period": 2.0}},
            all_flows_settings={"yosys": {"optimize": optimize}},
        )

    (yosys_results,) = tmp_path.glob("xeda_run/mac/yosys*/results.json")
    yosys_run = yosys_results.parent
    assert json.loads((yosys_run / "results.json").read_text())["success"] is True
    executed = re.findall(r"^ABC: \+ (.+?)\s*$", (yosys_run / "yosys.log").read_text(), re.M)
    script = Yosys.Settings(abc_script=yosys_flows.abc_opt_script(optimize)).abc_script
    assert script is not None
    selected = [command.replace(",", " ") for command in script[1:].split(";")]
    assert _contains_run(executed, selected), f"abc did not run the {optimize} script"


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_real_yosys_maps_with_the_selected_script(optimize, tmp_path):
    """The script as the yosys template passes it (`abc -script "<abc_script>"`), run for real
    against the Nangate45 library the flow ships."""
    require_yosys()
    lib = tmp_path / "nangate45.lib"
    with gzip.open(NANGATE45_LIB, "rb") as src, open(lib, "wb") as dst:
        shutil.copyfileobj(src, dst)
    (tmp_path / "abc.constr").write_text("set_driving_cell BUF_X1\nset_load 3.898\n")
    (tmp_path / "top.v").write_text(
        "module top(input clk, input [7:0] a, b, output reg [15:0] q);\n"
        "  always @(posedge clk) q <= q + a * b;\n"
        "endmodule\n"
    )
    script = Yosys.Settings(abc_script=yosys_flows.abc_opt_script(optimize)).abc_script
    commands = (
        "read_verilog top.v; synth -top top -flatten; dfflibmap -liberty nangate45.lib; "
        f'abc -D 1000 -script "{script}" -liberty nangate45.lib -constr abc.constr; '
        "opt_clean -purge; write_verilog -noattr netlist.v"
    )
    proc = subprocess.run(
        ["yosys", "-q", "-p", commands], cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    netlist = (tmp_path / "netlist.v").read_text()
    assert "_X1 " in netlist or "_X2 " in netlist, "no Nangate45 cells in the mapped netlist"


@pytest.mark.parametrize("copy_platform_files", [False, True])
def test_a_second_launch_of_openroad_is_fresh(tmp_path, copy_platform_files, monkeypatch):
    """Real yosys synthesis, stand-in OpenROAD/KLayout, and an
    unchanged relaunch starts no tools and rewrites no run output, even byte-identically."""
    from .tool_utils import run_outputs_state, use_fake_asic_tools

    real_init = Openroad.init

    def read_only_init(self):
        before = run_outputs_state(self.run_path)
        real_init(self)
        assert run_outputs_state(self.run_path) == before

    monkeypatch.setattr(Openroad, "init", read_only_init)
    require_yosys()
    binary = use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    (binary / "yosys").unlink()  # synthesis still uses the real installed yosys
    settings = {
        "platform": "nangate45",
        "clock": {"period": 2.0},
        "copy_platform_files": copy_platform_files,
    }
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = _mac_design(tmp_path)
    first = runner.launch_flow(Openroad, design, settings)
    assert first.succeeded and not first.reused
    again = launch_until_fresh(runner, lambda: runner.launch_flow(Openroad, design, settings))
    assert next(flow for flow in runner.launched if flow.name == "yosys").succeeded
    assert (again.run_path / "merged.lib").read_bytes() == (
        again.run_path.parent / "yosys" / "merged_lib.lib"
    ).read_bytes()
    before = run_outputs_state(again.run_path, again.run_path.parent / "yosys")
    last = len(runner.launched)
    fresh = runner.launch_flow(Openroad, design, settings)
    assert fresh.reused and all(flow.reused for flow in runner.launched[last:])
    assert run_outputs_state(again.run_path, again.run_path.parent / "yosys") == before
