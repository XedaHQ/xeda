"""A run directory a flow cannot work in is refused, before anything is created for it: not the
run root, not the directory, not its lock. `Flow.check_run_directory` is the hook. The launcher
calls it for planning, for every launch and, once, for a design-space exploration, never for the
`--remote` runner (its build is the remote's).

Two flows have such a limit, for the same paths:

* `verilator`: Verilator's GNU Make build cannot work in a directory whose path has whitespace.
  Its makefile stops with "GNU Make cannot build in directories containing spaces" before it
  compiles anything. The model is built in `sim_dir`, a name inside the run directory.
* `bsc_sim`, with every simulator: bsc's link step runs its tools through a shell, unquoted.
"""

import re
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dse.dse_runner import Dse, Optimizer
from xeda.flows import BscSim, Verilator
from xeda.run_root import ensure_run_root

from .settings_samples import flow_classes, minimal_settings
from .tool_utils import require_verilator

#: a run root whose path has whitespace make splits at
SPACED_ROOTS = ["r r", "r\tr"]
#: every simulator bsc_sim accepts
BSC_SIMULATORS = ["bluesim", "verilator", "iverilog", "modelsim", "questa", "vcs", "vcsi", "xsim"]
#: what each flow says: Verilator's makefile, and bsc's shell
VERILATOR_MESSAGE = "GNU Make"
BSC_MESSAGE = "without quoting"


def _verilog_design(root: Path) -> Design:
    (root / "top.sv").write_text("`timescale 1ns/1ps\nmodule top; initial $finish; endmodule\n")
    return Design(name="top", design_root=root, rtl={"sources": ["top.sv"], "top": "top"})


def _bluespec_design(root: Path) -> Design:
    for name in ("Top.bsv", "Tb.bsv"):
        (root / name).write_text("package P; endpackage\n")
    return Design(
        name="top",
        design_root=root,
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
        tb={"sources": ["Tb.bsv"], "top": "mkTb"},
    )


#: (flow, its settings, a design for it, what its refusal says), one per flow and simulator
CASES = [
    pytest.param(Verilator, {}, _verilog_design, VERILATOR_MESSAGE, id="verilator"),
    *[
        pytest.param(
            BscSim,
            {"simulator": simulator},
            _bluespec_design,
            BSC_MESSAGE,
            id=f"bsc_sim_{simulator}",
        )
        for simulator in BSC_SIMULATORS
    ],
]


def _names(run_root: Path) -> str:
    """What a refusal names: the run directory, under `run_root`."""
    return re.escape(str(run_root.resolve()))


@pytest.mark.parametrize("spaced", SPACED_ROOTS, ids=["space", "tab"])
@pytest.mark.parametrize(("flow_class", "settings", "design", "message"), CASES)
def test_a_launch_in_a_run_root_with_whitespace_is_refused_before_anything_is_created(
    flow_class, settings, design, message, spaced, tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "")  # nothing may run
    run_root = tmp_path / spaced
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(run_root, display_results=False).run_flow(
            flow_class, design(tmp_path), settings
        )
    assert re.search(_names(run_root), str(raised.value)), raised.value
    assert message in str(raised.value)
    assert "--run-root" in str(raised.value)
    assert not run_root.exists(), "the run root, the run directory and its lock are not created"
    assert not list(tmp_path.glob("*.lock"))


@pytest.mark.parametrize(("flow_class", "settings", "design", "message"), CASES)
def test_planning_refuses_a_run_root_with_whitespace_too(
    flow_class, settings, design, message, tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "")
    run_root = tmp_path / "r r"
    with pytest.raises(FlowSettingsException, match=message):
        DefaultRunner(run_root, display_results=False).plan(
            flow_class, design(tmp_path), flow_settings=settings
        )
    assert not run_root.exists()


def test_a_design_space_exploration_is_refused_once_before_the_search_starts(tmp_path, monkeypatch):
    """Not once in each of its runs: they would fail apart and be reported only as "no
    successful run"."""

    class NoopOptimizer(Optimizer):
        def next_batch(self):
            return None

    monkeypatch.chdir(tmp_path)
    run_root = tmp_path / "r r"
    dse = Dse(NoopOptimizer, {}, run_root, variations={}, max_workers=1)
    with pytest.raises(FlowSettingsException, match=VERILATOR_MESSAGE):
        dse.run_flow(Verilator, _verilog_design(tmp_path))
    assert not run_root.exists(), "no run root was created"
    assert not list(tmp_path.glob("fmax_*")), "no search was started"


def test_a_symbolic_link_in_the_run_root_is_judged_by_what_it_leads_to(tmp_path):
    """Make builds in the physical directory: a link without whitespace to one with it is refused,
    and a link with whitespace to one without is not."""
    spaced, plain = tmp_path / "a b", tmp_path / "plain"
    spaced.mkdir()
    plain.mkdir()
    (tmp_path / "to_spaced").symlink_to(spaced)
    (tmp_path / "to plain").symlink_to(plain)
    with pytest.raises(FlowSettingsException, match=VERILATOR_MESSAGE):
        Verilator.check_run_directory(Verilator.Settings(), tmp_path / "to_spaced" / "run")
    Verilator.check_run_directory(Verilator.Settings(), tmp_path / "to plain" / "run")


def _run_directory_with_a_stale_link_at_sim_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A run root, and in it the run directory of `verilator` for the design `top`, with a link a
    tool left at `sim_build`'s own name, leading out of it to a directory whose path has whitespace.
    The launcher removes such a link before the run (it never follows it)."""
    run_root = ensure_run_root(tmp_path / "runs")
    assert run_root is not None
    elsewhere = tmp_path / "elsewhere with space"
    elsewhere.mkdir()
    run_dir = run_root / "top" / "verilator"
    run_dir.mkdir(parents=True)
    (run_dir / "sim_build").symlink_to(elsewhere)
    return run_root, run_dir


def test_a_stale_link_at_sim_dir_is_judged_by_its_name_when_planning(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    run_root, run_dir = _run_directory_with_a_stale_link_at_sim_dir(tmp_path)
    Verilator.check_run_directory(Verilator.Settings(), run_dir)
    DefaultRunner(run_root, display_results=False).plan(Verilator, _verilog_design(tmp_path))


def test_a_stale_link_at_sim_dir_does_not_stop_a_launch(tmp_path):
    require_verilator()
    run_root, run_dir = _run_directory_with_a_stale_link_at_sim_dir(tmp_path)
    flow = DefaultRunner(run_root, display_results=False).run_flow(
        Verilator, _verilog_design(tmp_path), {}
    )
    assert flow is not None and flow.succeeded
    assert not (run_dir / "sim_build").is_symlink(), "the link was removed, not followed"


def test_a_build_directory_with_whitespace_is_refused_too(tmp_path):
    """`verilator` builds in `sim_dir`, a name inside the run directory: make stops in it just the
    same."""
    with pytest.raises(FlowSettingsException, match="my build") as raised:
        Verilator.check_run_directory(Verilator.Settings(sim_dir="my build"), tmp_path / "run")
    assert "sim_dir" in str(raised.value)
    Verilator.check_run_directory(Verilator.Settings(sim_dir="sim_build"), tmp_path / "run")


def test_bsc_sim_judges_a_symbolic_link_by_what_it_leads_to_too(tmp_path):
    spaced = tmp_path / "a b"
    spaced.mkdir()
    (tmp_path / "to_spaced").symlink_to(spaced)
    with pytest.raises(FlowSettingsException, match=BSC_MESSAGE) as raised:
        BscSim.check_run_directory(BscSim.Settings(), tmp_path / "to_spaced" / "run")
    assert str(spaced.resolve()) in str(raised.value)


@pytest.mark.parametrize("simulator", BSC_SIMULATORS)
def test_bsc_sim_refuses_a_run_directory_with_whitespace_for_every_simulator(simulator, tmp_path):
    """bsc's link step runs its tools through a shell without quoting, whatever the simulator:
    one rule, one message."""
    with pytest.raises(FlowSettingsException, match=BSC_MESSAGE) as raised:
        BscSim.check_run_directory(BscSim.Settings(simulator=simulator), tmp_path / "r r" / "run")
    assert "r r" in str(raised.value)
    BscSim.check_run_directory(BscSim.Settings(simulator=simulator), tmp_path / "run")


def test_only_the_flows_with_such_a_limit_refuse(tmp_path):
    """Pinned, so that a flow added to the list is a decision."""
    spaced = tmp_path / "r r" / "run"
    refusing = set()
    for flow_class, name in flow_classes():
        if not flow_class.__module__.startswith("xeda.flows"):
            continue
        try:
            flow_class.check_run_directory(
                flow_class.Settings(**minimal_settings(flow_class)), spaced
            )
        except FlowSettingsException:
            refusing.add(name)
    assert refusing == {"verilator", "bsc_sim"}


@pytest.mark.parametrize("name", ["r#r", "r$r"])
def test_a_run_root_with_a_character_make_reads_is_built_in(name, tmp_path):
    """`#` starts a comment and `$` a variable in a makefile. They broke every build while the
    compiler flags named the run directory's path; the header is included by its name now."""
    require_verilator()
    flow = DefaultRunner(tmp_path / name, display_results=False).run_flow(
        Verilator, _verilog_design(tmp_path), {}
    )
    assert flow is not None and flow.succeeded
    assert flow.results["sim.ended_by"] == "finish"
