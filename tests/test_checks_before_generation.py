"""What a launch refuses on the design's declarations alone, it refuses before a generator runs.

A design lists the sources that its generator writes as declarations (a name or a pattern in the
output of the generator), typed, whether or not they exist. Loading runs nothing. A launch
resolves the request on that declared design first: a flow that reads none of the design's
sources, a source in a language the flow cannot read, a wrong setting, a run directory the flow
refuses are reported before the generator has run, for nothing. Only then does the launch run the
generator.

Each test counts the generator's runs: a refusal leaves the count at zero.
"""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.design import DesignValidationError
from xeda.errors import DesignError
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dse import Dse
from xeda.flow_runner.dse.fmax import FmaxOptimizer

from .scoping import node_settings
from .remote_fixtures import connected, fake_remote, fake_ssh, remote_host, remote_request
from .tool_utils import use_fake_tools

FIXTURES = (fake_ssh, fake_remote, remote_host)


#: Appends a line to the counter its first argument names, then writes each file named after it
#: in its storage (`XEDA_GENERATED`), with contents of the kind its suffix says.
GENERATOR = """\
import os, sys
from pathlib import Path

CONTENTS = {
    ".v": "module top(input clk, output q); assign q = clk; endmodule\\n",
    ".edf": "(edif top)\\n",
    ".bsv": "package Top; endpackage\\n",
    ".vhd": "entity top is end;\\narchitecture a of top is begin end;\\n",
    ".yaml": "{}\\n",
    ".toml": "name = 'dep'\\n",
}
root = Path(os.environ["XEDA_GENERATED"])
with open(sys.argv[1], "a") as counter:
    counter.write("ran\\n")
for name in sys.argv[2:]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONTENTS[path.suffix])
"""

XILINX = {"fpga": {"part": "xc7a12tcsg325-1"}, "clocks": {"main_clock": {"period": "10 ns"}}}


class Generated:
    """A design whose generator writes `writes`, listed in `rtl.sources` as `sources`."""

    def __init__(self, tmp_path: Path, writes: list[str], sources=None, **design) -> None:
        self.root = tmp_path / "design"
        self.root.mkdir()
        self.counter = tmp_path / "generator.log"
        (self.root / "gen.py").write_text(GENERATOR)
        spec = {
            "name": "generated",
            "rtl": {
                "sources": writes if sources is None else sources,
                "top": "top",
                "clocks": ["clk"],
                "generator": {
                    "executable": sys.executable,
                    "args": ["$DESIGN_ROOT/gen.py", str(self.counter), *writes],
                    "sources": ["gen.py"],
                },
            },
            **design,
        }
        self.file = self.root / "design.json"
        self.file.write_text(json.dumps(spec))

    @property
    def runs(self) -> int:
        """How many times the generator has run."""
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0


#: A request refused on the declared design, and what the refusal says.
REFUSALS = {
    "a flow that reads none of the sources": (
        ["gen/top.edf"],
        {},
        "yosys_fpga",
        XILINX,
        "yosys_fpga reads none of the design's sources",
    ),
    "a language the flow cannot read": (
        ["gen/top.v", "gen/top.sc"],
        {},
        "vivado_synth",
        XILINX,
        "vivado_synth cannot read the design's Chisel source",
    ),
    "a language the simulator cannot read": (
        ["gen/top.vhd"],
        {},
        "verilator",
        {},
        "verilator cannot read the design's Vhdl source",
    ),
    "a testbench without its top": (
        ["gen/top.v"],
        {"tb": {"sources": ["tb.v"]}},
        "vivado_sim",
        {},
        "needs to know which module is the testbench",
    ),
    "a setting the flow does not have": (
        ["gen/top.v"],
        {},
        "yosys_fpga",
        {**XILINX, "no_such_setting": 1},
        "no_such_setting",
    ),
}


def _design(tmp_path, writes, extra) -> Generated:
    generated = Generated(tmp_path, writes, **extra)
    (generated.root / "tb.v").write_text("module tb; initial $finish; endmodule\n")
    return generated


@pytest.mark.parametrize("case", REFUSALS, ids=list(REFUSALS))
@pytest.mark.parametrize("launch", ["run", "plan"])
def test_a_launch_refuses_on_the_declared_design_before_the_generator_runs(
    case, launch, tmp_path, monkeypatch
):
    writes, extra, flow, settings, refusal = REFUSALS[case]
    monkeypatch.chdir(tmp_path)
    generated = _design(tmp_path, writes, extra)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsException, match=refusal):
        getattr(runner, launch)(
            flow, str(generated.file), flow_settings=node_settings(flow, settings)
        )
    assert generated.runs == 0
    assert not (generated.root / "gen").exists()
    assert not (tmp_path / "xeda_run").exists(), "nothing was set up"


def test_a_run_directory_a_flow_refuses_is_refused_before_the_generator_runs(tmp_path, monkeypatch):
    """Verilator cannot build in a directory whose path has a space."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"])
    runner = DefaultRunner(tmp_path / "run root", display_results=False)
    with pytest.raises(FlowSettingsException, match="space"):
        runner.run("verilator", str(generated.file))
    assert generated.runs == 0


def test_the_command_line_reports_the_refusal_and_runs_no_generator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"])
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "yosys_fpga",
            str(generated.file),
            "-s",
            "yosys_fpga.fpga.part=xc7a12tcsg325-1",
            "--json",
        ],
    )
    document = json.loads(result.stdout)
    assert result.exit_code == 2 and document["outcome"] is None, result.output
    assert "yosys_fpga reads none of the design's sources" in document["error"]["message"]
    assert generated.runs == 0


def test_a_remote_launch_refuses_before_the_generator_runs(tmp_path, monkeypatch, remote_host):
    """Before the design is shipped, and before a connection is made to a host that has none."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"])
    outcome = remote_request(
        tmp_path,
        generated.file,
        "yosys_fpga",
        flow_settings=node_settings("yosys_fpga", ["fpga.part=xc7a12tcsg325-1"]),
    )
    assert isinstance(outcome.error, FlowSettingsException)
    assert "yosys_fpga reads none of the design's" in str(outcome.error)
    assert generated.runs == 0 and not connected(remote_host)


def test_an_exploration_refuses_before_the_generator_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v", "gen/top.sc"])
    runner = Dse(
        FmaxOptimizer,
        {"init_freq_low": 50.0, "init_freq_high": 200.0},
        run_root=tmp_path / "xeda_run",
        variations={},
        max_workers=1,
    )
    with pytest.raises(FlowSettingsException, match="cannot read the design's Chisel"):
        runner.run(
            "vivado_synth",
            str(generated.file),
            flow_settings=node_settings("vivado_synth", dict(XILINX)),
        )
    assert generated.runs == 0
    assert not (tmp_path / "xeda_run").exists()


def test_a_design_the_flow_can_run_is_generated_once(tmp_path, monkeypatch):
    """The design loads once and the generator runs once. A launch of the unchanged design runs
    no more, and a plan runs none."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(
        "vivado_synth",
        str(generated.file),
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )
    assert flow is not None and flow.succeeded
    assert generated.runs == 1
    flow = runner.run(
        "vivado_synth",
        str(generated.file),
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )
    assert flow is not None and flow.succeeded
    assert generated.runs == 1
    assert runner.plan(
        "vivado_synth",
        str(generated.file),
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )


def test_a_plan_of_a_generator_design_succeeds_and_starts_no_generator(tmp_path, monkeypatch):
    """A plan needs no file that the generator writes, so it is made, and it writes nothing."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    plan = runner.plan(
        "vivado_synth",
        str(generated.file),
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )
    assert [node.name for node in plan.nodes] == ["vivado_synth"]
    assert generated.runs == 0
    assert not (tmp_path / "xeda_run").exists() and not (generated.root / "gen").exists()


def test_a_file_a_setting_reads_to_check_it_must_exist_before_the_generator_runs(
    tmp_path, monkeypatch
):
    """`custom_boards_file` is read when the setting is validated, which comes before the
    generator runs, so the generator cannot write it. With the file in place, the plan is made."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v", "gen/boards.yaml"], sources=["gen/top.v"])
    settings = {"board": "mine", "custom_boards_file": "$DESIGN_ROOT/gen/boards.yaml"}
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsException, match="Cannot read custom boards file"):
        runner.run(
            "nextpnr", str(generated.file), flow_settings=node_settings("nextpnr", dict(settings))
        )
    assert generated.runs == 0
    (generated.root / "gen").mkdir()
    (generated.root / "gen" / "boards.yaml").write_text(
        "mine:\n  fpga: {part: LFE5U-25F-6BG256C}\n"
    )
    assert runner.plan(
        "nextpnr", str(generated.file), flow_settings=node_settings("nextpnr", dict(settings))
    )
    assert generated.runs == 0


# ----------------------------------------------------------- what the declarations say


def test_a_pattern_without_a_definite_type_is_refused_when_the_design_loads(tmp_path, monkeypatch):
    """`gen/*` says nothing about its files before they exist: it needs a `type`, and the error
    shows how to give one. Nothing runs."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"], sources=["gen/*"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignValidationError, match=r"`gen/\*`.*type: Verilog"):
        runner.run(
            "yosys_fpga",
            str(generated.file),
            flow_settings=node_settings("yosys_fpga", dict(XILINX)),
        )
    assert generated.runs == 0


def test_a_pattern_with_a_type_is_judged_on_its_declaration_before_the_generator(
    tmp_path, monkeypatch
):
    """The extension of `gen/*.edf` says what its files are, before any exists, so the launch
    refuses `yosys_fpga` (it reads none of the design's sources) without running the generator."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"], sources=["gen/*.edf"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsException, match="yosys_fpga reads none of the design's"):
        runner.run(
            "yosys_fpga",
            str(generated.file),
            flow_settings=node_settings("yosys_fpga", dict(XILINX)),
        )
    assert generated.runs == 0


def test_a_pattern_is_one_source_in_the_plan_and_its_files_in_the_launch(tmp_path, monkeypatch):
    """`gen/*.edf` is one netlist in the plan, and `vivado_impl` takes one. The launch hands over
    the file that the pattern matched."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"], sources=["gen/*.edf"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(
        "vivado_impl", str(generated.file), flow_settings=node_settings("vivado_impl", dict(XILINX))
    )
    assert flow is not None and flow.succeeded
    assert generated.runs == 1


def test_a_pattern_that_matches_two_files_fails_a_flow_that_takes_one(tmp_path, monkeypatch):
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/a.edf", "gen/b.edf"], sources=["gen/*.edf"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(Exception, match="2 paths"):
        runner.run(
            "vivado_impl",
            str(generated.file),
            flow_settings=node_settings("vivado_impl", dict(XILINX)),
        )
    assert generated.runs == 1


def test_a_testbench_file_the_generator_writes_is_refused_when_the_design_loads(
    tmp_path, monkeypatch
):
    """Only `rtl.sources` can be generated: a `tb.sources` file must exist, and a launch that
    cannot find it says so before it runs the generator."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(
        tmp_path, ["gen/top.v", "gen/tb.v"], sources=["gen/top.v"], tb={"sources": ["gen/tb.v"]}
    )
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignValidationError, match="does not exist"):
        runner.run(
            "vivado_synth",
            str(generated.file),
            flow_settings=node_settings("vivado_synth", dict(XILINX)),
        )
    assert generated.runs == 0


def test_a_project_s_design_generates_once(tmp_path, monkeypatch):
    """A design selected from a project is loaded twice when it generates, and the generator
    runs once."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"])
    spec = json.loads(generated.file.read_text())
    other = {"name": "other", "rtl": {"sources": []}}
    project = tmp_path / "xedaproject.json"
    project.write_text(json.dumps({"designs": [{"file": "design/design.json"}, other]}))
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(
        "vivado_synth",
        spec["name"],
        xedaproject=str(project),
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )
    assert flow is not None and flow.succeeded
    assert generated.runs == 1


# ------------------------------------------- what a malformed design does to the generator


def test_a_malformed_design_is_refused_without_running_its_generator(tmp_path, monkeypatch):
    """The design fails for a reason no generator output can cure (a source that is no path)."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"], sources=[1])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignValidationError):
        runner.run(
            "vivado_synth",
            str(generated.file),
            flow_settings=node_settings("vivado_synth", dict(XILINX)),
        )
    assert generated.runs == 0


def test_a_remote_launch_refuses_a_malformed_design_without_running_its_generator(
    tmp_path, monkeypatch, remote_host
):
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"], sources=[1])
    outcome = remote_request(
        tmp_path,
        generated.file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )
    assert isinstance(outcome.error, DesignValidationError)
    assert generated.runs == 0 and not connected(remote_host)


def test_a_source_given_as_both_file_and_path_is_refused_as_it_is(tmp_path, monkeypatch):
    """`file` and `path` exclude each other: the generator does not run."""
    monkeypatch.chdir(tmp_path)
    both = {"file": "gen/top.v", "path": "gen/top.v"}
    generated = Generated(tmp_path, ["gen/top.v"], sources=[both])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignValidationError, match="mutually exclusive"):
        runner.run(
            "vivado_synth",
            str(generated.file),
            flow_settings=node_settings("vivado_synth", dict(XILINX)),
        )
    assert generated.runs == 0


def test_a_local_dependency_the_generator_writes_is_refused_at_planning(tmp_path, monkeypatch):
    """The design file of a local dependency must exist when planning starts, and the generator
    has not run: the launch is refused, naming the generator, and the generator does not run."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(
        tmp_path,
        ["gen/top.v", "gen/dep.toml"],
        sources=["gen/top.v"],
        dependencies=["gen/dep.toml"],
    )
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignError, match="dep.toml") as refused:
        runner.run(
            "vivado_synth",
            str(generated.file),
            flow_settings=node_settings("vivado_synth", dict(XILINX)),
        )
    assert refused.value.code == "design_file_not_found" and "gen.py" in str(refused.value)
    assert generated.runs == 0


def test_a_remote_launch_refuses_a_local_dependency_the_generator_writes(
    tmp_path, monkeypatch, remote_host
):
    monkeypatch.chdir(tmp_path)
    generated = Generated(
        tmp_path,
        ["gen/top.v", "gen/dep.toml"],
        sources=["gen/top.v"],
        dependencies=["gen/dep.toml"],
    )
    outcome = remote_request(
        tmp_path,
        generated.file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", dict(XILINX)),
    )
    assert isinstance(outcome.error, DesignError)
    assert generated.runs == 0 and not connected(remote_host)


def test_a_malformed_field_beside_a_missing_file_runs_no_generator(tmp_path, monkeypatch):
    """The testbench file is missing and `rtl.top` is no name: the design is refused for both."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(
        tmp_path, ["gen/top.v", "gen/tb.v"], sources=["gen/top.v"], tb={"sources": ["gen/tb.v"]}
    )
    spec = json.loads(generated.file.read_text())
    spec["rtl"]["top"] = 123
    generated.file.write_text(json.dumps(spec))
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignValidationError, match="rtl"):
        runner.run(
            "vivado_synth",
            str(generated.file),
            flow_settings=node_settings("vivado_synth", dict(XILINX)),
        )
    assert generated.runs == 0
