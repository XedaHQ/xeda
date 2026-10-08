"""A run reads the strategy and step tables and writes none of them.

The non-project Vivado flows take their step options from a strategy table that lives in the
module (`vivado_alt_synth.strategies`), and `vivado_synth` from the steps of its settings. What a
run derives from its settings -- `-mode out_of_context`, `flatten_hierarchy`, the hooks of the
project-mode steps -- it computes where it renders the script. It does not write that into the
step tables: a table that a run wrote would hand its options to the next run of the same process
(DSE, the API, a test), and the settings the run records would claim options the user never gave.
"""

import copy
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoImpl, VivadoSynth
from xeda.flows.vivado import vivado_alt_synth as alt
from xeda.flows.vivado.vivado_synth import RunOptions

from .test_vivado_impl import SETTINGS as IMPL_SETTINGS
from .test_vivado_impl import _edif_design
from .tool_utils import use_fake_tools

RESOURCES = Path(__file__).parent / "resources"
PART = "xc7a12tcsg325-1"

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="the fake Vivado runs the TCL it is handed under tclsh"
)


class _Unvalidated(RunOptions):
    """Run options that take an assignment as it is, as a model that copies nothing would."""

    model_config = ConfigDict(validate_assignment=False)


def _grow(value: Any) -> None:
    """Change a step the way a careless writer would: a key in a mapping, an item in a list."""
    if isinstance(value, dict):
        value["written_by_a_test"] = "x"
    elif isinstance(value, list):
        value.append("-written_by_a_test")


@pytest.mark.parametrize("run", sorted(alt.RUN_STEPS))
def test_expanding_a_strategy_hands_out_copies_of_its_steps(run, monkeypatch) -> None:
    """The steps of a strategy are the module's. The expansion copies them whatever the model
    does on an assignment (pydantic happens to copy a mapping it validates: a model that did not
    would have shared them), so what a caller does to its steps never reaches the table. The
    test works on a copy of the table: a table it wrote to would hand that to every later test."""
    monkeypatch.setattr(alt, "strategies", copy.deepcopy(alt.strategies))
    pristine = copy.deepcopy(alt.strategies)
    for name in alt.strategies[run]:
        expanded = alt.expand_run_options(run, _Unvalidated(strategy=name))
        assert expanded.steps.keys() >= set(alt.RUN_STEPS[run])
        for step in expanded.steps.values():
            _grow(step)
    assert alt.strategies == pristine


def _design() -> Design:
    return Design.from_file(RESOURCES / "design0" / "design0.toml")


def _run(flow_class, root: Path, design: Design, settings: dict, monkeypatch):
    use_fake_tools(monkeypatch)
    flow = DefaultRunner(root).run_flow(flow_class, design, settings)
    assert flow is not None and flow.succeeded
    return flow


def _script(flow, name: str) -> str:
    return (flow.run_path / name).read_text()


def _synth_design(script: str) -> str:
    (line,) = [line for line in script.splitlines() if line.startswith("synth_design ")]
    return line


@needs_tclsh
def test_vivado_alt_synth_derives_its_synthesis_options_where_it_renders_the_script(
    tmp_path, monkeypatch
) -> None:
    """`out_of_context` and `flatten_hierarchy` reach `synth_design`; the synthesis step of the
    settings, which the run records, is the strategy's alone."""
    settings = {"fpga": PART, "clock_period": 5.5}
    plain = _run(VivadoAltSynth, tmp_path / "plain", _design(), settings, monkeypatch)
    derived = _run(
        VivadoAltSynth,
        tmp_path / "derived",
        _design(),
        {**settings, "out_of_context": True, "flatten_hierarchy": "full"},
        monkeypatch,
    )
    line = _synth_design(_script(derived, "vivado_alt_synth.tcl"))
    assert "-mode out_of_context" in line and "-flatten_hierarchy full" in line
    assert derived.settings.synth.steps == plain.settings.synth.steps
    assert "mode" not in derived.settings.synth.steps["synth"]
    assert "flatten_hierarchy" not in derived.settings.synth.steps["synth"]


@needs_tclsh
@pytest.mark.parametrize(
    "synth",
    [
        pytest.param({"strategy": "Default"}, id="mapping"),
        pytest.param({"strategy": "", "steps": {}}, id="none"),
        pytest.param({"strategy": "", "steps": {"synth": ["retiming"]}}, id="list"),
    ],
)
def test_out_of_context_reaches_synth_design_whatever_the_form_of_the_synthesis_step(
    tmp_path, monkeypatch, synth
) -> None:
    """The step is a mapping of options (a strategy's), a list of them, or none at all (an empty
    strategy gives none): the run asked for an out-of-context synthesis in each case."""
    run = _run(
        VivadoAltSynth,
        tmp_path / "run",
        _design(),
        {"fpga": PART, "clock_period": 5.5, "out_of_context": True, "synth": synth},
        monkeypatch,
    )
    assert "-mode out_of_context" in _synth_design(_script(run, "vivado_alt_synth.tcl"))


def _two_launches(flow_class, tmp_path, monkeypatch, design, plain: dict, dirty: dict, script: str):
    """The script of the same launch before, and after, one that set options of every kind:
    the run root is the same, so the scripts name the same files and compare as text."""
    root = tmp_path / "run"
    before = _script(_run(flow_class, root, design, plain, monkeypatch), script)
    _run(flow_class, root, design, {**plain, **dirty}, monkeypatch)
    after = _script(_run(flow_class, root, design, plain, monkeypatch), script)
    return before, after


@needs_tclsh
def test_a_launch_after_one_that_set_every_derived_option_is_the_launch_it_would_have_been(
    tmp_path, monkeypatch
) -> None:
    """Two launches in one process (DSE and the API make as many): the second, with the defaults,
    renders the script of a launch before the first -- and the tables of the module are as the
    module wrote them."""
    pristine = copy.deepcopy(alt.strategies)
    before, after = _two_launches(
        VivadoAltSynth,
        tmp_path,
        monkeypatch,
        _design(),
        {"fpga": PART, "clock_period": 5.5},
        {
            "out_of_context": True,
            "flatten_hierarchy": "full",
            "synth": {"strategy": "Timing", "steps": {"synth": {"retiming": True}}},
            "impl": {"strategy": "Timing", "steps": {"power_opt": {"x": 1}}},
        },
        "vivado_alt_synth.tcl",
    )
    assert after == before
    assert "out_of_context" not in after and "retiming" not in after
    assert alt.strategies == pristine


@needs_tclsh
def test_vivado_impl_launches_do_not_leave_their_options_for_the_next(
    tmp_path, monkeypatch
) -> None:
    pristine = copy.deepcopy(alt.strategies)
    plain = IMPL_SETTINGS
    dirty = {
        "impl": {
            "strategy": "AreaTiming",
            "steps": {"power_opt": {"x": 1}, "route": {"directive": "Explore"}},
        }
    }
    before, after = _two_launches(
        VivadoImpl,
        tmp_path,
        monkeypatch,
        _edif_design(tmp_path / "design"),
        plain,
        dirty,
        "vivado_impl.tcl",
    )
    assert after == before
    assert "-retime" not in after and "Explore" not in after
    assert alt.strategies == pristine


@needs_tclsh
def test_vivado_synth_derives_its_steps_where_it_renders_the_script(tmp_path, monkeypatch) -> None:
    """In project mode the steps of the settings are what the script sets as properties of the
    runs. `flatten_hierarchy`, `out_of_context`, the empty `ARGS` and `TCL` of every step and the
    hooks that the run attaches are derived: the script has them, the settings the run records
    do not."""
    settings = {"fpga": PART, "clock_period": 5.5}
    design = _design()
    plain = _run(VivadoSynth, tmp_path / "plain", design, settings, monkeypatch)
    derived = _run(
        VivadoSynth,
        tmp_path / "derived",
        design,
        {**settings, "out_of_context": True, "flatten_hierarchy": "full"},
        monkeypatch,
    )
    script = _script(derived, "vivado_synth.tcl")
    assert re.search(r"STEPS\.SYNTH_DESIGN\.flatten_hierarchy\S* .*full", script), script
    assert "-mode out_of_context" in script and ".TCL.POST" in script
    for run in ("synth", "impl"):
        assert getattr(derived.settings, run).steps == getattr(plain.settings, run).steps
    assert derived.settings.synth.steps["SYNTH_DESIGN"] == {}


@needs_tclsh
def test_vivado_synth_launches_do_not_leave_their_options_for_the_next(
    tmp_path, monkeypatch
) -> None:
    before, after = _two_launches(
        VivadoSynth,
        tmp_path,
        monkeypatch,
        _design(),
        {"fpga": PART, "clock_period": 5.5},
        {
            "out_of_context": True,
            "flatten_hierarchy": "full",
            "synth": {"steps": {"SYNTH_DESIGN": {"ARGS": {"MORE": {"OPTIONS": ["-retiming"]}}}}},
        },
        "vivado_synth.tcl",
    )
    assert after == before
    assert "out_of_context" not in after and "retiming" not in after


@needs_tclsh
@pytest.mark.parametrize(
    "options",
    [pytest.param(["-retiming"], id="list"), pytest.param("-retiming", id="text")],
)
def test_vivado_synth_adds_the_mode_to_the_more_options_the_design_gave(
    tmp_path, monkeypatch, options
) -> None:
    """An out-of-context synthesis adds its mode to the `MORE OPTIONS` of `synth_design`, and
    keeps the ones the settings already hold."""
    more = {"steps": {"SYNTH_DESIGN": {"ARGS": {"MORE": {"OPTIONS": options}}}}}
    run = _run(
        VivadoSynth,
        tmp_path / "run",
        _design(),
        {"fpga": PART, "clock_period": 5.5, "out_of_context": True, "synth": more},
        monkeypatch,
    )
    script = _script(run, "vivado_synth.tcl")
    assert re.search(
        r'-name "STEPS\.SYNTH_DESIGN\.ARGS\.MORE OPTIONS" -value "-retiming -mode out_of_context"',
        script,
    ), script
    assert run.settings.synth.steps["SYNTH_DESIGN"]["ARGS"]["MORE"]["OPTIONS"] == options
