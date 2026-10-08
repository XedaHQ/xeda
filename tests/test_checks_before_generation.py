"""What a launch refuses on the design's declarations alone, it refuses before a generator runs.

A design load may run the design's generator (`rtl.generator`), and a launch used to judge the
request only once the design had loaded: a flow that reads none of the design's sources, a
source in a language the flow cannot read, a wrong setting, a run directory the flow refuses --
each was reported after the generator had run, for nothing. Every launch path now loads the
design first without the generator (`design.deferring_load_side_effects`): the declared design
lists the sources the generator writes as the design declares them, typed, whether or not they
exist yet. The request is resolved and refused on it; only then is the design loaded in full.

Each test counts the generator's runs: a refusal leaves the count at zero.
"""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import DesignValidationError, deferring_load_side_effects
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dse import Dse
from xeda.flow_runner.dse.fmax import FmaxOptimizer
from xeda.flow_runner.remote import RemoteRunner

from .tool_utils import use_fake_tools


@pytest.fixture
def clones(monkeypatch):
    """Where `git clone` was asked to clone: a stand-in that clones nothing."""
    import git.repo

    asked: list[Path] = []

    def clone_from(url, to_path, **kwargs):
        asked.append(Path(to_path))
        raise AssertionError(f"nothing is cloned here: {url}")

    monkeypatch.setattr(git.repo.Repo, "clone_from", staticmethod(clone_from))
    return asked


#: Appends a line to the counter its first argument names, then writes each file named after it
#: under the design root, with contents of the kind its suffix says.
GENERATOR = """\
import os, sys
from pathlib import Path

CONTENTS = {
    ".v": "module top(input clk, output q); assign q = clk; endmodule\\n",
    ".edf": "(edif top)\\n",
    ".bsv": "package Top; endpackage\\n",
    ".vhd": "entity top is end;\\narchitecture a of top is begin end;\\n",
}
root = Path(os.environ["DESIGN_ROOT"])
with open(sys.argv[1], "a") as counter:
    counter.write("ran\\n")
for name in sys.argv[2:]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONTENTS[path.suffix])
"""

XILINX = {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0}


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
                "clock_port": "clk",
                "generator": {
                    "executable": sys.executable,
                    "args": ["gen.py", str(self.counter), *writes],
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
        ["gen/top.v", "gen/top.bsv"],
        {},
        "vivado_synth",
        XILINX,
        "vivado_synth cannot read the design's Bluespec source",
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
        getattr(runner, launch)(flow, str(generated.file), flow_settings=settings)
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
        cli, ["run", "yosys_fpga", str(generated.file), "-s", "fpga=xc7a12tcsg325-1", "--json"]
    )
    document = json.loads(result.stdout)
    assert result.exit_code != 0 and document["success"] is False, result.output
    assert "yosys_fpga reads none of the design's sources" in document["error"]["message"]
    assert generated.runs == 0


def test_a_remote_launch_refuses_before_the_generator_runs(tmp_path, monkeypatch):
    """Before the design is shipped, and before a connection is made to a host that has none."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"])
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    with pytest.raises(FlowSettingsException, match="yosys_fpga reads none of the design's"):
        runner.run_remote(
            str(generated.file),
            "yosys_fpga",
            "host.invalid",
            flow_settings=["fpga=xc7a12tcsg325-1"],
        )
    assert generated.runs == 0


def test_an_exploration_refuses_before_the_generator_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v", "gen/top.bsv"])
    runner = Dse(
        FmaxOptimizer,
        {"init_freq_low": 50.0, "init_freq_high": 200.0},
        run_root=tmp_path / "xeda_run",
        variations={},
        max_workers=1,
    )
    with pytest.raises(FlowSettingsException, match="cannot read the design's Bluespec"):
        runner.run("vivado_synth", str(generated.file), flow_settings=dict(XILINX))
    assert generated.runs == 0
    assert not (tmp_path / "xeda_run").exists()


def test_a_design_the_flow_can_run_is_generated_once(tmp_path, monkeypatch):
    """The declared design is judged, then the design is loaded in full: the generator runs
    once, not once per load. Fresh, it runs no more, and the launch loads the design once."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run("vivado_synth", str(generated.file), flow_settings=dict(XILINX))
    assert flow is not None and flow.succeeded
    assert generated.runs == 1
    flow = runner.run("vivado_synth", str(generated.file), flow_settings=dict(XILINX))
    assert flow is not None and flow.succeeded
    assert generated.runs == 1
    # nothing to generate: a plan is made, as before
    assert runner.plan("vivado_synth", str(generated.file), flow_settings=dict(XILINX))


def test_a_plan_still_starts_no_generator(tmp_path, monkeypatch):
    """A plan of a design the flow can run refuses to generate, as it always did, once the
    declared design passes."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.v"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(DesignValidationError, match="Cannot plan a design that needs a generator"):
        runner.plan("vivado_synth", str(generated.file), flow_settings=dict(XILINX))
    assert generated.runs == 0


# ---------------------------------------------------- what the declared design leaves to the load


def test_a_pattern_without_a_typed_suffix_leaves_the_judgement_to_the_full_load(
    tmp_path, monkeypatch
):
    """`gen/*` says nothing about its files before they exist: the declared design is not
    complete, so nothing is refused on it. The generator runs, and the full load judges."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"], sources=["gen/*"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsException, match="yosys_fpga reads none of the design's"):
        runner.run("yosys_fpga", str(generated.file), flow_settings=dict(XILINX))
    assert generated.runs == 1


def test_a_pattern_with_a_typed_suffix_is_judged_on_its_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    generated = Generated(tmp_path, ["gen/top.edf"], sources=["gen/*.edf"])
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsException, match="yosys_fpga reads none of the design's"):
        runner.run("yosys_fpga", str(generated.file), flow_settings=dict(XILINX))
    assert generated.runs == 0


def test_a_design_that_does_not_load_before_its_generator_runs_is_loaded_in_full(
    tmp_path, monkeypatch
):
    """The generator also writes the testbench, which the design lists as a file that must
    exist: the declared design does not load, so the full load decides, as before."""
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    generated = Generated(
        tmp_path, ["gen/top.v", "gen/tb.v"], sources=["gen/top.v"], tb={"sources": ["gen/tb.v"]}
    )
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run("vivado_synth", str(generated.file), flow_settings=dict(XILINX))
    assert flow is not None and flow.succeeded
    assert generated.runs == 1


def test_a_git_dependency_leaves_the_judgement_to_the_full_load(tmp_path, monkeypatch, clones):
    """A Git dependency is fetched only by the full load, and it may bring sources, a top and a
    testbench: here the netlist-only design is completed by the dependency's Verilog, so it is
    not refused on its declarations."""
    monkeypatch.chdir(tmp_path)
    generated = Generated(
        tmp_path, ["gen/top.edf"], dependencies=["git+https://example.com/u/lib.git#lib.toml"]
    )
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    # a plan starts no generator and fetches nothing: what it refuses is the generation
    with pytest.raises(DesignValidationError, match="Cannot plan a design that needs"):
        runner.plan("yosys_fpga", str(generated.file), flow_settings=dict(XILINX))
    assert generated.runs == 0 and clones == []


def test_the_declared_design_lists_what_the_generator_writes_with_its_type(tmp_path):
    """Nothing runs, nothing is created, and the declared sources keep the types the design
    gives them, explicit or by suffix, whether or not they exist yet."""
    generated = Generated(
        tmp_path,
        ["gen/top.v", "gen/top.edf"],
        sources=["gen/top.v", {"file": "gen/top.edf", "type": "Edif"}, "gen/*.v"],
    )
    with deferring_load_side_effects() as deferred:
        design = Design.from_file(generated.file)
    assert deferred.deferred and deferred.complete
    assert [(src.file.name, src.type.name) for src in design.rtl.sources] == [
        ("top.v", "Verilog"),
        ("top.edf", "Edif"),
        ("*.v", "Verilog"),
    ]
    assert generated.runs == 0 and not (generated.root / "gen").exists()


def test_a_load_with_nothing_to_defer_is_a_full_load(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "top.v").write_text("module top; endmodule\n")
    with deferring_load_side_effects() as deferred:
        design = Design(name="d", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    assert not deferred.deferred and deferred.complete
    assert design == Design(name="d", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
