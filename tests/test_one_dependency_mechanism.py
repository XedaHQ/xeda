"""The oracles of the one dependency mechanism: no flow registers a dependency of its own, and
declared flows reach a producer's files only through the resolver.

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
from xeda.flow import MetricSpec
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
from xeda.flows import VivadoSynth, Yosys

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
                # an optional output with no `enabled_by` (a file of the platform's, say) is
                # refused by design: a consumer cannot switch it on
                if not setting and declaration.enabled_by is not None:
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
        ("vivado_impl", "bitstream"),
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
    """`bitstream` has no default to switch on: without `VivadoImplementation.enable_output`, a
    consumer's demand would fail at resolve time, which is what this oracle exists to catch."""
    from xeda.flows.vivado.vivado_synth import VivadoImplementation

    inherited = Flow.__dict__["enable_output"]
    assert VivadoImplementation.__dict__.get("enable_output") is not None
    monkeypatch.setattr(VivadoImplementation, "enable_output", inherited)
    problems = switch_problems(VivadoSynth)
    assert any(
        "bitstream" in p and "give `bitstream` a value" in p and "minimal settings" in p
        for p in problems
    ), problems


# ------------------------------------------------------------------------------- no cross-read

#: The attributes that hold a flow's own state. Another flow's are reached through `self.inputs`
#: alone, which names the files a producer declared.
STATE_ATTRIBUTES = frozenset({"settings", "run_path", "artifacts", "results", "outputs", "inputs"})
STATE_ACCESSORS = frozenset({"getattr", "setattr", "hasattr", "delattr"})
#: The code a flow is built from: the packages of the flows and of their base classes, and the
#: cocotb support. Every module of them is scanned, whichever flow reaches it.
FLOW_CODE_ROOTS = ("flows", "flow", "cocotb.py")
PACKAGE_ROOT = Path(xeda.__file__).parent


class _Function:
    """One function of a flow's code: its name, its parameters and how a call binds them."""

    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef, owner_class: str | None):
        self.node = node
        args = node.args
        self.params = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        static = any(ast.unparse(d) == "staticmethod" for d in node.decorator_list)
        #: a method: a call `x.f(...)` binds the first parameter to `x`
        self.bound = owner_class is not None and not static and bool(self.params)
        #: the names a call can use: a constructor is called by the name of its class
        self.names = [owner_class] if node.name == "__init__" and owner_class else [node.name]
        self.constructor = node.name == "__init__"

    def argument(self, call: ast.Call, param: str) -> ast.expr | None:
        """What `call` passes as `param`; None if it cannot be told (a star argument)."""
        for keyword in call.keywords:
            if keyword.arg == param:
                return keyword.value
        positional = [a.arg for a in (*self.node.args.posonlyargs, *self.node.args.args)]
        if param not in positional or any(isinstance(a, ast.Starred) for a in call.args):
            return None
        index = positional.index(param)
        if self.bound and (self.constructor or isinstance(call.func, ast.Attribute)):
            index -= 1
        return call.args[index] if 0 <= index < len(call.args) else None


def _owner(name: str, scope: tuple[_Function, ...]) -> _Function | None:
    """The innermost enclosing function that has a parameter `name`."""
    return next((f for f in reversed(scope) if name in f.params), None)


def _is_the_instance(name: str, scope: tuple[_Function, ...]) -> bool:
    """Whether `name` is the `self` of the method that encloses the code (a function nested in a
    method sees it too). A parameter that is merely called `self` is no instance."""
    owner = _owner(name, scope)
    return name == "self" and owner is not None and owner.bound and owner.params[0] == "self"


class _Reads(ast.NodeVisitor):
    """The reads of state attributes in some code, and its functions and calls."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.scope: list[_Function] = []
        self.classes: list[str] = []
        self.functions: list[_Function] = []
        #: (line, receiver name or None, text, enclosing functions) of each read of a state attribute
        self.reads: list[tuple[int, str | None, str, tuple[_Function, ...]]] = []
        #: (call, enclosing functions)
        self.calls: list[tuple[ast.Call, tuple[_Function, ...]]] = []
        #: (line, name) of each accessor (`getattr`) used other than by calling it, which would
        #: let a read of a state attribute go by another name
        self.aliases: list[tuple[int, str]] = []
        self._called: set[int] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_FunctionDef(self, node) -> None:
        in_class = bool(self.classes) and not self.scope
        function = _Function(node, self.classes[-1] if in_class else None)
        self.functions.append(function)
        self.scope.append(function)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _read(self, node: ast.AST, receiver: ast.AST, text: str) -> None:
        name = receiver.id if isinstance(receiver, ast.Name) else None
        scope = tuple(self.scope)
        if name is not None and _is_the_instance(name, scope):
            return
        self.reads.append((node.lineno, name, text, scope))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in STATE_ATTRIBUTES:
            self._read(node, node.value, f"{ast.unparse(node.value)}.{node.attr}")
        if node.attr in STATE_ACCESSORS and id(node) not in self._called:
            self.aliases.append((node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in STATE_ACCESSORS and id(node) not in self._called:
            self.aliases.append((node.lineno, node.id))

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, tuple(self.scope)))
        func = node.func
        self._called.add(id(func))
        if (
            _accessor(func)
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in STATE_ATTRIBUTES
        ):
            self._read(node, node.args[0], ast.unparse(node))
        self.generic_visit(node)


def _accessor(func: ast.expr) -> str | None:
    """The accessor (`getattr`, ...) that a call's function names, bare or qualified
    (`builtins.getattr`)."""
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr if isinstance(func, ast.Attribute) else None
    )
    return name if name in STATE_ACCESSORS else None


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return (
        func.id
        if isinstance(func, ast.Name)
        else func.attr if isinstance(func, ast.Attribute) else None
    )


def _candidates(
    call: ast.Call, scope: tuple[_Function, ...], by_name: dict[str, list[_Function]]
) -> list[_Function]:
    """The functions `call` can call, by its name: `name(...)` a function or a constructor, never a
    method; `self.name(...)` a method, never a function; `other.name(...)` either (a module's
    function or a method of `other`)."""
    functions = by_name.get(_callee(call) or "", [])
    func = call.func
    if isinstance(func, ast.Name):
        return [f for f in functions if not f.bound or f.constructor]
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if _is_the_instance(func.value.id, scope):
            return [f for f in functions if f.bound and not f.constructor]
    return functions


def analyze(units: list[tuple[str, ast.AST]]) -> tuple[list[str], set[str]]:
    """How the code in `units`, as `(module, tree)`, reads the state of anything but the flow it
    runs as, and which parameters carry the flow into a function (as `module:function.param`).

    One rule: code reads the state attributes (`STATE_ATTRIBUTES`) of the instance it is a method
    of. A function can take the flow as a parameter and read its state, or pass it on to one that
    does, which is the same thing as long as every call passes the instance or, in such a
    function, its own such parameter -- and there is a call. A read of a state attribute of
    anything else (a local, an attribute, a call), a call that passes something else for such a
    parameter and a parameter that no call gives a value are the problems.

    Scope. The scan guards against an accidental regression in the spellings a maintainer would
    write, not against code that hides a read. It recognizes: a direct read of a state attribute
    on any receiver but the instance; the accessors `getattr`, `setattr`, `hasattr` and `delattr`
    called by their bare or qualified name (`builtins.getattr`) with a string literal as the field
    name, and any other use of one, which could hide a read; a read through a local name (a local
    is no parameter, so it is no flow); and a helper or method whose every call passes the flow.
    It does no data-flow analysis across functions or containers: a helper reached through another
    name (`reader = read_state`), a flow kept in a list or a dictionary, one returned by a call,
    and an accessor given a computed field name are not followed. Code review is the backstop for
    those."""
    problems: list[str] = []
    visits = []
    for label, tree in units:
        visit = _Reads(label)
        visit.visit(tree)
        visits.append(visit)
    for visit in visits:
        problems += [
            f"{visit.label}:{line}: uses `{name}` other than by calling it: a read of a state "
            "attribute through another name cannot be seen"
            for line, name in visit.aliases
        ]
    #: (function node id, parameter) -> where it is
    place: dict[tuple[int, str], str] = {}
    by_name: dict[str, list[_Function]] = {}
    for visit in visits:
        for function in visit.functions:
            for name in function.names:
                by_name.setdefault(name, []).append(function)
            for param in function.params:
                place[(id(function.node), param)] = (
                    f"{visit.label}:{function.node.lineno}: {function.node.name}.{param}"
                )
    #: the parameters read as the flow's state, or passed on to one that is
    tracked: set[tuple[int, str]] = set()
    for visit in visits:
        for line, name, text, scope in visit.reads:
            owner = _owner(name, scope) if name is not None else None
            if owner is None:
                problems.append(f"{visit.label}:{line}: reads `{text}`, not the flow's own state")
            else:
                tracked.add((id(owner.node), name))  # type: ignore[arg-type]
    changed = True
    while changed:
        changed = False
        for visit in visits:
            for call, scope in visit.calls:
                for function in _candidates(call, scope, by_name):
                    for param in function.params:
                        if (id(function.node), param) not in tracked:
                            continue
                        argument = function.argument(call, param)
                        if not isinstance(argument, ast.Name):
                            continue
                        if _is_the_instance(argument.id, scope):
                            continue
                        owner = _owner(argument.id, scope)
                        if owner is not None and (id(owner.node), argument.id) not in tracked:
                            tracked.add((id(owner.node), argument.id))
                            changed = True
    given: set[tuple[int, str]] = set()
    for visit in visits:
        for call, scope in visit.calls:
            callee = _callee(call)
            for function in _candidates(call, scope, by_name):
                for param in function.params:
                    if (id(function.node), param) not in tracked:
                        continue
                    given.add((id(function.node), param))
                    argument = function.argument(call, param)
                    allowed = isinstance(argument, ast.Name) and (
                        _is_the_instance(argument.id, scope)
                        or any(
                            (id(f.node), argument.id) in tracked
                            for f in scope
                            if argument.id in f.params
                        )
                    )
                    if not allowed:
                        shown = (
                            "an argument that cannot be told"
                            if argument is None
                            else f"`{ast.unparse(argument)}`"
                        )
                        problems.append(
                            f"{visit.label}:{call.lineno}: passes {shown} as `{param}` of "
                            f"`{callee}`, which reads its state: pass the flow itself"
                        )
    for key in sorted(tracked - given, key=place.get):  # type: ignore[arg-type]
        problems.append(f"{place[key]}: read as the flow's state, but no call gives it a value")
    return problems, {place[key] for key in tracked}


def cross_reads(units: list[tuple[str, ast.AST]]) -> list[str]:
    return analyze(units)[0]


def flow_code_files() -> list[Path]:
    """Every module of the code that flows are built from (`FLOW_CODE_ROOTS`)."""
    files: list[Path] = []
    for name in FLOW_CODE_ROOTS:
        path = PACKAGE_ROOT / name
        files += sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return files


def flow_code() -> list[tuple[str, ast.AST]]:
    return [
        (str(path.relative_to(PACKAGE_ROOT)), ast.parse(path.read_text()))
        for path in flow_code_files()
    ]


def test_the_scan_holds_every_module_a_flow_is_built_from() -> None:
    """Not vacuous: every class of every registered flow's MRO is in a scanned module, and the
    helpers that take the flow as a parameter are found in the scan."""
    scanned = {Path(path).resolve() for path in flow_code_files()}
    for cls, name in FLOWS:
        for klass in cls.__mro__:
            if klass.__module__.startswith("xeda."):
                assert Path(inspect.getfile(klass)).resolve() in scanned, f"{name}: {klass}"
    _, parameters = analyze(flow_code())
    for helper in ("judge_evidence.flow", "project_outputs.flow", "declare_outputs.flow"):
        assert any(f": {helper}" in p for p in parameters), helper


def test_no_flow_reads_the_state_of_another_flow() -> None:
    """Another flow's files reach a flow through its declared inputs alone, so a consumer that
    is judged on its inputs (the trace records them) is judged on everything it read."""
    assert cross_reads(flow_code()) == []


READS_A_LOCAL = """
class Consumer:
    def run(self):
        producer = self.something()
        return producer.results["outputs"]
"""
READS_AN_ATTRIBUTE = """
class Consumer:
    def run(self):
        return self.tool.run_path / "x"
"""
READS_BY_NAME = """
class Consumer:
    def run(self):
        return getattr(self.dependency, "artifacts")
"""
READS_THROUGH_AN_ALIAS = """
class Consumer:
    def run(self):
        read = getattr
        return read(self.dependency, "results")
"""
READS_BY_QUALIFIED_NAME = """
import builtins

class Consumer:
    def run(self):
        return builtins.getattr(self.dependency, "results")
"""
READS_THROUGH_A_QUALIFIED_ALIAS = """
import builtins

class Consumer:
    def run(self):
        read = builtins.getattr
        return read(self.dependency, "results")
"""
READS_IN_A_FUNCTION_WITH_NO_INSTANCE = """
def free():
    return self.results
"""
HELPER = """
def helper(flow):
    return flow.settings.x

def forwarding(flow):
    return helper(flow)
"""
PASSES_SELF = HELPER + """
class Consumer:
    def run(self):
        def nested():
            return helper(self)
        return forwarding(self) + helper(flow=self) + nested()
"""
PASSES_ANOTHER = HELPER + """
class Consumer:
    def run(self):
        return forwarding(self.producer)
"""
PASSES_A_LOCAL_THROUGH_THE_HELPER = HELPER + """
class Consumer:
    def run(self):
        producer = self.producer
        return helper(producer)
"""
PASSES_A_STAR = HELPER + """
class Consumer:
    def run(self):
        return helper(*self.producers)
"""
METHOD = """
class Consumer:
    def read(self, other):
        return other.outputs

    def run(self):
        return self.read(self.producer)
"""
#: a helper class, which no flow inherits from, whose method reads the state of what it is given
HELPER_CLASS = """
class Reader:
    def read(self, other):
        return other.results

class Consumer:
    def run(self):
        return Reader().read(self.producer)
"""
HELPER_CLASS_CONSTRUCTOR = """
class Reader:
    def __init__(self, other):
        self.value = other.results

class Consumer:
    def run(self):
        return Reader(self.producer)
"""
#: a parameter that is merely called `self` is no instance
PARAMETER_CALLED_SELF = """
def read(self):
    return self.results

class Consumer:
    def run(self):
        return read(self.producer)
"""
NO_CALLER = """
def read(flow):
    return flow.artifacts
"""
METHOD_NOT_FOLLOWED = """
class Reader:
    def read(self, flow):
        return flow.results

    def other(self):
        return 1

class Consumer:
    def run(self):
        return Reader().other()
"""


def _problems(source: str) -> list[str]:
    return cross_reads([("mutant", ast.parse(source))])


def test_a_read_of_the_state_of_anything_but_the_instance_is_found() -> None:
    for source, text in (
        (READS_A_LOCAL, "`producer.results`"),
        (READS_AN_ATTRIBUTE, "`self.tool.run_path`"),
        (READS_BY_NAME, "getattr(self.dependency, 'artifacts')"),
        (READS_BY_QUALIFIED_NAME, "builtins.getattr(self.dependency, 'results')"),
        (READS_IN_A_FUNCTION_WITH_NO_INSTANCE, "`self.results`"),
    ):
        (problem,) = _problems(source)
        assert text in problem and "not the flow's own state" in problem, problem


def test_an_accessor_that_is_not_called_by_its_name_is_found() -> None:
    (problem,) = _problems(READS_THROUGH_AN_ALIAS)
    assert "uses `getattr` other than by calling it" in problem, problem
    (problem,) = _problems(READS_THROUGH_A_QUALIFIED_ALIAS)
    assert "uses `builtins.getattr` other than by calling it" in problem, problem


def test_a_helper_that_is_only_ever_given_the_flow_is_not_a_problem() -> None:
    assert _problems(PASSES_SELF) == []
    assert _problems("class C:\n    def run(self):\n        return self.inputs.x\n") == []


@pytest.mark.parametrize(
    "source, argument",
    [
        (PASSES_ANOTHER, "self.producer"),
        (PASSES_A_LOCAL_THROUGH_THE_HELPER, "producer"),
        (PASSES_A_STAR, "cannot be told"),
        (METHOD, "self.producer"),
        (HELPER_CLASS, "self.producer"),
        (HELPER_CLASS_CONSTRUCTOR, "self.producer"),
        (PARAMETER_CALLED_SELF, "self.producer"),
    ],
    ids=[
        "another",
        "local",
        "star",
        "method",
        "helper-class",
        "constructor",
        "parameter-called-self",
    ],
)
def test_a_helper_given_anything_but_the_flow_is_found(source: str, argument: str) -> None:
    problems = _problems(source)
    assert any(argument in p and "pass the flow itself" in p for p in problems), problems


@pytest.mark.parametrize("source", [NO_CALLER, METHOD_NOT_FOLLOWED], ids=["function", "method"])
def test_a_reader_of_a_flow_s_state_that_nothing_calls_is_found(source: str) -> None:
    (problem,) = _problems(source)
    assert "no call gives it a value" in problem, problem


def test_a_stand_in_flow_that_reads_a_producer_through_a_helper_is_found() -> None:
    from xeda.flow import registered_flows

    class _Spying(Flow):
        """Reads its producer's results through a module-level helper."""

        results_description: dict[str, MetricSpec] = {}

        def run(self) -> None:
            _read_results(self.producer_flow)  # type: ignore[attr-defined]

    def _read_results(flow):
        return flow.results

    # the stand-in's helper is local to this test, so the check runs on its source text
    source = textwrap.dedent(inspect.getsource(_Spying)) + textwrap.dedent(
        inspect.getsource(_read_results)
    )
    for name in (_Spying.name, _Spying.__name__):
        registered_flows.pop(name, None)
    assert any("passes `self.producer_flow` as `flow`" in p for p in _problems(source))


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

    from .test_isolation import DESIGNS, SQRT_DESIGN, UNPLANNABLE, sweep_settings

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
    settings = sweep_settings(flow_class, world)
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

        results_description: dict[str, MetricSpec] = {}

        def init(self) -> None:
            self.add_dependency(Yosys, {})

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
