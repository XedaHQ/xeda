"""The oracles of the one dependency mechanism: no flow registers a dependency of its own, and
declared flows reach a producer's files only through the resolver. For OpenROAD, see the tests
on its synthesis prerequisite and on its reads of producer state.

Over every registered flow and every combination of its target-narrowing settings reachable from
`settings_samples.PROBES`:

* an input with a default producer names a registered flow and, of its declared outputs, one that
  fits (`chains.fitting_outputs`, the one edge predicate), and a narrowing of either end by the
  settings stays inside the declared vocabulary;
* an output with an `enabled_by` can be switched on the way a consumer switches it on
  (`Flow.enable_output`, from the resolver) without a setting that has no default raising "give
  `<setting>` a value" -- which would make a consumer's demand fail at resolve time.
  `vivado_synth`'s `bitstream` is exactly that case: it has no default, so its
  `enable_output` override names one;
* no source type a design may state by file suffix displaces a producer it need not: a netlist
  (`VerilogNetlist`) is given by `type` only.

Each check is a function returning its problems, so the test that proves it has teeth can hand it
a broken world and see it find them.
"""

import ast
import json
import inspect
import re
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import xeda
from xeda.design import TYPE_ONLY, SourceType
from xeda.flow import Flow, FlowSettingsError
from xeda.flow.io import (
    declared_inputs,
    declared_outputs,
    output_enabled,
    selected_types,
)
from xeda.flow_runner import get_flow_class
from xeda.flow_runner.chains import fitting_outputs
from xeda.flows import Openroad, VivadoSynth

from .settings_samples import PROBES, flow_classes, minimal_settings

FLOWS = flow_classes()
DECLARED = [(cls, name) for cls, name in FLOWS if declared_inputs(cls) or declared_outputs(cls)]
DECLARED_IDS = [name for _, name in DECLARED]


def _variants(cls: type[Flow]) -> Iterator[tuple[str, Any, Any]]:
    """`cls`'s minimal settings, and with each setting set to each probe, for those the model
    accepts: `(setting, probe, settings)`; the unvaried ones come first as `("", None, ...)`."""
    base = minimal_settings(cls)
    yield "", None, cls.Settings.from_input(base)
    for name in cls.Settings.model_fields:
        if name.endswith("_"):
            continue
        for probe in PROBES:
            try:
                yield name, probe, cls.Settings.from_input({**base, name: probe})
            except FlowSettingsError:
                continue


def edge_problems(
    cls: type[Flow], resolve: Callable[[str], type[Flow]] = get_flow_class
) -> list[str]:
    """What is wrong with the edges `cls`'s inputs name, by their default producers."""
    problems: list[str] = []
    for name, declaration in declared_inputs(cls).items():
        if declaration.producer is None:
            continue
        where = f"{cls.name}.{name} <- {declaration.producer}"
        try:
            producer = resolve(declaration.producer)
        except Exception as error:
            problems.append(f"{where}: no such flow ({error})")
            continue
        produced = declared_outputs(producer)
        if declaration.output is not None and declaration.output not in produced:
            problems.append(f"{where}: {producer.name} has no output `{declaration.output}`")
            continue
        fitting, _ = fitting_outputs(producer, declaration, output=declaration.output)
        if not fitting:
            problems.append(f"{where}: no declared output of {producer.name} fits the input")
            continue
        # narrowed by the settings, at either end, an edge stays inside what is declared
        for end, owner, names in (("input", cls, [name]), ("output", producer, fitting)):
            for kind in names:
                for problem in _narrowings(owner, kind, end == "output"):
                    problems.append(f"{where}: its {end} `{kind}`: {problem}")
    return problems


def _narrowings(cls: type[Flow], name: str, output: bool) -> list[str]:
    """Problems of narrowing `cls`'s `name` by any settings variant: it must give a subset of the
    declared types (`selected_types` refuses anything else with a `ValueError`)."""
    problems: list[str] = []
    for setting, probe, settings in _variants(cls):
        try:
            selected_types(cls, settings, name, output=output)
        except Exception as error:
            problems.append(f"with {setting}={probe!r}: {type(error).__name__}: {error}")
    return problems


def switch_problems(cls: type[Flow]) -> list[str]:
    """What stops a consumer from switching `cls`'s outputs on, from its minimal settings and
    from every variant of them: only a `ValueError` (a refusal) is acceptable, never another
    exception; and from the minimal settings every `enabled_by` output must switch on."""
    problems: list[str] = []
    for name, declaration in declared_outputs(cls).items():
        for setting, probe, settings in _variants(cls):
            where = (
                f"{cls.name}.{name} with {setting}={probe!r}" if setting else f"{cls.name}.{name}"
            )
            try:
                cls.enable_output(settings, name, design_name="design")
            except ValueError as error:
                if not setting:
                    problems.append(
                        f"{where}: cannot be switched on from minimal settings: {error}"
                    )
                continue
            except Exception as error:
                problems.append(f"{where}: {type(error).__name__}: {error}")
                continue
            if not output_enabled(settings, declaration):
                problems.append(f"{where}: enable_output returned but it is not enabled")
    return problems


def naming_problems(cls: type[Flow], design_name: str = "design") -> list[str]:
    """What a consumer's switch names otherwise than the output's conventional name, for each
    output whose `enabled_by` is a deliverable: switched on from the minimal settings, the
    setting must name what the run writes when it is given a location (`outputs/<design>.<ext>`),
    so one output has one name however it is asked for."""
    from xeda.dataclass import DELIVERABLE_ROLE, written_role

    problems: list[str] = []
    for name, declaration in declared_outputs(cls).items():
        field = declaration.enabled_by
        if field is None or written_role(cls.Settings, field) != DELIVERABLE_ROLE:
            continue
        settings = cls.Settings.from_input(minimal_settings(cls))
        cls.enable_output(settings, name, design_name=design_name)
        conventional = settings.conventional_output(field, design_name)
        value = getattr(settings, field)
        if conventional is None or value is None or Path(value) != Path(conventional):
            problems.append(f"{cls.name}.{name}: switched on as {value}, not {conventional}")
    return problems


@pytest.mark.parametrize("cls", [cls for cls, _ in DECLARED], ids=DECLARED_IDS)
def test_a_deliverable_a_consumer_switches_on_has_its_conventional_name(cls) -> None:
    assert naming_problems(cls) == []


def test_the_naming_sweep_covers_the_vivado_bitstream_and_the_simulation_s_activity() -> None:
    from xeda.dataclass import DELIVERABLE_ROLE, written_role

    swept = {
        (cls.name, name)
        for cls, _ in DECLARED
        for name, declaration in declared_outputs(cls).items()
        if declaration.enabled_by is not None
        and written_role(cls.Settings, declaration.enabled_by) == DELIVERABLE_ROLE
    }
    assert {
        ("vivado_synth", "bitstream"),
        ("vivado_alt_synth", "bitstream"),
        ("vivado_postsynth_sim", "saif"),
    } <= swept


def test_a_fixed_bitstream_name_is_found(monkeypatch) -> None:
    """A consumer's switch that names the bitstream otherwise than its conventional name, as
    `outputs/bitstream.bit` once did, is what the naming sweep exists to catch."""

    def fixed(cls, settings, name, *, design_name):
        settings.bitstream = Path("outputs/bitstream.bit")

    monkeypatch.setattr(VivadoSynth, "enable_output", classmethod(fixed))
    (problem,) = naming_problems(VivadoSynth)
    assert "outputs/bitstream.bit" in problem and "outputs/design.bit" in problem


@pytest.mark.parametrize("cls", [cls for cls, _ in DECLARED], ids=DECLARED_IDS)
def test_every_declared_edge_names_a_registered_flow_and_one_of_its_outputs(cls) -> None:
    assert edge_problems(cls) == []


@pytest.mark.parametrize("cls", [cls for cls, _ in DECLARED], ids=DECLARED_IDS)
def test_every_enabled_by_output_can_be_switched_on_by_a_consumer(cls) -> None:
    assert switch_problems(cls) == []


def test_there_are_declared_flows_with_edges_and_with_switches() -> None:
    """The sweeps above are not vacuous."""
    assert {"nextpnr", "fpga_pack", "openfpgaloader"} <= {
        cls.name for cls, _ in DECLARED if any(d.producer for d in declared_inputs(cls).values())
    }
    assert {"vivado_synth", "vivado_alt_synth", "yosys_fpga"} <= {
        cls.name for cls, _ in DECLARED if any(d.enabled_by for d in declared_outputs(cls).values())
    }


def test_a_netlist_is_given_by_type_never_inferred_from_a_suffix() -> None:
    """A design source of a type an input takes displaces that input's default producer; a type no
    suffix infers cannot do it by accident (`SOURCE_SUFFIXES`)."""
    assert SourceType.VerilogNetlist in TYPE_ONLY


# ------------------------------------------------------------------------------------- teeth


def test_an_edge_to_an_unknown_flow_is_found() -> None:
    from xeda.flows import Nextpnr

    def resolve(name: str):
        raise LookupError(f"unknown flow {name}")

    (problem,) = edge_problems(Nextpnr, resolve)
    assert "nextpnr.netlist <- yosys_fpga: no such flow" in problem


def test_an_edge_to_a_flow_without_the_output_is_found() -> None:
    from xeda.flows import FpgaPack, Nextpnr

    problems = edge_problems(Nextpnr, lambda name: FpgaPack)
    assert any("no output" in p or "fits the input" in p for p in problems), problems


def test_a_bitstream_switch_without_its_override_is_found(monkeypatch) -> None:
    """`bitstream` has no default to switch on: without `VivadoSynth.enable_output`, a consumer's
    demand would fail at resolve time, which is what this oracle exists to catch."""
    inherited = Flow.__dict__["enable_output"]
    assert VivadoSynth.__dict__.get("enable_output") is not None
    monkeypatch.setattr(VivadoSynth, "enable_output", inherited)
    problems = switch_problems(VivadoSynth)
    assert any(
        "bitstream" in p and "give `bitstream` a value" in p and "minimal settings" in p
        for p in problems
    ), problems


# ------------------------------------------------------------------------------------- OpenROAD


def test_openroad_hands_synthesis_nothing_but_its_declared_input():
    """Synthesis derives its files in its own run directory."""
    source = inspect.getsource(inspect.getmodule(Openroad))
    assert "yosys_settings.liberty" not in source
    inputs = declared_inputs(Openroad)
    assert inputs["netlist"].producer == "yosys"
    assert inputs["netlist"].output == "netlist"


def test_openroad_reads_no_producer_state():
    """A producer's file is reached through self.inputs alone."""
    tree = ast.parse(inspect.getsource(inspect.getmodule(Openroad)))
    state = {"settings", "run_path", "artifacts", "results", "outputs", "inputs"}
    other = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in state
        and not (isinstance(node.value, ast.Name) and node.value.id == "self")
    ]
    assert not other


# ------------------------------------------------------------------------- one-node plans


def _leaf_flow_launch(flow_class, tmp_path, monkeypatch):
    """Launch `flow_class` as the isolation sweep does. The world, and what the resolver was asked
    and answered for it: `(requested flow, its plan or None, the refusal or None)`."""
    from xeda.flow_runner import DefaultRunner

    from .test_isolation import _launch, _world

    outcomes: list = []
    resolve = DefaultRunner.resolve

    def spy(self, flow_cls, *args, **kwargs):
        try:
            plan = resolve(self, flow_cls, *args, **kwargs)
        except Exception as error:
            outcomes.append((flow_cls, None, error))
            raise
        outcomes.append((flow_cls, plan, None))
        return plan

    monkeypatch.setattr(DefaultRunner, "resolve", spy)
    world = _world(tmp_path)
    _launch(flow_class, world, monkeypatch, "clean", [])
    return world, outcomes


@pytest.mark.parametrize("flow_class", [cls for cls, _ in FLOWS], ids=[n for _, n in FLOWS])
def test_every_flow_is_launched_as_a_plan_and_a_flow_without_inputs_is_one_node(
    flow_class, tmp_path, monkeypatch
):
    """A flow that declares nothing goes through the resolver like any other: its plan has one
    node, and that node is what a launch without a plan used to make -- the run directory, the
    recorded settings and the identity are those of the flow's own validated settings."""
    from pathlib import Path

    from xeda import Design
    from xeda.flow import flowrun_hash
    from xeda.flow_runner.bindings import node_identity
    from xeda.flow_runner.default_runner import DefaultRunner
    from xeda.flow_runner.trace import as_recorded

    from .test_isolation import DESIGNS, EXTRA_SETTINGS, SQRT_DESIGN, UNPLANNABLE

    world, outcomes = _leaf_flow_launch(flow_class, tmp_path, monkeypatch)
    asked = [(plan, error) for cls, plan, error in outcomes if cls is flow_class]
    assert len(asked) == 1, f"{flow_class.name} was not launched through the resolver"
    plan, refusal = asked[0]
    if refusal is not None:
        reason = UNPLANNABLE.get(flow_class.name)
        assert reason is not None, f"planning refused {flow_class.name}: {refusal}"
        assert reason in str(refusal)
        return
    assert flow_class.name not in UNPLANNABLE, f"{flow_class.name} plans now: unlist it"
    node = plan.node(flow_class.name)
    rtl, tb = DESIGNS.get(flow_class.name, SQRT_DESIGN)
    design = Design(
        name="sqrt",
        design_root=world.work,
        rtl=rtl,
        tb=tb,
        language={"vhdl": {"standard": "2008"}},
    )
    settings = {**minimal_settings(flow_class), **EXTRA_SETTINGS.get(flow_class.name, {})}
    again = DefaultRunner(world.root).plan(flow_class, design, flow_settings=settings)
    assert again.node(flow_class.name).flowrun_hash == node.flowrun_hash
    assert node.run_path == world.root / "sqrt" / flow_class.name
    if declared_inputs(flow_class) or declared_outputs(flow_class):
        return
    assert [n.name for n in plan.nodes] == [flow_class.name]
    assert node.inputs == () and node.origins == ()
    assert node.flowrun_hash == node_identity(node.settings_hash)
    # what a launch without a plan made: the flow's own settings, validated in the same context
    direct = flow_class.Settings.from_input(
        settings, design_root=design.root_path, runner_cwd=Path.cwd()
    )
    assert node.settings_hash == flowrun_hash(flow_class.name, direct, design.name)
    recorded = json.loads((node.run_path / "settings.json").read_text())
    assert recorded["flow_settings"] == as_recorded(direct)
    assert recorded["flowrun_hash"] == node.flowrun_hash


# ------------------------------------------------------------------------- no registration

REMOVED_NAMES = (
    "add_dependency",
    "resolve_dependency",
    "_run_dependencies",
    "dependency_settings",
    "completed_dependencies",
    "pop_dependency",
    "copy_resources",
    "copied_resources_dir",
    "flow_settings_from_sections",
)


def registration_problems(cls: type[Flow]) -> list[str]:
    """How `cls` still takes part in the removed mechanism: a table of nested producer settings,
    a field holding another flow's settings, or an `add_dependency` call in an `init()` along
    its MRO."""
    problems: list[str] = []
    if getattr(cls.Settings, "dependency_settings", {}):
        problems.append(f"{cls.name}: Settings.dependency_settings is not empty")
    for name, field in cls.Settings.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, Flow.Settings):
            problems.append(f"{cls.name}: field `{name}` holds another flow's settings")
    for klass in cls.__mro__:
        init = klass.__dict__.get("init")
        if init is None:
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_dependency"
            ):
                problems.append(f"{cls.name}: {klass.__name__}.init calls add_dependency")
    return problems


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=[name for _, name in FLOWS])
def test_no_flow_registers_a_dependency_or_nests_another_flow_s_settings(cls) -> None:
    assert registration_problems(cls) == []


def test_a_flow_that_registers_a_dependency_is_found() -> None:
    class _Registering(Flow):
        """Registers a dependency in its init()."""

        results_description: dict[str, str] = {}

        def init(self) -> None:
            self.add_dependency(Openroad, {})

        def run(self) -> None:
            pass

    try:
        problems = registration_problems(_Registering)
    finally:
        from xeda.flow import registered_flows

        for name in (_Registering.name, _Registering.__name__):
            registered_flows.pop(name, None)
    assert any("calls add_dependency" in p for p in problems), problems


def names_left(root: Path) -> dict[str, list[str]]:
    """The names of the removed mechanism in the text files under `root`, by name."""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix in {".pyc", ".so", ".png", ".gz"}:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for name in REMOVED_NAMES:
            if re.search(re.escape(name), text):
                found.setdefault(name, []).append(str(path.relative_to(root)))
    return found


def test_no_name_of_the_removed_mechanism_is_left_in_the_package() -> None:
    assert names_left(Path(xeda.__file__).parent) == {}


def test_the_text_scan_finds_a_name_left_in_a_file(tmp_path) -> None:
    (tmp_path / "flow.py").write_text("self.add_dependency(X)\n")
    (tmp_path / "doc.md").write_text("see copy_resources\n")
    assert names_left(tmp_path) == {"add_dependency": ["flow.py"], "copy_resources": ["doc.md"]}
