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

A field that holds a settings group is not a setting. Its leaves are, each by its dotted path
(`global_placement.timing_driven.enabled`), and `unread_settings` reports the leaves that nothing
reads. A leaf `p.q.leaf` is read when:

* the leaf has a `Flag` in the `FLAGS` of its group (or is `extra_args`), and the code or a
  template calls `render_flags` on exactly that group (`render_flags(settings.p.q, ...)`: the first
  argument is an expression that holds the settings, with the path of the group). `render_flags`
  does not go into sub-groups, so a call on `p` does not read the leaves of `p.q`; or
* the chain `settings.p.q.leaf` appears in the code (as a chain of attributes) or in a template,
  outside a comment. A chain that goes on past the leaf (`settings.p.q.leaf.x`) reads it too; or
* a template alias stands for the start of the chain. `{% set gp = settings.p %}` makes
  `gp.q.leaf` and `render_flags(gp.q, ...)` count as `settings.p.q.leaf` and
  `render_flags(settings.p.q, ...)` from there to the end of that template, and in the templates it
  includes. Another template may use the same name for another group.

Code does not follow an alias: `gp = ss.global_placement` followed by `gp.leaf` is no read. Code
reads the leaf of a group by its whole chain, or has `render_flags` print the group.

The leaves of other nested models (`fpga`, `clocks`, `cocotb`, the platform) still belong to the
code that owns the model, and the settings of a nested flow (`ghdl` under the yosys flows) are
checked on that flow. A field that a read only derives another value from (`abc_dff` adds a flag
to a list that no template renders) counts as read.
"""

import ast
import functools
import importlib
import inspect
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, Optional

import pytest

from xeda.dataclass import Field, XedaBaseModel, waveform_fields
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException
from xeda.flow.settings_group import Flag, SettingsGroup
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
NAME_TABLES = frozenset({"results_description", "removed_settings", "required_settings"})
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
    #: the dotted paths below the settings that a chain of attributes names, in the code and in
    #: the templates: `settings.p.q.leaf` gives `p`, `p.q` and `p.q.leaf`
    chains: frozenset[str] = frozenset()
    #: the dotted paths of the groups that a `render_flags` call renders
    rendered: frozenset[str] = frozenset()

    def reads(self, name: str) -> bool:
        return name in self.attributes or name in self.template_names

    def reads_leaf(self, group: str, model: type[XedaBaseModel], name: str) -> bool:
        """Whether the field `name` of the group `model` at the dotted path `group` is read: a
        chain names it, or a call renders the group and the group prints the field."""
        if f"{group}.{name}" in self.chains:
            return True
        if group not in self.rendered:
            return False
        flags = getattr(model, "FLAGS", ())
        return name in {flag.field for flag in flags} or name == "extra_args"


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
        #: the chains below the settings (`Reads.chains`) and the groups that code renders
        self.chains: set[str] = set()
        self.rendered: set[str] = set()
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
        if name == "render_flags" and node.args:
            chain = self._chain(node.args[0])
            if chain:
                self.rendered.add(chain)
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
            chain = self._chain(node)
            if chain:
                self.chains.add(chain)
        self.generic_visit(node)

    def _chain(self, node: ast.AST) -> str:
        """The dotted path below the settings that `node` names (`ss.p.q` gives `p.q`), or the
        empty text when `node` is no chain of attributes on an expression that holds them."""
        text = ast.unparse(node)
        roots = SETTINGS_EXPRESSIONS | SETTINGS_SELF if self.settings_code else SETTINGS_EXPRESSIONS
        for root in sorted(roots, key=len, reverse=True):
            rest = text.removeprefix(root + ".")
            if rest != text and re.fullmatch(r"\w+(\.\w+)*", rest):
                return rest
        return ""

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


def _uncommented(name: str, text: str) -> str:
    """The text of a template without its Jinja comments and, in a template of a language that
    has them, its line comments."""
    text = re.sub(r"{#.*?#}", "", text, flags=re.DOTALL)
    if any(name.endswith(suffix) for suffix in HASH_COMMENT_SUFFIXES):
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return text


def template_group_reads(templates: Iterable[tuple[str, str]]) -> tuple[set[str], set[str]]:
    """What the templates of one flow read below a settings group: the dotted paths of the chains
    (`settings.p.q.leaf` gives `p`, `p.q` and `p.q.leaf`) and of the groups that a `render_flags`
    call renders (`render_flags(settings.p.q, ...)` gives `p.q`).

    A `{% set name = settings.path %}` makes `name.rest` and `render_flags(name` stand for
    `settings.path.rest` from there to the end of the template that sets it, and in the templates
    that one includes (`{% include "name" %}`, and `{% include step + "tail" %}` for the
    templates that end in `tail`), with the names as they are at the include. Another template does not see the name, and a template that
    sets the name again gives it the new path from there on. A name that stands for another name
    resolves too. Each template is read once alone, and once for every template that includes it,
    with the names of that template."""
    texts = {name: _uncommented(name, text) for name, text in templates}
    dotted = r"\w+(?:\s*\.\s*\w+)*"
    assignment = re.compile(rf"{{%-?\s*set\s+(\w+)\s*=\s*({dotted})\s*-?%}}")
    literal = re.compile(r"""{%-?\s*include\s+['"]([^'"]+)['"](?!\s*[~+])""")
    computed = re.compile(r"""{%-?\s*include\s+\w+\s*\+\s*['"]([^'"]+)['"]""")
    tail = r"(?:\s*\.\s*\w+)*"
    chains: set[str] = set()
    rendered: set[str] = set()

    def path_of(text: str) -> list[str]:
        return [part.strip() for part in text.split(".")]

    def resolve(roots: dict[str, str], root: str, rest: list[str]) -> str:
        return ".".join([*filter(None, [roots[root]]), *rest])

    def read(chunk: str, roots: dict[str, str]) -> None:
        names = "|".join(re.escape(root) for root in sorted(roots, key=len, reverse=True))
        chain = re.compile(rf"(?<![\w.])({names})({tail})(?!\w)")
        render = re.compile(rf"\brender_flags\s*\(\s*({names})({tail})(?!\w)")
        for root, rest in chain.findall(chunk):
            full = resolve(roots, root, path_of(rest.strip(". ")) if rest.strip() else [])
            full_path = full.split(".")
            chains.update(
                ".".join(full_path[: n + 1]) for n in range(len(full_path)) if full_path[0]
            )
        for root, rest in render.findall(chunk):
            group = resolve(roots, root, path_of(rest.strip(". ")) if rest.strip() else [])
            if group:
                rendered.add(group)

    events = re.compile(f"{assignment.pattern}|{literal.pattern}|{computed.pattern}")

    def scan(name: str, inherited: dict[str, str], above: tuple[str, ...]) -> None:
        text = texts[name]
        roots = dict(inherited)
        position = 0
        for match in events.finditer(text):
            read(text[position : match.start()], roots)
            position = match.end()
            alias, value, target, ending = match.groups()
            if alias is not None:
                if alias in SETTINGS_EXPRESSIONS:
                    continue
                first, *rest = path_of(value)
                # `self.settings` is two words: the longest root that starts the value wins
                for root in sorted(roots, key=len, reverse=True):
                    words = root.split(".")
                    if [first, *rest][: len(words)] == words:
                        roots[alias] = resolve(roots, root, [*rest][len(words) - 1 :])
                        break
                else:
                    roots.pop(alias, None)  # the name stands for something else from here on
                continue
            # an include reads its template with the names as they are here; one that loops
            # back is no new text to read
            found = [target] if target is not None else [n for n in texts if n.endswith(ending)]
            for included in found:
                if included in texts and included not in above:
                    scan(included, roots, (*above, name))
        read(text[position:], roots)

    base = {expression: "" for expression in SETTINGS_EXPRESSIONS}
    for name in texts:
        scan(name, base, (name,))
    return chains, rendered


def template_reads(name: str, text: str) -> set[str]:
    """The settings a template reads: `settings.x` and `settings["x"]`, outside a Jinja comment
    and, in a template of a language that has them, a line comment."""
    text = _uncommented(name, text)
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
    chains: set[str] = set()
    rendered: set[str] = set()
    names: set[str] = set(entries)
    code: list[ast.AST] = []
    functions: set[tuple[str, str]] = set()
    taken: set[tuple[str, str]] = set()
    templates = list(templates)
    used = frozenset().union(*(template_reads(name, text) for name, text in templates))
    chains, rendered = template_group_reads(templates)
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
            chains |= collector.chains
            rendered |= collector.rendered
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
            return Reads(frozenset(attributes), used, frozenset(chains), frozenset(rendered)), code


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


def _group_class(annotation: object) -> type[SettingsGroup] | None:
    if isinstance(annotation, type) and issubclass(annotation, SettingsGroup):
        return annotation
    return None


def unread_in(settings: type[XedaBaseModel], reads: Reads) -> list[str]:
    """The settings in the model `settings` that `reads` does not cover, in declaration order. A
    setting is a field of the model that holds no group, or a field of a group at any depth, by
    its dotted path (`global_placement.timing_driven.enabled`). A field that holds a group is not
    a setting: it is read when every setting in it is."""
    unread: list[str] = []

    def walk(model: type[XedaBaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            group = _group_class(info.annotation)
            if group is not None:
                walk(group, f"{prefix}{name}.")
            elif not prefix and not reads.reads(name):
                unread.append(name)
            elif prefix and not reads.reads_leaf(prefix[:-1], model, name):
                unread.append(f"{prefix}{name}")

    walk(settings, "")
    return unread


def unread_settings(cls: type[Flow]) -> list[str]:
    """The settings of `cls` that nothing reads, in declaration order."""
    return unread_in(cls.Settings, flow_reads(cls))


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


# ------------------------------------------------------------------------- settings groups


class _Q(SettingsGroup):
    """The group `p.q` of the toy settings. Its template calls `render_flags` on it."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("rendered", "-rendered", "value"),)
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-free"})

    rendered: Optional[int] = None
    unflagged: Optional[int] = None
    by_chain_template: Optional[int] = None
    by_chain_code: Optional[int] = None
    past_leaf: list[str] = []
    by_alias: Optional[int] = None
    in_docstring: Optional[int] = None
    in_field: Optional[int] = None
    in_validator: Optional[int] = None
    in_jinja_comment: Optional[int] = None
    in_tcl_comment: Optional[int] = None
    extra_args: list[str] = []


class _U(SettingsGroup):
    """The group `p.u`, which a template renders through an alias of `p`."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("u_flag", "-u_flag", "value"),)

    u_flag: Optional[int] = None


class _P(SettingsGroup):
    """A group of the toy settings that holds two groups."""

    q: _Q = Field(default_factory=_Q)
    u: _U = Field(default_factory=_U)


class _T(SettingsGroup):
    """The group `s.t`. A call on `s` does not render it."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("t_flag", "-t_flag", "value"),)

    t_flag: Optional[int] = None


class _S(SettingsGroup):
    """A group with a flag and a sub-group; the template renders `s` only."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("s_flag", "-s_flag", "value"),)

    s_flag: Optional[int] = None
    t: _T = Field(default_factory=_T)


class _N(SettingsGroup):
    """A group that no template renders."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("n_flag", "-n_flag", "value"),)

    n_flag: Optional[int] = None


class _C(SettingsGroup):
    """A group that the code renders."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("c_flag", "-c_flag", "value"),)

    c_flag: Optional[int] = None


class _A(SettingsGroup):
    """A group that a template renders through an alias."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("a_flag", "-a_flag", "value"),)

    a_flag: Optional[int] = None


class _O(SettingsGroup):
    """A group that a call renders, but not a group of the settings."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("o_flag", "-o_flag", "value"),)

    o_flag: Optional[int] = None


class _GroupSettings(XedaBaseModel):
    """The settings of the toy flow: a plain setting and groups."""

    plain: int = 0
    p: _P = Field(default_factory=_P)
    s: _S = Field(default_factory=_S)
    norender: _N = Field(default_factory=_N)
    code_rendered: _C = Field(default_factory=_C)
    alias_rendered: _A = Field(default_factory=_A)
    other: _O = Field(default_factory=_O)


GROUP_FIXTURE = '''
class ToyGroups(Flow):
    """Mentions settings.p.q.in_docstring."""

    declared = Field(0, default=settings.p.q.in_field)

    @field_validator("declared")
    @classmethod
    def check(cls, value):
        return settings.p.q.in_validator

    def run(self):
        ss = self.settings
        ss.plain
        self.settings.p.q.by_chain_code
        ss.p.q.past_leaf.append("x")
        render_flags(self.settings.code_rendered, quote)
        render_flags(self.tool.other, quote)
'''

GROUP_TEMPLATES = [
    (
        "script.tcl",
        """{% set gp = settings.p %}
{{ render_flags(settings.p.q, tcl_word) }}
{{ settings.p.q.by_chain_template }}
{{ gp.q.by_alias }}
{{ render_flags(gp.u, tcl_word) }}
{{ render_flags(settings.s, tcl_word) }}
{# settings.p.q.in_jinja_comment #}
# settings.p.q.in_tcl_comment
""",
    ),
    ("defs.tcl", "{% set ar = settings.alias_rendered %}{% include 'use.tcl' %}"),
    ("use.tcl", "{{ render_flags(ar, tcl_word) }}"),
]


def _group_reads(source: str = GROUP_FIXTURE, templates=GROUP_TEMPLATES) -> Reads:
    reads, _ = settings_reads(
        {"fixture": source},
        {"fixture": {"ToyGroups"}},
        {},
        templates,
        frozenset({"run"}),
    )
    return reads


GROUP_READ = [
    "plain",
    "p.q.rendered",
    "p.q.extra_args",
    "p.q.by_chain_template",
    "p.q.by_chain_code",
    "p.q.past_leaf",
    "p.q.by_alias",
    "p.u.u_flag",
    "s.s_flag",
    "code_rendered.c_flag",
    "alias_rendered.a_flag",
]
GROUP_NOT_READ = [
    "p.q.unflagged",
    "p.q.in_docstring",
    "p.q.in_field",
    "p.q.in_validator",
    "p.q.in_jinja_comment",
    "p.q.in_tcl_comment",
    "s.t.t_flag",
    "norender.n_flag",
    "other.o_flag",
]


def test_the_leaves_of_a_group_are_reported_by_dotted_path_and_the_group_is_not() -> None:
    unread = unread_in(_GroupSettings, _group_reads())
    assert unread == [
        "p.q.unflagged",
        "p.q.in_docstring",
        "p.q.in_field",
        "p.q.in_validator",
        "p.q.in_jinja_comment",
        "p.q.in_tcl_comment",
        "s.t.t_flag",
        "norender.n_flag",
        "other.o_flag",
    ]
    assert not {"p", "p.q", "s", "s.t", "norender", "code_rendered", "alias_rendered"} & set(unread)


@pytest.mark.parametrize("leaf", GROUP_READ)
def test_the_scan_sees_each_way_of_reading_a_leaf_of_a_group(leaf: str) -> None:
    """A leaf is read when a template or the code calls `render_flags` on exactly its group and
    the leaf is in `FLAGS` (or is `extra_args`), when a chain of attributes names it in the code
    or a template, or when a template alias stands for the start of the chain."""
    assert leaf in unread_in(_GroupSettings, Reads(frozenset(), frozenset())) + ["plain"]
    assert leaf not in unread_in(_GroupSettings, _group_reads())


@pytest.mark.parametrize("leaf", GROUP_NOT_READ)
def test_the_scan_does_not_count_what_is_no_read_of_a_leaf_of_a_group(leaf: str) -> None:
    assert leaf in unread_in(_GroupSettings, _group_reads())


class _G(SettingsGroup):
    """A group with a flag and a field that has none."""

    FLAGS: ClassVar[tuple[Flag, ...]] = (Flag("x", "-x", "value"),)

    x: Optional[int] = None
    y: Optional[int] = None


class _OneGroup(XedaBaseModel):
    g: _G = Field(default_factory=_G)


@pytest.mark.parametrize("expression", sorted(SETTINGS_EXPRESSIONS))
def test_a_render_call_on_any_expression_that_holds_the_settings_reads_the_flags(
    expression: str,
) -> None:
    source = (
        f"class ToyGroups(Flow):\n    def run(self):\n        render_flags({expression}.g, q)\n"
    )
    template = [("t.tcl", f"{{{{ render_flags({expression}.g, tcl_word) }}}}")]
    for reads in (_group_reads(source, ()), _group_reads("class ToyGroups(Flow): pass", template)):
        assert unread_in(_OneGroup, reads) == ["g.y"]


def test_a_chain_that_goes_past_a_leaf_in_a_template_reads_it() -> None:
    reads = _group_reads(
        "class ToyGroups(Flow): pass", [("t.tcl", "{{ settings.g.y.bit_length }}")]
    )
    assert unread_in(_OneGroup, reads) == ["g.x"]


def test_a_template_alias_of_an_alias_stands_for_the_whole_path() -> None:
    templates = [("t.tcl", "{% set a = settings.g %}{% set b = a %}{{ b.y }}")]
    reads = _group_reads("class ToyGroups(Flow): pass", templates)
    assert unread_in(_OneGroup, reads) == ["g.x"]


def test_two_templates_may_bind_the_same_alias_to_different_groups() -> None:
    """An alias belongs to the template that sets it. The scan ends, and each read counts for the
    group that its own template bound the alias to."""
    templates = [
        ("a.tcl", "{% set rt = settings.first %}{{ rt.x }}"),
        ("b.tcl", "{% set rt = settings.second.inner %}{{ render_flags(rt, q) }}{{ rt.y }}"),
    ]
    chains, rendered = template_group_reads(templates)
    assert chains == {
        "first",
        "first.x",
        "second",
        "second.inner",
        "second.inner.y",
    }
    assert rendered == {"second.inner"}


def test_an_alias_is_not_visible_in_a_template_that_the_setting_template_does_not_include() -> None:
    templates = [
        ("sets.tcl", "{% set gp = settings.g %}{{ gp.x }}"),
        ("other.tcl", "{{ gp.y }}"),
    ]
    chains, _ = template_group_reads(templates)
    assert chains == {"g", "g.x"}


def test_an_alias_is_visible_in_the_templates_that_the_setting_template_includes() -> None:
    templates = [
        ("driver.tcl", "{% set gp = settings.g %}{% include 'stages/one.tcl' %}"),
        ("stages/one.tcl", '{{ gp.x }}{% include "stages/two.tcl" %}'),
        ("stages/two.tcl", "{{ render_flags(gp.h, q) }}"),
    ]
    chains, rendered = template_group_reads(templates)
    assert chains == {"g", "g.x", "g.h"}
    assert rendered == {"g.h"}


def test_an_alias_set_again_in_a_template_stands_for_the_last_binding_from_there_on() -> None:
    templates = [
        ("t.tcl", "{% set a = settings.one %}{{ a.x }}{% set a = settings.two %}{{ a.y }}"),
    ]
    chains, _ = template_group_reads(templates)
    assert chains == {"one", "one.x", "two", "two.y"}


def test_an_alias_set_in_an_included_template_does_not_reach_the_including_template() -> None:
    templates = [
        ("driver.tcl", "{% include 'stage.tcl' %}{{ gp.z }}"),
        ("stage.tcl", "{% set gp = settings.g %}{{ gp.x }}"),
    ]
    chains, _ = template_group_reads(templates)
    assert chains == {"g", "g.x"}


def test_an_included_template_sees_the_names_as_they_are_at_the_include() -> None:
    """A name that the including template sets again after the include does not change what the
    included template reads."""
    templates = [
        (
            "driver.tcl",
            "{% set gp = settings.first %}{% include 'child.tcl' %}"
            "{% set gp = settings.second %}{{ gp.y }}",
        ),
        ("child.tcl", "{{ gp.x }}"),
    ]
    chains, _ = template_group_reads(templates)
    assert chains == {"first", "first.x", "second", "second.y"}


def test_a_group_added_to_a_real_flow_is_reported_by_its_leaves() -> None:
    from xeda.flows import Yosys

    with preserved_flow_registry():

        class _Extended(Yosys):
            """A flow with a group that nothing reads."""

            class Settings(Yosys.Settings):
                grouped: _G = Field(default_factory=_G, description="A group.")

        assert [name for name in unread_settings(_Extended) if "grouped" in name] == [
            "grouped.x",
            "grouped.y",
        ]
