"""Every setting of a flow is read by something.

A user can set a setting that nothing reads, and nothing happens: the flow accepts it, records it
in `settings.json`, hashes it into the run's identity and ignores it. This module finds such
settings in every product flow, by reading the flow's code and templates. A setting that nothing
reads fails the test unless a table below says why that is fine.

The code of a flow (`flow_reads`) is:

* the module-level statements of the modules of its MRO classes, and the class-level statements
  of those classes;
* a method of those classes when the launcher calls it (the names `Flow` defines, `FLOW_API`) or
  other code names it. A method that a class overrides counts only where the override calls
  `super()`. A base class of two flows holds the code of both, and each flow reads what its own
  entry points reach;
* the module-level functions that code names, in its modules and in the modules it imports them
  from (`test_source_contracts._Module`);
* a method of a settings class when code or a template names it, such as `write_verilog_flags()`;
* the templates that code names, and the templates those include
  (`test_design_parts.reachable_templates`).

A validator is not a read. It checks or normalizes a value that the flow may then ignore.

A read of the setting `x` is:

* in Python, the attribute `x` of an expression that holds the settings (`settings`, `ss`,
  `self.settings`, `flow.settings`, or `self` and `cls` inside a settings class), or
  `getattr(<that expression>, "x")`, or the text `"x"` in a literal tuple, list or set of names in
  code that also calls `getattr` on the settings with a name it holds in a variable (a table of
  option names that the code walks). `hasattr` and `setattr` are no read, nor is text in a
  dictionary, a docstring, `Field(...)`, a decorator or a `Literal[...]`, nor a value that the
  code assigns or compares;
* in a template, `settings.x` or `settings["x"]` outside a comment.

Known limit: only the top-level fields of a flow's `Settings` are checked. The leaves of a nested
model belong to the code that owns the model (`fpga`, `clocks`, `platform`, `cocotb`), and the
settings of a nested flow (`ghdl` under the yosys flows) are checked on that flow. A field that a
read only derives another value from (`abc_dff` adds a flag to a list that no template renders)
counts as read.
"""

import ast
import functools
import importlib
import inspect
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest

from xeda.dataclass import waveform_fields
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException
from xeda.flow_runner.resolver import SHARED_SETTINGS

from .flow_registry import preserved_flow_registry
from .settings_samples import flow_classes, minimal_settings
from .test_design_parts import _mro, reachable_templates
from .test_source_contracts import DEFINITIONS, _loaded_source, _Module, _uses

#: Expressions that hold a flow's settings.
SETTINGS_EXPRESSIONS = frozenset({"settings", "ss", "self.settings", "flow.settings"})
#: In the code of a settings class, the settings are `self` (or `cls`).
SETTINGS_SELF = frozenset({"self", "cls"})
#: Class-level tables whose text names things, but whose strings are no read.
NAME_TABLES = frozenset(
    {"results_description", "removed_settings", "required_settings", "results_canonical_aliases"}
)
IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
#: Template suffixes whose lines starting with `#` are comments.
HASH_COMMENT_SUFFIXES = frozenset(
    {".tcl", ".ys", ".sdc", ".xdc", ".ldc", ".fdc", ".lpf", ".pcf", ".ucf", ".sh", ".py", ".cfg"}
)


#: The methods the launcher and the rest of xeda call on a flow: whatever `Flow` defines.
FLOW_API = frozenset(vars(Flow))


@dataclass(frozen=True)
class Reads:
    """What the code and the templates of one flow read of its settings."""

    attributes: frozenset[str]
    template_names: frozenset[str]

    def reads(self, name: str) -> bool:
        return name in self.attributes or name in self.template_names


def _is_validator(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        "validator" in ast.unparse(decorator) for decorator in node.decorator_list
    )


def _calls_super(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "super" for n in ast.walk(node))


@dataclass
class Held:
    """The text a value can be, by what the code does with it: `elements` are the text the value
    is, or yields when it is looped over (the keys of a dictionary); `values` are what a lookup
    in it gives; `by_key` is what a lookup with a literal key gives."""

    elements: set[str] = field(default_factory=set)
    values: set[str] = field(default_factory=set)
    by_key: dict[str, set[str]] = field(default_factory=dict)

    def merged(self, other: "Held") -> "Held":
        by_key = {
            key: self.by_key.get(key, set()) | other.by_key.get(key, set())
            for key in self.by_key.keys() | other.by_key.keys()
        }
        return Held(self.elements | other.elements, self.values | other.values, by_key)


Env = dict[str, Held]


def _merge_envs(envs: list[Env]) -> Env:
    merged: Env = {}
    for env in envs:
        for name, held in env.items():
            merged[name] = merged[name].merged(held) if name in merged else held
    return merged


def _scalar(texts: set[str]) -> Held:
    return Held(elements=set(texts), values=set(texts))


def _held(expr: ast.AST | None, env: Env) -> Held:
    """What `expr` can be: the text in a literal tuple, list, set or dictionary, in either
    branch of a conditional, in what a variable holds, and in what a lookup (`table[key]`,
    `table.get(key)`) gives: the values of a dictionary, or the one value of a literal key."""
    if isinstance(expr, ast.Constant):
        value = expr.value
        if isinstance(value, str) and IDENTIFIER.fullmatch(value):
            return _scalar({value})
        return Held()
    if isinstance(expr, ast.Name):
        return env.get(expr.id, Held())
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        return _scalar(set().union(*(_held(item, env).elements for item in expr.elts)))
    if isinstance(expr, ast.Dict):
        keys = set().union(*(_held(key, env).elements for key in expr.keys if key is not None))
        values = set().union(*(_held(value, env).elements for value in expr.values))
        by_key = {
            key.value: _held(value, env).elements
            for key, value in zip(expr.keys, expr.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        return Held(elements=keys, values=values, by_key=by_key)
    if isinstance(expr, ast.IfExp):
        return _held(expr.body, env).merged(_held(expr.orelse, env))
    if isinstance(expr, ast.Starred):
        return _held(expr.value, env)
    if isinstance(expr, ast.BinOp):
        return _held(expr.left, env).merged(_held(expr.right, env))
    if isinstance(expr, ast.Subscript):
        return _lookup(_held(expr.value, env), expr.slice)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and expr.args
    ):
        table = _held(expr.func.value, env)
        found = _lookup(table, expr.args[0])
        if len(expr.args) > 1 and not _in_table(table, expr.args[0]):
            # the default is what `.get` gives when the key is absent, or may be absent
            default = _held(expr.args[1], env)
            return default if _is_literal_key(expr.args[0]) else found.merged(default)
        return found
    return Held()


def _is_literal_key(key: ast.AST) -> bool:
    return isinstance(key, ast.Constant) and isinstance(key.value, str)


def _in_table(table: Held, key: ast.AST) -> bool:
    return isinstance(key, ast.Constant) and key.value in table.by_key


def _lookup(table: Held, key: ast.AST) -> Held:
    if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in table.by_key:
        return _scalar(table.by_key[key.value])
    return _scalar(table.values)


def _module_env(tree: ast.Module) -> Env:
    """What the module-level variables of a module hold (`_held`)."""
    env: Env = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                env[target.id] = _held(value, env)
    return env


class _Collector(ast.NodeVisitor):
    """The reads of the settings in some code. `settings_code` is true in the methods of a
    settings class, where `self` is the settings. `names` are all the names the code uses, for
    finding the settings methods it calls."""

    def __init__(self, *, settings_code: bool = False, env: Env | None = None) -> None:
        self.settings_code = settings_code
        self.attributes: set[str] = set()
        #: the text that each variable can hold (`Held`), from the literals assigned to it, that it
        #: loops over or that a dictionary lookup gives it: a read, when `getattr` takes it
        self.env: Env = dict(env or {})
        self.names: set[str] = set()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if _is_validator(node):
            return  # a validator checks a value; it applies none
        for statement in node.body:
            self.visit(statement)

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == "Settings":
            return  # its fields are declarations, and its methods are read where something names them
        for statement in node.body:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name in ("Field", "deliverable"):
            return  # a description and an alias are no use of a setting
        # `getattr` reads a setting. `hasattr` only asks whether there is one, and `setattr` and
        # `delattr` replace or drop it.
        if name == "getattr" and len(node.args) > 1 and self._holds_settings(node.args[0]):
            key = node.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self.attributes.add(key.value)
            elif isinstance(key, ast.Name) and key.id in self.env:
                self.attributes |= self.env[key.id].elements
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.target.id in NAME_TABLES:
            return
        if node.value is not None:
            self.visit(node.value)
            self._bind(node.target, _held(node.value, self.env))

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(isinstance(t, ast.Name) and t.id in NAME_TABLES for t in node.targets):
            return
        self.generic_visit(node)
        value = _held(node.value, self.env)
        for target in node.targets:
            self._bind(target, value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.generic_visit(node)
        if isinstance(node.target, ast.Name):
            before = self.env.get(node.target.id, Held())
            self.env[node.target.id] = before.merged(_held(node.value, self.env))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if ast.unparse(node.value).endswith("Literal"):
            return
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # a docstring
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.names.add(node.attr)
        if isinstance(node.ctx, ast.Load):
            base = ast.unparse(node.value)
            if base in SETTINGS_EXPRESSIONS or (self.settings_code and base in SETTINGS_SELF):
                self.attributes.add(node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)

    def _holds_settings(self, node: ast.AST) -> bool:
        base = ast.unparse(node)
        return base in SETTINGS_EXPRESSIONS or (self.settings_code and base in SETTINGS_SELF)

    def _bind(self, target: ast.AST, value: Held) -> None:
        """An assignment replaces what the variable held (straight-line code)."""
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.env[node.id] = value

    def _branches(self, *branches: list[ast.stmt], keep_before: bool = False) -> None:
        """Visit statements that may or may not run, each from the variables as they were before,
        and hold afterwards what any of them left (and, if none may run, what was before)."""
        before = dict(self.env)
        after = [before] if keep_before else []
        for statements in branches:
            self.env = dict(before)
            for statement in statements:
                self.visit(statement)
            after.append(self.env)
        self.env = _merge_envs(after)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._branches(node.body, node.orelse)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind(node.target, Held(elements=_held(node.iter, self.env).elements))
        # a second pass lets an assignment late in the body reach a read early in it
        self._branches(node.body + node.body, node.orelse, keep_before=True)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._branches(node.body + node.body, node.orelse, keep_before=True)

    def visit_Try(self, node: ast.Try) -> None:
        handlers = [handler.body for handler in node.handlers]
        self._branches(node.body + node.orelse, *handlers, keep_before=bool(handlers))
        for statement in node.finalbody:
            self.visit(statement)

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp):
        before = dict(self.env)
        for generator in node.generators:
            self.visit(generator.iter)
            self._bind(generator.target, Held(elements=_held(generator.iter, self.env).elements))
            for condition in generator.ifs:
                self.visit(condition)
        for part in (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,):
            self.visit(part)
        self.env = before

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comprehension


def template_reads(name: str, text: str) -> set[str]:
    """The settings a template reads: `settings.x` and `settings["x"]`, outside a Jinja comment
    and, in a template of a language that has them, a line comment."""
    text = re.sub(r"{#.*?#}", "", text, flags=re.DOTALL)
    if any(name.endswith(suffix) for suffix in HASH_COMMENT_SUFFIXES):
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return set(re.findall(r"\bsettings\s*\.\s*(\w+)", text)) | set(
        re.findall(r"""\bsettings\s*\[\s*['"](\w+)['"]""", text)
    )


@dataclass(frozen=True)
class Member:
    """A method, and the module its class is in."""

    owner: str
    node: ast.AST


def settings_reads(
    sources: dict[str, str],
    classes: dict[str, set[str]],
    methods: dict[str, list[Member]],
    templates: Iterable[tuple[str, str]] = (),
    entries: frozenset[str] = frozenset(),
    order: Iterable[tuple[str, str]] = (),
    load=_loaded_source,
) -> tuple[Reads, list[ast.AST]]:
    """What the code of one flow reads of its settings, with the code that was read.

    `sources` maps the modules of the flow's classes to their text, and `classes` to the names of
    those classes in them. `methods` are the methods of the flow's settings classes by name, and
    `templates` the `(name, text)` of the templates it can render. The code of the flow is:

    * the module-level statements of its modules, and the class-level statements of its classes;
    * a method of its classes, where the launcher calls it (`entries` are the names of the
      methods that the launcher calls) or the code calls it by name. `order` is the `(module,
      class)` of its classes from the most derived: a method that a class overrides counts only
      where the override calls `super()`. A base class of two flows holds the code of both, and
      each flow reads what its own entries reach;
    * the module-level functions that code names, in its modules and in the modules it imports
      them from (`load` gives the text of an imported module);
    * a method of a settings class, where the code or a template names it.
    """
    parsed: dict[str, _Module | None] = {}

    def module(name: str) -> _Module | None:
        if name not in parsed:
            text = sources[name] if name in sources else load(name)
            parsed[name] = _Module(name, text, load) if text is not None else None
        return parsed[name]

    class_nodes: dict[tuple[str, str], ast.ClassDef] = {}
    queue: list[tuple[str, ast.AST, bool]] = []  # owner module, code, in a settings class
    for owner in sources:
        here = module(owner)
        assert here is not None
        for statement in here.tree.body:
            if isinstance(statement, ast.ClassDef):
                if statement.name in classes.get(owner, ()):
                    class_nodes[(owner, statement.name)] = statement
            elif not isinstance(statement, DEFINITIONS):
                queue.append((owner, statement, False))
    flow_methods: dict[str, list[Member]] = {}
    for key in [*order, *sorted(set(class_nodes) - set(order))]:
        if key not in class_nodes:
            continue
        for member in class_nodes[key].body:
            if isinstance(member, DEFINITIONS):
                flow_methods.setdefault(member.name, []).append(Member(key[0], member))
            else:
                queue.append((key[0], member, False))

    attributes: set[str] = set()
    names: set[str] = set(entries)
    code: list[ast.AST] = []
    functions: set[tuple[str, str]] = set()
    taken: set[tuple[str, str]] = set()
    used = frozenset().union(*(template_reads(name, text) for name, text in templates))
    names |= used  # a template calls a settings method as it reads a setting

    envs: dict[str, Env] = {}

    def module_env(name: str) -> Env:
        if name not in envs:
            here = module(name)
            envs[name] = _module_env(here.tree) if here is not None else {}
        return envs[name]

    def function_called(owner: str, qualifier: str | None, name: str) -> None:
        here = module(owner)
        assert here is not None
        target: tuple[str, str] | None = None
        if qualifier is None and name in here.functions:
            target = (owner, name)
        elif qualifier is None and name in here.names:
            target = here.names[name]
        elif qualifier in here.modules:
            target = (here.modules[qualifier], name)
        there = module(target[0]) if target else None
        if there is None or target is None or target[1] not in there.functions:
            return
        if target not in functions:
            functions.add(target)
            queue.append((target[0], there.functions[target[1]], False))

    while True:
        while queue:
            owner, node, settings_code = queue.pop()
            code.append(node)
            collector = _Collector(settings_code=settings_code, env=module_env(owner))
            collector.visit(node)
            attributes |= collector.attributes
            names |= collector.names
            for qualifier, name in _uses(node):
                function_called(owner, qualifier, name)
        for kind, table in (("flow", flow_methods), ("settings", methods)):
            for name in sorted(names):
                if name not in table or (kind, name) in taken:
                    continue
                taken.add((kind, name))
                for member in table[name]:
                    queue.append((member.owner, member.node, kind == "settings"))
                    # of a flow's methods the most derived counts, and the next if it calls super()
                    if kind == "flow" and not _calls_super(member.node):
                        break
        if not queue:
            return Reads(frozenset(attributes), used), code


def _settings_methods(cls: type[Flow]) -> dict[str, list[Member]]:
    """The methods of the settings classes of `cls` (its own and its bases'), by name. A
    validator is left out."""
    found: dict[str, list[Member]] = {}
    for klass in cls.Settings.__mro__:
        module = klass.__module__
        if not module.startswith("xeda.") or module == "xeda.dataclass":
            continue
        tree = ast.parse(inspect.getsource(sys.modules[module]))
        scope: list[ast.stmt] = tree.body
        node: ast.ClassDef | None = None
        for part in klass.__qualname__.split("."):
            node = next((s for s in scope if isinstance(s, ast.ClassDef) and s.name == part), None)
            if node is None:
                break
            scope = node.body
        if node is None:
            continue
        for statement in node.body:
            if isinstance(statement, DEFINITIONS) and not _is_validator(statement):
                found.setdefault(statement.name, []).append(Member(module, statement))
    return found


@functools.cache
def flow_reads(cls: type[Flow]) -> Reads:
    """What the code and the templates of `cls` read of its settings."""
    names_by_module: dict[str, set[str]] = {}
    for klass in _mro(cls):
        names_by_module.setdefault(klass.__module__, set()).add(klass.__name__)
    sources = {name: inspect.getsource(sys.modules[name]) for name in names_by_module}
    methods = _settings_methods(cls)
    order = [(klass.__module__, klass.__name__) for klass in _mro(cls)]
    # the templates are the ones that code names, helpers and settings methods included
    _, code = settings_reads(sources, names_by_module, methods, (), FLOW_API, order)
    templates = reachable_templates(cls, [node for top in code for node in ast.walk(top)])
    reads, _ = settings_reads(
        sources, names_by_module, methods, sorted(templates.items()), FLOW_API, order
    )
    return reads


def unread_settings(cls: type[Flow]) -> list[str]:
    """The settings of `cls` that nothing reads, in declaration order."""
    reads = flow_reads(cls)
    return [name for name in cls.Settings.model_fields if not reads.reads(name)]


PRODUCT_FLOWS = [(cls, name) for cls, name in flow_classes() if cls.__module__.startswith("xeda.")]


#: Settings that every flow has and the framework reads for it: the setting, the module that reads
#: it, the attribute that module reads, and what the module does with it.
FRAMEWORK_READS: dict[str, tuple[str, str, str]] = {
    "verbose": (
        "xeda.flow_runner.settings_layers",
        "verbose",
        "a producer takes its consumer's verbosity",
    ),
    "debug": (
        "xeda.flow_runner.settings_layers",
        "debug",
        "a producer takes its consumer's debug mode, and the launcher's `--debug` sets it",
    ),
    "quiet": (
        "xeda.flow_runner.launch_run",
        "is_quiet",
        "the launcher prints a failure unless quiet",
    ),
    "docker": ("xeda.tool", "docker", "a tool takes the image from the flow's settings"),
    "dockerized": ("xeda.tool", "dockerized", "a tool takes it from the flow's settings"),
    "print_commands": ("xeda.tool", "print_commands", "a tool takes it from the flow's settings"),
    "console_colors": ("xeda.tool", "console_colors", "a tool takes it from the flow's settings"),
}

#: Settings that every flow accepts (`xeda list-settings --all`) and that only some flows honor.
#: A flow that ignores one takes it without effect. Each must be read by at least one flow.
UNIVERSAL_SETTINGS: dict[str, str] = {
    "nthreads": "advertised for every flow (`ncpus`); the tools that have a thread count get it",
    "lib_paths": "advertised for every flow; the tools that take library paths get them",
    "redirect_stdout": "advertised for every flow; `Vivado.init` redirects the tool's output",
}

#: A flow that declares inputs or outputs is a node of the build graph, where the resolver agrees
#: these settings between the nodes that declare them.
RESOLVER_SETTINGS = frozenset(SHARED_SETTINGS)

#: `(flow, setting)` -> a value that the flow refuses. The setting stays in the model with its
#: default, and a validator refuses any other value, naming the flow that has the setting.
REFUSED_WHEN_SET: dict[tuple[str, str], object] = {
    ("vivado_alt_synth", "tcl_files"): ["hook.tcl"],
    ("vivado_alt_synth", "show_available_strategies"): True,
    ("vivado_alt_synth", "set_synth_properties"): {"A.B": 1},
    ("vivado_alt_synth", "set_impl_properties"): {"A.B": 1},
}

#: `(flow, setting)` -> why the flow does not read the setting and that is fine.
REVIEWED: dict[tuple[str, str], str] = {}


def _declares_io(cls: type[Flow]) -> bool:
    return bool(cls.Inputs.model_fields or cls.Outputs.model_fields)


def findings(cls: type[Flow], name: str) -> list[str]:
    """The settings of `cls` that nothing reads and that no table accounts for."""
    return [
        setting
        for setting in unread_settings(cls)
        if setting not in FRAMEWORK_READS
        and setting not in UNIVERSAL_SETTINGS
        and not (setting in RESOLVER_SETTINGS and _declares_io(cls))
        and (name, setting) not in REVIEWED
        and (name, setting) not in REFUSED_WHEN_SET
        and setting not in getattr(cls, "unsupported_waveforms", {})
    ]


@pytest.mark.parametrize(("cls", "name"), PRODUCT_FLOWS, ids=[name for _, name in PRODUCT_FLOWS])
def test_every_setting_of_a_flow_is_read(cls: type[Flow], name: str) -> None:
    """A setting that nothing reads is refused by the flow (take it out of the flow's settings),
    applied (read it in the flow's code or template) or, when a table above says why it is fine,
    reviewed."""
    unread = findings(cls, name)
    assert not unread, (
        f"{name}: nothing reads {', '.join(unread)}. A user can set it and nothing happens. "
        "Read it in the flow, or take it out of the flow's settings so that it is refused."
    )


def test_every_reviewed_setting_is_still_unread() -> None:
    by_name = dict(PRODUCT_FLOWS_BY_NAME)
    for (name, setting), reason in REVIEWED.items():
        assert name in by_name, f"{name} is no product flow"
        assert reason, f"({name}, {setting}) has no reason"
        assert setting in unread_settings(by_name[name]), f"{name} reads {setting} now: drop it"


@pytest.mark.parametrize(("name", "setting"), sorted(REFUSED_WHEN_SET))
def test_a_refused_setting_is_refused_and_its_default_is_not(name: str, setting: str) -> None:
    cls = dict(PRODUCT_FLOWS_BY_NAME)[name]
    assert setting in unread_settings(cls), f"{name} reads {setting} now: drop it"
    base = minimal_settings(cls)
    cls.Settings.from_input(base)
    with pytest.raises(FlowSettingsError, match=setting):
        cls.Settings.from_input({**base, setting: REFUSED_WHEN_SET[(name, setting)]})


@pytest.mark.parametrize(("cls", "name"), PRODUCT_FLOWS, ids=[name for _, name in PRODUCT_FLOWS])
def test_a_waveform_setting_a_simulator_cannot_write_is_refused(cls: type[Flow], name: str) -> None:
    """`SimFlow.unsupported_waveforms` names the waveform settings that a simulator has no way to
    write. Nothing reads them, and the planning check of the simulator refuses a value."""
    unsupported = getattr(cls, "unsupported_waveforms", {})
    formats = waveform_fields(cls.Settings)
    for setting in unsupported:
        assert setting in unread_settings(cls), f"{name} reads {setting} now: drop it"
        settings = cls.Settings.from_input(
            {**minimal_settings(cls), setting: f"dump.{formats[setting]}"}
        )
        with pytest.raises(FlowSettingsException, match=setting):
            cls.check_settings_supported(settings)


def test_every_framework_read_is_there() -> None:
    for setting, (module, attribute, reason) in FRAMEWORK_READS.items():
        text = inspect.getsource(importlib.import_module(module))
        assert reason
        assert re.search(rf"\.{attribute}\b", text), f"{module} reads no `{attribute}`"
        assert any(setting in cls.Settings.model_fields for cls, _ in PRODUCT_FLOWS)


@pytest.mark.parametrize("setting", sorted(UNIVERSAL_SETTINGS))
def test_every_universal_setting_is_read_by_a_flow(setting: str) -> None:
    assert any(
        setting in cls.Settings.model_fields and flow_reads(cls).reads(setting)
        for cls, _ in PRODUCT_FLOWS
    ), f"no flow reads {setting}"


PRODUCT_FLOWS_BY_NAME = [(name, cls) for cls, name in PRODUCT_FLOWS]


FIXTURE_FLOW = '''
def used_helper(ss):
    return ss.read_by_helper

def unused_helper(ss):
    return ss.read_by_unused_helper

OPTIONS = ("read_by_table",)
LOOKUP = {"ecp5": "read_by_lookup", "ice40": "read_by_lookup_too"}
ALIASES = {"alias": "read_by_unrelated_dict"}
RENAMES = {"read_by_lookup_key": "read_by_lookup_value"}
PAIRS = {"a": "read_by_a_value", "b": "read_by_b_value"}
KNOWN = {"known": "read_by_known_value"}
UNUSED_TABLE = ("read_by_table_nothing_walks",)

class Other:
    def run(self):
        return self.settings.read_by_other_flow

class Fixture(Flow):
    """Mentions read_by_docstring."""
    results_description = {"read_by_results_table": "text"}

    class Settings(Flow.Settings):
        direct: int = Field(0, description="Mentions read_by_description.")
        kind: Literal["read_by_literal"] = "read_by_literal"

        @field_validator("read_by_decorator")
        @classmethod
        def check(cls, value):
            return value.read_by_validator

        @property
        def derived(self):
            return self.read_through_property

        def unused_method(self):
            return self.read_by_unused_method

        def called_by_template(self):
            return self.read_through_template_call

    def run(self):
        ss = self.settings
        ss.direct
        self.settings.read_by_self_settings
        ss.derived
        used_helper(ss)
        for name in OPTIONS:
            getattr(ss, name)
        key = LOOKUP.get(self.family)
        getattr(ss, key)
        getattr(ss, "read_by_getattr")
        for name in ("read_by_local_table", "read_by_second_local_table"):
            value = getattr(self.settings, name)
        renamed = RENAMES[self.kind]
        getattr(ss, renamed)
        name = "read_by_replaced"
        name = "read_by_replacement"
        getattr(ss, name)
        if self.flag:
            branch = "read_by_if_branch"
        else:
            branch = "read_by_else_branch"
        getattr(ss, branch)
        picked = KNOWN.get("known", "read_by_unused_default")
        getattr(ss, picked)
        absent = KNOWN.get("absent", "read_by_default_of_an_absent_key")
        getattr(ss, absent)
        either = KNOWN.get(self.key, "read_by_default_of_a_variable_key")
        getattr(ss, either)
        mode = "read_before_loop"
        for item in self.items:
            mode = "read_in_loop"
        getattr(ss, mode)
        chosen = PAIRS["a"]
        getattr(ss, chosen)
        hasattr(ss, "read_by_hasattr")
        setattr(ss, "read_by_setattr", 1)
        getattr(self.tool, "read_by_getattr_on_another_object")
        getattr(ss, self.name)
        self.name.read_by_wrong_base
        ss.wave_format = "read_by_assignment"
        if ss.wave_format == "read_by_comparison":
            pass
'''

FIXTURE_TEMPLATES = [
    ("script.tcl", "{{ settings.read_in_template }}\n# settings.read_in_tcl_comment\n"),
    ("script.ys", "{# settings.read_in_jinja_comment #}{{ settings.called_by_template() }}"),
    ("script.cpp", "#define X {{ settings['read_in_cpp_line'] }}"),
]


def _fixture_methods() -> dict[str, list[Member]]:
    tree = ast.parse(FIXTURE_FLOW)
    fixture = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Fixture")
    settings = next(n for n in fixture.body if isinstance(n, ast.ClassDef))
    return {
        n.name: [Member("fixture", n)]
        for n in settings.body
        if isinstance(n, DEFINITIONS) and not _is_validator(n)
    }


def _fixture_reads() -> Reads:
    reads, _ = settings_reads(
        {"fixture": FIXTURE_FLOW},
        {"fixture": {"Fixture"}},
        _fixture_methods(),
        FIXTURE_TEMPLATES,
        frozenset({"run"}),
    )
    return reads


@pytest.mark.parametrize(
    "setting",
    [
        "direct",
        "read_by_self_settings",
        "read_through_property",
        "read_by_helper",
        "read_by_table",
        "read_by_lookup",
        "read_by_lookup_too",
        "read_by_getattr",
        "read_by_local_table",
        "read_by_second_local_table",
        "read_by_lookup_value",
        "read_by_replacement",
        "read_by_if_branch",
        "read_by_else_branch",
        "read_by_a_value",
        "read_by_known_value",
        "read_by_default_of_an_absent_key",
        "read_by_default_of_a_variable_key",
        "read_before_loop",
        "read_in_loop",
        "read_in_template",
        "read_through_template_call",
        "read_in_cpp_line",
    ],
)
def test_the_scan_sees_each_way_of_reading_a_setting(setting: str) -> None:
    assert _fixture_reads().reads(setting)


@pytest.mark.parametrize(
    "setting",
    [
        "read_by_docstring",
        "read_by_description",
        "read_by_literal",
        "read_by_decorator",
        "read_by_validator",
        "read_by_unused_method",
        "read_by_unused_helper",
        "read_by_other_flow",
        "read_by_results_table",
        "read_by_wrong_base",
        "read_by_assignment",
        "read_by_unrelated_dict",
        "read_by_lookup_key",
        "read_by_replaced",
        "read_by_b_value",
        "read_by_unused_default",
        "read_by_table_nothing_walks",
        "read_by_hasattr",
        "read_by_setattr",
        "read_by_getattr_on_another_object",
        "read_by_comparison",
        "read_in_tcl_comment",
        "read_in_jinja_comment",
    ],
)
def test_the_scan_does_not_count_what_is_no_read(setting: str) -> None:
    assert not _fixture_reads().reads(setting)


OVERRIDES = """
class Base(Flow):
    def run(self):
        return self.settings.read_by_overridden_method

    def check(self):
        return self.settings.read_by_base_check

class Calling(Base):
    def run(self):
        super().run()

class Replacing(Base):
    def run(self):
        return self.settings.read_by_override

    def check(self):
        return 1
"""


@pytest.mark.parametrize(
    ("flow", "read", "not_read"),
    [
        ("Calling", {"read_by_overridden_method"}, set()),
        ("Replacing", {"read_by_override"}, {"read_by_overridden_method", "read_by_base_check"}),
    ],
)
def test_a_method_a_class_overrides_counts_only_where_the_override_calls_super(
    flow: str, read: set[str], not_read: set[str]
) -> None:
    reads, _ = settings_reads(
        {"fixture": OVERRIDES},
        {"fixture": {"Base", flow}},
        {},
        (),
        frozenset({"run", "check"}),
        [("fixture", flow), ("fixture", "Base")],
    )
    assert all(reads.reads(name) for name in read)
    assert not any(reads.reads(name) for name in not_read)


def test_a_setting_added_to_a_real_flow_is_found() -> None:
    from xeda.dataclass import Field
    from xeda.flows import Yosys

    with preserved_flow_registry():

        class _Extended(Yosys):
            """A flow with a setting that nothing reads."""

            class Settings(Yosys.Settings):
                never_read: int = Field(0, description="No code reads it.")

        assert "never_read" in unread_settings(_Extended)
    assert "never_read" not in unread_settings(Yosys)
