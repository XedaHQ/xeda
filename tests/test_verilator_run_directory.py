"""Verilator's GNU Make build cannot work in a directory whose path has whitespace: its makefile
stops with "GNU Make cannot build in directories containing spaces" before it compiles anything.
Every flow that builds a model with that makefile (`verilator`, and `bsc_sim` with its Verilator
backend) refuses such a run directory, before anything is created for it: not the run root, not
the directory, not its lock. The check is one shared function behind `Flow.check_run_directory`,
which the launcher calls for planning and for every launch.
"""

import re
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import BscSim, Verilator

from .settings_samples import flow_classes, minimal_settings

#: a run root whose path has whitespace make splits at
SPACED_ROOTS = ["r r", "r\tr"]


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


def _refusal(run_root: Path) -> str:
    """What a refusal says: the run directory it names is under `run_root`."""
    return re.escape(str(run_root.resolve()))


@pytest.mark.parametrize("spaced", SPACED_ROOTS, ids=["space", "tab"])
@pytest.mark.parametrize(
    ("flow_class", "settings", "design"),
    [
        (Verilator, {}, _verilog_design),
        (BscSim, {"simulator": "verilator"}, _bluespec_design),
    ],
    ids=["verilator", "bsc_sim_verilator"],
)
def test_a_launch_in_a_run_root_with_whitespace_is_refused_before_anything_is_created(
    flow_class, settings, design, spaced, tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "")  # nothing may run
    run_root = tmp_path / spaced
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(run_root, display_results=False).run_flow(
            flow_class, design(tmp_path), settings
        )
    message = str(raised.value)
    assert re.search(_refusal(run_root), message), message
    assert "GNU Make" in message and "whitespace" in message
    assert "--run-root" in message
    assert not run_root.exists(), "the run root, the run directory and its lock are not created"
    assert not list(tmp_path.glob("*.lock"))


@pytest.mark.parametrize(
    ("flow_class", "settings", "design"),
    [
        (Verilator, {}, _verilog_design),
        (BscSim, {"simulator": "verilator"}, _bluespec_design),
    ],
    ids=["verilator", "bsc_sim_verilator"],
)
def test_planning_refuses_a_run_root_with_whitespace_too(
    flow_class, settings, design, tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "")
    run_root = tmp_path / "r r"
    with pytest.raises(FlowSettingsException, match="GNU Make"):
        DefaultRunner(run_root, display_results=False).plan(
            flow_class, design(tmp_path), flow_settings=settings
        )
    assert not run_root.exists()


def test_a_symbolic_link_is_judged_by_what_it_leads_to(tmp_path):
    """Make builds in the physical directory: a link without whitespace to one with it is refused,
    and a link with whitespace to one without is not."""
    spaced, plain = tmp_path / "a b", tmp_path / "plain"
    spaced.mkdir()
    plain.mkdir()
    (tmp_path / "to_spaced").symlink_to(spaced)
    (tmp_path / "to plain").symlink_to(plain)
    with pytest.raises(FlowSettingsException, match="GNU Make"):
        Verilator.check_run_directory(Verilator.Settings(), tmp_path / "to_spaced" / "run")
    Verilator.check_run_directory(Verilator.Settings(), tmp_path / "to plain" / "run")


def test_a_build_directory_with_whitespace_is_refused_too(tmp_path):
    """`verilator` builds in `sim_dir`, a name inside the run directory: make stops in it just the
    same."""
    with pytest.raises(FlowSettingsException, match="my build") as raised:
        Verilator.check_run_directory(Verilator.Settings(sim_dir="my build"), tmp_path / "run")
    assert "sim_dir" in str(raised.value)
    Verilator.check_run_directory(Verilator.Settings(sim_dir="sim_build"), tmp_path / "run")


def test_bsc_sim_refuses_it_only_for_the_verilator_backend(tmp_path):
    spaced = tmp_path / "r r" / "run"
    with pytest.raises(FlowSettingsException, match="GNU Make"):
        BscSim.check_run_directory(BscSim.Settings(simulator="verilator"), spaced)
    for simulator in ("bluesim", "iverilog"):
        BscSim.check_run_directory(BscSim.Settings(simulator=simulator), spaced)


def test_only_the_flows_that_build_with_verilators_makefile_refuse(tmp_path):
    """Pinned, so that a flow added to the list is a decision: `verilator`, and `bsc_sim` for its
    Verilator backend (its other backends build elsewhere)."""
    spaced = tmp_path / "r r" / "run"
    refusing = set()
    for flow_class, name in flow_classes():
        if not name.startswith("xeda.") and not flow_class.__module__.startswith("xeda.flows"):
            continue
        settings = flow_class.Settings(**minimal_settings(flow_class))
        variants = [settings]
        if flow_class is BscSim:
            variants.append(BscSim.Settings(simulator="verilator"))
        for variant in variants:
            try:
                flow_class.check_run_directory(variant, spaced)
            except FlowSettingsException:
                refusing.add(name)
    assert refusing == {"verilator", "bsc_sim"}
