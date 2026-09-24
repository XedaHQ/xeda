"""Flows run `dockerized`: xeda starts the tool in the flow's default container image.

Opt-in: set ``XEDA_TESTS_DOCKER=1`` with a working `docker`. A test whose default image is not
present locally is skipped, naming the `docker pull` to run -- images of commercial tools are
tens of GB, and a test never pulls one. An image built for another platform runs emulated, and
slowly. The tests work under the checkout's ``xeda_run/`` (or ``XEDA_TESTS_WORK_DIR``), which
Docker can mount.
"""

import shutil
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, Modelsim, VivadoSynth, Yosys
from xeda.flows.ghdl import GhdlTool
from xeda.flows.modelsim import ModelsimTool
from xeda.flows.vivado import VivadoTool
from xeda.flows.yosys.common import YOSYS_DOCKER_IMAGE
import xeda.tool
from xeda.tool import Docker, Tool

from .tool_utils import checkout_work_dir, require_docker, require_docker_image

INVERTER_V = (
    "module inv(input clk, input a, output reg y); always @(posedge clk) y <= ~a; endmodule\n"
)


def _image(docker: Docker) -> str:
    """The image reference `Docker.run` runs: the tag is the image's own, or `tag`."""
    return docker.image if ":" in docker.image else f"{docker.image}:{docker.tag or 'latest'}"


@pytest.fixture
def work_dir():
    """Provide a workspace that Docker can mount."""
    require_docker()
    path = checkout_work_dir("docker_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _design(root: Path, name: str, rtl: dict, **files: str) -> Design:
    """Create a small design for a containerized flow."""
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return Design(name=name, design_root=root, rtl=rtl)


def test_vivado_synth_runs_in_its_default_image(work_dir) -> None:
    """Vivado synth runs in its default image."""
    docker = VivadoTool.model_fields["docker"].default
    assert docker is not None
    require_docker_image(_image(docker))
    design = _design(
        work_dir / "design",
        "dk",
        {"sources": ["inv.v"], "top": "inv", "clock_port": "clk"},
        **{"inv.v": INVERTER_V},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoSynth, design, {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0, "dockerized": True}
    )
    assert flow is not None and flow.succeeded
    assert flow.results["ff"] >= 1


def test_ghdl_sim_runs_in_its_default_image(work_dir) -> None:
    """Ghdl sim runs in its default image."""
    docker = GhdlTool.model_fields["docker"].default
    assert docker is not None
    require_docker_image(_image(docker))
    root = work_dir / "design"
    (root).mkdir(parents=True)
    (root / "tb.vhd").write_text(
        "entity tb is end;\n"
        "architecture sim of tb is begin\n"
        '  process begin report "tb done"; wait; end process;\n'
        "end;\n"
    )
    design = Design(
        name="sim",
        design_root=root,
        rtl={"sources": [], "top": "tb"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(GhdlSim, design, {"dockerized": True})
    assert flow is not None and flow.succeeded


def test_yosys_runs_in_its_default_image(work_dir) -> None:
    """Yosys runs in its default image."""
    design = _design(
        work_dir / "design",
        "ys",
        {"sources": ["inv.v"], "top": "inv", "clock_port": "clk"},
        **{"inv.v": INVERTER_V},
    )
    require_docker_image(_image(Docker(image=YOSYS_DOCKER_IMAGE)))
    flow = DefaultRunner(work_dir / "run").run_flow(Yosys, design, {"dockerized": True})
    assert flow is not None and flow.succeeded


# A VHDL testbench over a SystemVerilog unit whose `input logic` ports need `-svinputport=var`
# under `default_nettype none`. Its one check expects `y` to be `expect`, with `severity`.
MODELSIM_SV = """`default_nettype none
module inv(input logic a, output logic y); assign y = ~a; endmodule
"""
MODELSIM_TB = """library ieee; use ieee.std_logic_1164.all;
use std.textio.all;
entity tb is end;
architecture a of tb is
  signal a, y : std_logic := '0';
  component inv port (a : in std_logic; y : out std_logic); end component;
begin
  u : inv port map (a, y);
  process
    file marker : text;
    variable marker_line : line;
  begin
    wait for 1 ns;
    assert y = '{expect}' report "y /= {expect}" severity {severity};
    file_open(marker, "post_assertion.marker", write_mode);
    write(marker_line, string'("reached"));
    writeline(marker, marker_line);
    file_close(marker);
    std.env.finish;
  end process;
end;
"""


def _modelsim_run(
    work_dir: Path, tb: str, sv: str = MODELSIM_SV, check_post_assertion: bool = False, **settings
) -> bool:
    """Whether the flow succeeds on a mixed-language design in the default ModelSim image."""
    require_docker_image(_image(ModelsimTool.model_fields["docker"].default))
    root = work_dir / "design"
    root.mkdir()
    (root / "inv.sv").write_text(sv)
    (root / "tb.vhd").write_text(tb)
    design = Design(
        name="inv",
        design_root=root,
        language={"vhdl": {"standard": "2008"}},
        rtl={"sources": ["inv.sv"], "top": "inv"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        Modelsim, design, {"dockerized": True, **settings}
    )
    assert flow is not None
    if check_post_assertion:
        assert (flow.run_path / "post_assertion.marker").read_text().strip() == "reached"
    return flow.succeeded


def test_modelsim_runs_in_its_default_image(work_dir) -> None:
    """A passing testbench passes; `std.env.finish` returns to the script, which exits 0."""
    assert _modelsim_run(work_dir, MODELSIM_TB.format(expect="1", severity="failure"))


@pytest.mark.parametrize(
    "severity, fail_severity, fails",
    [
        ("error", None, False),
        ("failure", None, True),
        ("error", "error", True),
        ("failure", "fatal", True),
    ],
)
def test_modelsim_fails_a_testbench_at_its_fail_severity(
    work_dir, severity, fail_severity, fails
) -> None:
    """vsim exits 0 whatever the testbench reports; the flow reads its test status and fails at
    `fail_severity`, by default `failure`."""
    tb = MODELSIM_TB.format(expect="0", severity=severity)
    settings = {"fail_severity": fail_severity} if fail_severity else {}
    assert _modelsim_run(work_dir, tb, check_post_assertion=True, **settings) is not fails


def test_modelsim_fatal_status_fails_at_fatal_threshold(work_dir) -> None:
    """ModelSim reports SystemVerilog `$fatal` as TESTSTATUS 3, also used by VHDL failure."""
    tb = MODELSIM_TB.format(expect="1", severity="failure")
    sv = MODELSIM_SV.replace("endmodule", 'initial $fatal(1, "fatal check"); endmodule')
    assert not _modelsim_run(work_dir, tb, sv=sv, fail_severity="fatal")


def test_modelsim_fails_a_compile_error(work_dir) -> None:
    """The script's `exit 1` exited vsim with status 0: a source that did not compile passed."""
    tb = MODELSIM_TB.format(expect="1", severity="failure")
    assert not _modelsim_run(work_dir, tb, sv="module inv(;\n")


def test_a_containerized_tool_gets_its_default_arguments_once(monkeypatch, tmp_path) -> None:
    """Not gated: the `docker run` command is captured, not run. A container's command held the
    tool's `default_args`, and `execute` passed them as well: `yosys -T -Q -T -Q`."""
    ran = []
    monkeypatch.setattr(xeda.tool, "run_process", lambda cli, cmd, **kw: ran.append([cli, *cmd]))
    monkeypatch.chdir(tmp_path)
    tool = Tool(executable="yosys", default_args=["-q"], docker=Docker(image="hdlc/impl"))
    tool.dockerized = True
    tool.run("-s", "script.ys")
    (command,) = ran
    assert command[command.index("hdlc/impl:latest") + 1 :] == ["yosys", "-q", "-s", "script.ys"]
