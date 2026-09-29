"""A simulator without a cocotb integration rejects a cocotb testbench, rather than running the
design as an ordinary simulation whose cocotb tests silently never run.

The check is `Flow.check_design_supported`, made where a launch checks `required_settings`:
before anything is set up for the run (here), and before anything is shipped to a remote
(`test_remote_run.py`). `SimFlow.__init__` makes it too, for a flow constructed directly.
"""

import re
import shutil
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.flow import FlowException, SimFlow
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flow_runner.dse.dse_runner import Dse, Optimizer
from xeda.flows import Modelsim, Vcs

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"

SIM_FLOW_NAMES = sorted(
    {cls.name for _, cls in registered_flows.values() if issubclass(cls, SimFlow)}
)


@pytest.fixture
def cocotb_design(tmp_path):
    """`examples/vhdl/sqrt`, whose testbench is cocotb's (`tb.cocotb = true`)."""
    root = tmp_path / "design"
    shutil.copytree(SQRT, root, ignore=shutil.ignore_patterns("__pycache__", "xeda_run"))
    design = Design.from_file(root / "sqrt.toml")
    assert design.tb.cocotb
    return design


def _minimal_required_settings(settings_model):
    settings = {}
    for name, field in settings_model.model_fields.items():
        if field.is_required() and hasattr(field.annotation, "model_fields"):
            settings[name] = _minimal_required_settings(field.annotation)
    return settings


def _suggested_flows(message: str, flow_name: str) -> set[str]:
    prefix = f"{flow_name} cannot run cocotb tests; use one of: "
    assert message.startswith(prefix), message
    return set(message[len(prefix) :].split(", "))


def test_the_sweep_covers_simulators_with_and_without_cocotb():
    """The sweep covers simulators with and without cocotb."""
    assert {"ghdl_sim", "nvc", "verilator", "modelsim", "vcs"} <= set(SIM_FLOW_NAMES)


@pytest.mark.parametrize("flow_class", [Modelsim, Vcs], ids=lambda cls: cls.name)
def test_a_simulator_without_cocotb_names_the_ones_with_it(flow_class, cocotb_design):
    """The rejection names the simulators that can run the testbench, and only those."""
    with pytest.raises(FlowException) as raised:
        flow_class.check_design_supported(cocotb_design)

    suggested = _suggested_flows(str(raised.value), flow_class.name)
    assert {"ghdl_sim", "nvc", "verilator"} <= suggested
    assert not {"modelsim", "vcs"} & suggested


@pytest.mark.parametrize("flow_name", SIM_FLOW_NAMES, ids=str)
def test_every_simulator_runs_or_rejects_a_cocotb_testbench(flow_name, cocotb_design):
    """Every simulator runs or rejects a cocotb testbench."""
    flow_class = get_flow_class(flow_name)
    if flow_class.cocotb_sim_name:
        flow_class.check_design_supported(cocotb_design)
        return
    rejected = re.escape(f"{flow_name} cannot run cocotb tests; use one of: ")
    with pytest.raises(FlowException, match=rejected):
        flow_class.check_design_supported(cocotb_design)
    # and constructed directly, not launched
    with pytest.raises(FlowException, match=rejected):
        flow_class(
            settings=_minimal_required_settings(flow_class.Settings),
            design=cocotb_design,
        )


@pytest.mark.parametrize("flow_name", SIM_FLOW_NAMES, ids=str)
def test_every_simulator_runs_a_design_without_a_cocotb_testbench(flow_name, cocotb_design):
    """Every simulator runs a design without a cocotb testbench."""
    design = cocotb_design.model_copy(deep=True)
    design.tb.cocotb = False

    get_flow_class(flow_name).check_design_supported(design)


@pytest.mark.parametrize("flow_class", [Modelsim, Vcs], ids=lambda cls: cls.name)
def test_a_rejected_launch_sets_up_no_run_directory(flow_class, cocotb_design, tmp_path):
    """Rejected at launch, before a run directory is created for it."""
    run_dir = tmp_path / "xeda_run"

    with pytest.raises(FlowException, match=re.escape(f"{flow_class.name} cannot run cocotb")):
        DefaultRunner(run_dir, display_results=False).run_flow(flow_class, cocotb_design)

    assert not list(run_dir.iterdir())


def test_a_rejected_launch_keeps_the_previous_run_directory(cocotb_design, tmp_path):
    """A previous run's directory, which a `clean` launch empties, survives a launch that is
    rejected."""
    run_dir = tmp_path / "xeda_run"
    previous = run_dir / "sqrt" / "modelsim" / "results.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{"success": true}')

    with pytest.raises(FlowException, match=re.escape("modelsim cannot run cocotb")):
        DefaultRunner(run_dir, clean=True, display_results=False).run_flow(Modelsim, cocotb_design)

    assert sorted(run_dir.rglob("*")) == [previous.parent.parent, previous.parent, previous]
    assert previous.read_text() == '{"success": true}'


def test_a_design_space_exploration_rejects_it_before_starting_any_run(
    cocotb_design, tmp_path, monkeypatch
):
    """Once, before the search starts, rather than in each of its runs."""

    class NoopOptimizer(Optimizer):
        def next_batch(self):
            return None

    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "xeda_run_dse"
    dse = Dse(NoopOptimizer, {}, run_dir, variations={}, max_workers=1)

    with pytest.raises(FlowException, match=re.escape("modelsim cannot run cocotb tests")):
        dse.run_flow(Modelsim, cocotb_design)

    assert not list(run_dir.iterdir())
    assert not list(tmp_path.glob("fmax_*")), "no search was started"
