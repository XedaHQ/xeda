"""Flows run `dockerized`: xeda starts the tool in the flow's default container image.

Opt-in: set ``XEDA_TESTS_DOCKER=1`` with a working `docker`. A test whose default image is not
present locally is skipped, naming the `docker pull` to run -- images of commercial tools are
tens of GB, and a test never pulls one. An image built for another platform runs emulated, and
slowly. The tests work under the checkout's ``xeda_run/`` (or ``XEDA_TESTS_WORK_DIR``), which
Docker can mount.
"""

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Optional

import pytest

from xeda._internal.flow import MetricSpec
from xeda import Design
from xeda._internal.design import SourceType
from xeda._internal.dataclass import Field
from xeda._internal.flow import Flow, In, Out
from xeda._internal.flow_runner import DefaultRunner
from xeda._internal.flows import Bsc, BscSim, GhdlSim, Modelsim, VivadoSynth, Yosys
from xeda._internal.flows.ghdl import GhdlTool
from xeda._internal.flows.vivado import VivadoTool
from xeda._internal.flows.yosys.common import YOSYS_DOCKER_IMAGE
import xeda._internal.tool
from xeda._internal.tool import Docker, Tool

from .flow_registry import preserved_flow_registry
from .tool_utils import checkout_work_dir, require_docker, require_docker_image, require_modelsim

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
    return Design(name=name, root=root, rtl=rtl)


def test_vivado_synth_runs_in_its_default_image(work_dir) -> None:
    """Vivado synth runs in its default image."""
    docker = VivadoTool.model_fields["docker"].default
    assert docker is not None
    require_docker_image(_image(docker))
    design = _design(
        work_dir / "design",
        "dk",
        {"sources": ["inv.v"], "top": "inv", "clocks": ["clk"]},
        **{"inv.v": INVERTER_V},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        VivadoSynth,
        design,
        {
            "fpga": {"part": "xc7a12tcsg325-1"},
            "clocks": {"main_clock": {"period": "10 ns"}},
            "docker": True,
        },
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
        root=root,
        rtl={"sources": [], "top": "tb"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(GhdlSim, design, {"docker": True})
    assert flow is not None and flow.succeeded


def test_yosys_runs_in_its_default_image(work_dir) -> None:
    """Yosys runs in its default image."""
    design = _design(
        work_dir / "design",
        "ys",
        {"sources": ["inv.v"], "top": "inv", "clocks": ["clk"]},
        **{"inv.v": INVERTER_V},
    )
    require_docker_image(_image(Docker(image=YOSYS_DOCKER_IMAGE)))
    flow = DefaultRunner(work_dir / "run").run_flow(Yosys, design, {"docker": True})
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
    require_modelsim()
    root = work_dir / "design"
    root.mkdir()
    (root / "inv.sv").write_text(sv)
    (root / "tb.vhd").write_text(tb)
    design = Design(
        name="inv",
        root=root,
        language={"vhdl": {"standard": "2008"}},
        rtl={"sources": ["inv.sv"], "top": "inv"},
        tb={"sources": ["tb.vhd"], "top": "tb"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(Modelsim, design, {"docker": True, **settings})
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
        ("error", None, True),
        ("failure", None, True),
        ("error", "error", True),
        ("failure", "fatal", True),
    ],
)
def test_modelsim_fails_a_testbench_at_its_fail_severity(
    work_dir, severity, fail_severity, fails
) -> None:
    """vsim exits 0 whatever the testbench reports; the flow reads its test status and fails at
    `fail_severity`, by default `error`."""
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
    monkeypatch.setattr(
        xeda._internal.tool, "run_process", lambda cli, cmd, **kw: ran.append([cli, *cmd])
    )
    tool = Tool(executable="yosys", default_args=["-q"], docker=Docker(image="hdlc/impl"))
    tool.dockerized = True
    tool.run("-s", "script.ys", cwd=tmp_path)
    (command,) = ran
    assert command[command.index("hdlc/impl:latest") + 1 :] == ["yosys", "-q", "-s", "script.ys"]


def test_a_containerized_tool_never_relabels_the_users_files(monkeypatch, tmp_path) -> None:
    """Not gated: the `docker run` command is captured, not run. `:z` makes Docker relabel the
    mounted directory for SELinux, which changes the attributes of the user's own files. The
    container runs without a label instead, so every mount is passed as it is."""
    ran = []
    monkeypatch.setattr(
        xeda._internal.tool, "run_process", lambda cli, cmd, **kw: ran.append([cli, *cmd])
    )
    design = tmp_path / "design"
    design.mkdir()
    docker = Docker(image="hdlc/impl", mounts={str(tmp_path / "cache"): "/cache"})
    docker.run("yosys", read_only=[design], cwd=tmp_path)
    (command,) = ran
    volumes = [a for a in command if a.startswith("--volume=")]
    assert len(volumes) == 3
    assert all(":z" not in v and ",z" not in v and not v.endswith(":Z") for v in volumes), volumes
    assert f"--volume={design}:{design}:ro" in volumes
    option = command.index("--security-opt")
    assert command[option + 1] == "label=disable"


@pytest.mark.parametrize(
    "language, body, settings, passes",
    [
        ("sv", "initial $finish(0);", {}, True),
        ("sv", "initial begin #5; $finish; end", {}, True),
        ("sv", 'initial begin #5; $error("check"); $finish; end', {}, False),
        ("sv", 'initial begin #5; $fatal(1, "check"); end', {}, False),
        ("sv", "initial begin #5; $stop; end", {"fail_severity": "fatal"}, False),
        ("sv", 'initial begin #5; $warning("check"); $finish; end', {}, True),
        ("sv", "initial begin #5; end", {}, False),
        ("sv", "reg clk=0; always #1 clk=~clk;", {"stop_time": "10ns"}, True),
        ("sv", "reg clk=0; always #1 clk=~clk;", {"timeout": "20 s"}, False),
        ("vhdl", "std.env.finish;", {}, True),
        ("vhdl", "wait for 5 ns; std.env.stop;", {}, True),
        ("vhdl", "assert false severity warning; std.env.finish;", {}, True),
        ("vhdl", "assert false severity error; std.env.finish;", {}, False),
        ("vhdl", "assert false severity failure; std.env.finish;", {}, False),
    ],
)
def test_modelsim_native_runtime_contract(language, body, settings, passes, work_dir):
    require_modelsim()
    root = work_dir / "native"
    root.mkdir()
    if language == "sv":
        source = root / "tb.sv"
        source.write_text("`timescale 1ns/1ps\nmodule tb; " + body + " endmodule\n")
    else:
        source = root / "tb.vhd"
        source.write_text(
            "entity tb is end; architecture a of tb is begin process begin "
            + body
            + " wait; end process; end;\n"
        )
    design = Design(
        name="native",
        root=root,
        rtl={"sources": [source], "top": "tb"},
        tb={"top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    flow = DefaultRunner(work_dir / "runs").run_flow(
        Modelsim, design, {"docker": True, "timeout": "60 s", **settings}
    )
    assert flow is not None and flow.succeeded is passes
    if passes:
        assert "sim.time_unit" not in flow.results
        # the simulated time in femtoseconds, never in the ticks of the simulator
        expected_time = (
            10_000_000
            if "stop_time" in settings
            else 5_000_000 if "#5" in body or "5 ns" in body else 0
        )
        assert type(flow.results["sim.time"]) is int
        assert flow.results["sim.time"] == expected_time
    if "timeout" in settings:
        assert flow.results["error"]["code"] == "timeout"
        assert "XEDA_MODELSIM_RUNTIME_START" in (flow.run_path / "modelsim_process.log").read_text()
    elif "$stop" in body:
        assert flow.results["sim.ended_by"] == "error"
        assert flow.results["sim.errors"] == 1
    elif passes:
        assert flow.results["sim.ended_by"] == (
            "stop_time" if "stop_time" in settings else "finish"
        )
        assert flow.results["sim.warnings"] == (1 if "warning" in body else 0)
    elif body == "initial begin #5; end":
        assert flow.results["sim.ended_by"] == "unknown"


def test_a_dockerized_consumer_reads_the_files_of_its_producer_and_its_settings(work_dir) -> None:
    """The producer's run directory is a sibling of the consumer's: the container sees its file."""
    require_docker_image("alpine:latest")
    seen = []

    with preserved_flow_registry():

        class Maker(Flow):
            """Writes a file."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Outputs(Flow.Outputs):
                made: Path = Out(SourceType.Data, description="The file it writes.")

            def run(self) -> None:
                (self.run_path / "outputs").mkdir(exist_ok=True)
                self.outputs.made = self.run_path / "outputs" / "made.txt"
                self.outputs.made.write_text("made by the producer\n")

            def parse_reports(self) -> bool:
                return True

        class Reader(Flow):
            """Prints the file of its producer in a container."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                constraints: Optional[Path] = Field(None, description="A file it prints.")

            class Inputs(Flow.Inputs):
                made: Path = In(
                    SourceType.Data,
                    producer="maker",
                    output="made",
                    description="The producer's file.",
                )

            def init(self) -> None:
                self.cat = Tool(executable="cat", docker=Docker(image="alpine"))

            def run(self) -> None:
                seen.append(self.cat.run_get_stdout(str(self.inputs.made)))
                if self.settings.constraints is not None:
                    seen.append(self.cat.run_get_stdout(str(self.settings.constraints)))

            def parse_reports(self) -> bool:
                return True

        design = _design(
            work_dir / "design", "dk", {"sources": ["inv.v"], "top": "inv"}, **{"inv.v": INVERTER_V}
        )
        outside = work_dir / "outside"
        outside.mkdir()
        (outside / "pins.xdc").write_text("a setting names this file")
        flow = DefaultRunner(work_dir / "run").run_flow(
            Reader, design, {"docker": True, "constraints": str(outside / "pins.xdc")}
        )

    assert flow is not None and flow.succeeded
    assert seen == ["made by the producer", "a setting names this file"]


#: The image of the Docker tests of the bsc flows. Build it from tests/docker/bsc.Dockerfile; it is
#: never pulled.
BSC_TEST_IMAGE = "xeda-test-bsc:2026.07.1"

#: A design that reads every kind of file that the bsc flows stage: a listed testbench that
#: includes a listed Bluespec header, a package from a `search_paths` folder that also holds a
#: stray compiled `.bo` of it (bsc must not read that file), and, for `bsc_sim`, a C function
#: (`import "BDPI"`) whose source includes a header beside it.
BSC_STAGED_FILES = {
    "rtl/Tb.bsv": (
        "package Tb;\n"
        "import Helper::*;\n"
        '`include "limit.bsvi"\n'
        'import "BDPI" function Bit#(32) xeda_mac(Bit#(32) a, Bit#(32) b);\n'
        "(* synthesize *)\n"
        "module mkTb(Empty);\n"
        "  rule check;\n"
        '    if (bump(1) == `LIMIT && xeda_mac(2, 7) == 13) $display("PASS");\n'
        '    else $display("FAIL");\n'
        "    $finish(0);\n"
        "  endrule\n"
        "endmodule\n"
        "endpackage\n"
    ),
    "rtl/limit.bsvi": "`define LIMIT 6\n",
    "lib/Helper.bsv": ("package Helper;\nfunction Bit#(8) bump(Bit#(8) x) = x + 5;\nendpackage\n"),
    "lib/Helper.bo": "not a compiled package\n",
    "c/mac.h": "#define MUL 3\n",
    "c/mac.c": (
        '#include "mac.h"\n'
        "unsigned int xeda_mac(unsigned int a, unsigned int b) { return a * MUL + b; }\n"
    ),
}


def _require_bsc_test_image() -> None:
    """Skip unless the bsc test image is present, naming the command that builds it."""
    require_docker()
    found = subprocess.run(
        ["docker", "image", "inspect", BSC_TEST_IMAGE],
        cwd=Path(__file__).parent,
        capture_output=True,
        check=False,
    )
    if found.returncode != 0:
        pytest.skip(
            f"{BSC_TEST_IMAGE} is not present: `docker build -t {BSC_TEST_IMAGE} "
            "-f tests/docker/bsc.Dockerfile tests/docker` to run this test"
        )


def _bsc_staged_design(root: Path) -> Design:
    for rel, text in BSC_STAGED_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return Design(
        name="staged",
        root=root,
        rtl={"sources": [], "top": "mkTb"},
        tb={"sources": ["c/mac.c", "rtl/limit.bsvi", "rtl/Tb.bsv"], "top": "mkTb"},
    )


@pytest.mark.parametrize("simulator", ["bluesim", "iverilog"])
def test_bsc_sim_reads_its_staged_files_in_a_container(simulator, work_dir) -> None:
    """In a container, `bsc_sim` reaches the design through the links of its run directory: the
    listed sources, the listed header, the filtered `search_paths` folder (whose stray `.bo` bsc
    must not read) and the wrapper of a C source whose header lies beside it. The links have
    absolute targets, and the container mounts each folder at its own path."""
    _require_bsc_test_image()
    design = _bsc_staged_design(work_dir / "design")
    flow = DefaultRunner(work_dir / "run").run_flow(
        BscSim,
        design,
        {
            "simulator": simulator,
            "search_paths": ["lib"],
            "docker": True,
            "docker_image": BSC_TEST_IMAGE,
        },
    )
    assert flow is not None and flow.succeeded
    assert "PASS" in (flow.run_path / "sim.log").read_text()
    assert sorted(p.name for p in (work_dir / "design" / "c").iterdir()) == ["mac.c", "mac.h"]


def test_bsc_reads_its_staged_files_in_a_container(work_dir) -> None:
    """In a container, the `bsc` flow compiles a listed source that imports a package from a
    filtered `search_paths` folder and includes a listed header."""
    _require_bsc_test_image()
    root = work_dir / "design"
    for rel in ("rtl/limit.bsvi", "lib/Helper.bsv", "lib/Helper.bo"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(BSC_STAGED_FILES[rel])
    (root / "rtl/Top.bsv").write_text(
        "package Top;\n"
        "import Helper::*;\n"
        '`include "limit.bsvi"\n'
        "(* synthesize *)\n"
        "module mkTop(Reg#(Bit#(8)));\n"
        "  Reg#(Bit#(8)) r <- mkReg(bump(`LIMIT));\n"
        "  return r;\n"
        "endmodule\n"
        "endpackage\n"
    )
    design = Design(
        name="staged_rtl",
        root=root,
        rtl={"sources": ["rtl/limit.bsvi", "rtl/Top.bsv"], "top": "mkTop"},
    )
    flow = DefaultRunner(work_dir / "run").run_flow(
        Bsc,
        design,
        {"search_paths": ["lib"], "docker": True, "docker_image": BSC_TEST_IMAGE},
    )
    assert flow is not None and flow.succeeded
    assert "mkTop" in flow.results["modules"]


DIGEST = "sha256:" + "0123456789abcdef" * 4


@pytest.mark.parametrize(
    "text, image, tag, reference, recorded",
    [
        ("img", "img", None, "img", "img:latest"),
        ("img:tag", "img", "tag", "img:tag", "img:tag"),
        ("org/img:tag", "org/img", "tag", "org/img:tag", "org/img:tag"),
        ("localhost:5000/img", "localhost:5000/img", None, None, "localhost:5000/img:latest"),
        ("localhost:5000/img:tag", "localhost:5000/img", "tag", None, None),
        (f"host:443/org/img@{DIGEST}", f"host:443/org/img@{DIGEST}", None, None, None),
        (f"img:tag@{DIGEST}", f"img:tag@{DIGEST}", None, None, None),
    ],
)
def test_a_docker_image_text_follows_the_reference_grammar(
    monkeypatch, tmp_path, text, image, tag, reference, recorded
) -> None:
    """Not gated: a registry port is not a tag, and the trace records the reference that
    `docker run` gets."""
    ran, noted = [], []
    monkeypatch.setattr(
        xeda._internal.tool, "run_process", lambda cli, cmd, **kw: ran.append([cli, *cmd])
    )
    monkeypatch.setattr(
        xeda._internal.tool, "note_program", lambda name, path=None, client=None: noted.append(name)
    )
    tool = Tool(executable="yosys", docker=text)
    assert (tool.docker.image, tool.docker.tag) == (image, tag)
    reference = reference or text
    assert tool.docker.reference == reference
    tool.dockerized = True
    tool.run("-V", cwd=tmp_path)
    (command,) = ran
    assert reference in command
    assert noted == [f"docker-image:{recorded or reference}"]
