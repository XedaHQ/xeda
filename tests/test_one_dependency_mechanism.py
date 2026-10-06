"""The oracles of PC's one dependency mechanism (`41-plan-pc` section 6), as the tasks that need
them build them. So far: O-ND3, and for OpenROAD O-ND1 and O-ND2.

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
import inspect
from collections.abc import Callable, Iterator
from typing import Any

import pytest

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
                cls.enable_output(settings, name)
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


def test_openroad_passes_no_synthesis_resources():
    """PCD9 step (a): synthesis derives its files in its own run directory."""
    tree = ast.parse(inspect.getsource(inspect.getmodule(Openroad)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_dependency"
    ]
    for call in calls:
        assert len(call.args) == 2
        assert not call.keywords
    assert "copy_resources" not in inspect.getsource(Openroad)
    assert "yosys_settings.liberty" not in inspect.getsource(Openroad)


def test_openroad_registers_no_undeclared_dependency():
    """O-ND1: the resolver alone supplies OpenROAD's synthesis prerequisite."""
    assert Openroad.Settings.dependency_settings == {}
    for field in Openroad.Settings.model_fields.values():
        annotation = field.annotation
        assert not (isinstance(annotation, type) and issubclass(annotation, Flow.Settings))
    assert "add_dependency" not in inspect.getsource(Openroad.init)
    inputs = declared_inputs(Openroad)
    assert inputs["netlist"].producer == "yosys"
    assert inputs["netlist"].output == "netlist"


def test_openroad_reads_no_producer_state():
    """O-ND2: a producer's file is reached through self.inputs alone."""
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
    assert "pop_dependency" not in inspect.getsource(Openroad)
