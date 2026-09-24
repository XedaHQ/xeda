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
from xeda.flows import GhdlSim, VivadoSynth, Yosys
from xeda.flows.ghdl import GhdlTool
from xeda.flows.vivado import VivadoTool
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
    probe = Yosys(Yosys.Settings(), design, work_dir / "probe")
    assert probe.yosys.docker is not None
    require_docker_image(_image(probe.yosys.docker))
    flow = DefaultRunner(work_dir / "run").run_flow(Yosys, design, {"dockerized": True})
    assert flow is not None and flow.succeeded


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
