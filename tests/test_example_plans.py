"""The commands that the example READMEs and design files teach plan with the example's targets.

`test_example_flow_settings.py` checks that each `flows` section is valid for its flow. This test
plans the commands themselves (`xeda run <request> <design file> [--target T] --dry-run`), so a
request that the design cannot serve, or a target that lacks a setting its flow needs, fails here
without a tool.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow_runner import DefaultRunner
from xeda.flows import Openroad

from .launch_utils import empty_project

EXAMPLES = Path(__file__).parent.parent / "examples" / "bluespec"

#: (design, target, request, the flows the plan runs, in order)
COMMANDS = [
    ("gcd", None, "bsc", ["bsc"]),
    ("gcd", None, "bsc_sim", ["bsc_sim"]),
    (
        "gcd",
        None,
        "openroad",
        ["bsc", "yosys", "openroad_place", "openroad_cts", "openroad_grt", "openroad"],
    ),
    ("gcd", None, "vivado_synth", ["bsc", "vivado_synth"]),
    ("gcd", "ecp5", "yosys_fpga+nextpnr", ["bsc", "yosys_fpga", "nextpnr"]),
    ("gcd", "artix7", "yosys_fpga", ["bsc", "yosys_fpga"]),
    ("ascon", None, "bsc", ["bsc"]),
    ("ascon", None, "bsc_sim", ["bsc_sim"]),
    (
        "ascon",
        None,
        "openroad",
        ["bsc", "yosys", "openroad_place", "openroad_cts", "openroad_grt", "openroad"],
    ),
    ("ascon", None, "vivado_synth", ["bsc", "vivado_synth"]),
    ("ascon", "ecp5", "yosys_fpga+nextpnr", ["bsc", "yosys_fpga", "nextpnr"]),
    ("ascon", "artix7", "yosys_fpga", ["bsc", "yosys_fpga"]),
    ("uart", None, "bsc_sim", ["bsc_sim"]),
    ("uart", None, "bsc", ["bsc"]),
    ("uart", None, "vivado_synth", ["bsc", "vivado_synth"]),
    ("uart", None, "yosys", ["bsc", "yosys"]),
    ("uart", "artix7", "yosys_fpga", ["bsc", "yosys_fpga"]),
    ("uart", "ecp5", "yosys_fpga+nextpnr", ["bsc", "yosys_fpga", "nextpnr"]),
    ("uart_echo", None, "bsc_sim", ["bsc_sim"]),
    ("uart_echo", "switches", "bsc_sim", ["bsc_sim"]),
    ("uart_echo", "echo_transform", "bsc_sim", ["bsc_sim"]),
    ("uart_echo", "arty", "vivado_synth", ["bsc", "vivado_synth"]),
    (
        "uart_echo",
        "arty_nextpnr",
        "yosys_fpga+nextpnr+fpga_pack",
        ["bsc", "yosys_fpga", "nextpnr", "fpga_pack"],
    ),
    (
        "uart_echo",
        "ulx3s",
        "yosys_fpga+nextpnr+fpga_pack",
        ["bsc", "yosys_fpga", "nextpnr", "fpga_pack"],
    ),
    (
        "uart_echo",
        "stlv7325",
        "yosys_fpga+nextpnr+fpga_pack",
        ["bsc", "yosys_fpga", "nextpnr", "fpga_pack"],
    ),
]


@pytest.mark.parametrize(
    "name, target, request_, flows",
    COMMANDS,
    ids=[f"{n}-{t or 'default'}-{r}" for n, t, r, _ in COMMANDS],
)
def test_the_documented_command_plans(name, target, request_, flows, tmp_path):
    args = ["run", request_, str(EXAMPLES / name / f"{name}.yaml"), "--dry-run", "--json"]
    args += ["--run-root", str(tmp_path / "xeda_run")]
    args += ["--xedaproject", empty_project(tmp_path)]
    if target:
        args += ["--target", target]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["target"] == target
    assert [node["name"] for node in document["plan"]["nodes"]] == flows


def test_the_ascon_clock_reaches_synthesis_and_placement(tmp_path):
    """The `yosys` section of the design file sets the clock of the whole OpenROAD chain."""
    design = Design.from_file(EXAMPLES / "ascon" / "ascon.yaml")
    plan = DefaultRunner(tmp_path / "plan", display_results=False).plan(
        Openroad, design, xedaproject=empty_project(tmp_path)
    )
    for name in ("yosys", "openroad_place"):
        clock = next(iter(plan.node(name).settings.clocks.values()))
        assert clock.period.to("ns") == 2
        assert plan.node(name).settings.platform == "nangate45"
