"""A name the suite patches on a module is a name something looks up through that module.

`monkeypatch.setattr(some_module, "helper", fake)` rebinds `helper` *in that module's
namespace*. It changes what runs only if the call site looks the name up there at call time --
either a function defined in that module calls the bare name, or another module reached it as
`some_module.helper`. Neither holds after someone does `from some_module import helper`
elsewhere and calls the imported name, or moves `helper` to another module and leaves a
re-export behind: the patch then sets an attribute nobody reads, the test still passes, and it
no longer tests anything. That failure is silent, and a silently weakened test is worse than a
red one.

So this is the oracle for refactorings that move code between modules -- written before the one
that needs it, the `flow_runner/default_runner.py` split by launch stage, and useful to every
later move. It collects every `(module, name)` the suite replaces -- `monkeypatch.setattr`, a
plain module-attribute assignment, and both of those inside the Python scripts some tests embed
as string literals and run in a child process -- and requires, for each, that something looks
the name up through the module.

An identity check (`getattr(module, name) is other_module.name`) would prove nothing: a
re-export *is* the same object. Only "the lookup goes through this module" tells a live patch
target from a dead one.

Three checks, because source reachability alone is not proof:

- `test_a_patched_module_name_is_looked_up_through_that_module` -- the general class, over every
  target the scan finds. The lookup may be anywhere: a function of the module itself, another
  xeda module, or a flow or helper the suite defines (`test_prepare_inputs.py`'s
  `_ChipdbTaker.prepare_inputs` calls `xilinx.prepare_chipdb(...)` through the module, so
  patching it there is live even though `nextpnr.py` calls the name it imported).
- `test_the_launcher_stage_names_are_looked_up_in_the_product` -- for the names the stage split
  moves, the lookup must be in `src/xeda`: a patch meant to intercept the launcher's own
  behavior is dead the moment only test code reads it there, however green the suite looks.
- `test_patching_a_launcher_stage_name_intercepts_a_real_launch` (and its failing-run twin) --
  the behavioral check, and the only one that is a *proof*. It replaces each of those names on
  the module with a recorder that delegates, runs a real launch, and requires every one to have
  been observed. **A re-export that is still callable from `default_runner`, and still mentioned
  by some function there, but no longer on the live path -- because the moved stage module calls
  its own copy -- passes the reachability check and fails this one.** That is the hazard the
  split actually poses, so the reachability checks explain a failure while this one decides it.

**What the scan cannot see** (each is a blind spot, not a bug, and the oracle makes no claim
about them):

- a patch whose target or attribute name is not a literal -- `setattr(mod, name, value)` with a
  variable, or a dotted string assembled at run time;
- `unittest.mock.patch` / `patch.object`, which this suite does not use;
- a patch applied indirectly, by a fixture or helper that receives the module and name as
  arguments;
- a patch on an *object's* attribute rather than a module's (`monkeypatch.setattr(runner.console,
  "print", ...)`), which is out of scope by design;
- for every target but the launcher's, only reachability is checked: a name whose call path no
  launch here exercises could still be a dead re-export. Extend the behavioral check to a name
  when that matters for it.

Nothing is listed by hand: `_collect()` re-parses every test file on each run, so a sixth patched
name appears in the sweep with no edit here. The one literal is `LAUNCHER_TARGETS`, the subset
under the stricter rules, and `test_the_scan_finds_the_suites_patch_targets` fails if the scan
stops finding any of them.

A name that genuinely only has to exist on the module, with nothing looking it up there, goes in
`LOOKED_UP_ELSEWHERE` with the reason. It is empty, and should stay so.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest

TESTS = Path(__file__).parent
SOURCE = Path(inspect.getsourcefile(importlib.import_module("xeda")) or "").parent

#: (module, name) pairs the suite replaces although nothing looks the name up through that
#: module, each with why that is right. A patch of such a name reaches nothing, so an entry here
#: needs a reason that is not "the test passes".
LOOKED_UP_ELSEWHERE: dict[tuple[str, str], str] = {}


class PatchTarget(NamedTuple):
    """One `(module, name)` the suite replaces, and the first place it does it."""

    module: str
    name: str
    where: str

    def __str__(self) -> str:
        return f"{self.module}.{self.name} (patched at {self.where})"


@cache
def _is_module(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError):
        return False


@cache
def _sources(root: Path) -> tuple[tuple[Path, str], ...]:
    """Every Python file under `root`, with its text, read once for the whole module."""
    return tuple(
        (path, path.read_text())
        for path in sorted(root.rglob("*.py"))
        if path.name != Path(__file__).name
    )


def _package_of(path: Path) -> str:
    """The package a file under the installed `xeda` belongs to, for its relative imports."""
    parts = path.relative_to(SOURCE).parts[:-1]
    return ".".join(("xeda", *parts))


def _module_names(tree: ast.AST, package: str | None = None) -> dict[str, str]:
    """Local name -> dotted xeda module, for every xeda module this source imports.

    Covers `import xeda.flow_runner.default_runner as runner`, `import xeda.tool` (which binds
    `xeda`), `from xeda.flow_runner import default_runner` and, with `package`, xeda's own
    relative form `from .flow_runner import remote as remote_runner` -- the `from` cases only
    when the imported name really is a module, so a class or function imported the same way is
    not mistaken for one.
    """
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("xeda"):
                    continue
                if alias.asname:
                    names[alias.asname] = alias.name
                else:  # `import xeda.tool` binds `xeda`
                    names[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level and package:  # `from .flow_runner import remote`
                base = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                prefix = f"{base}.{node.module}" if node.module else base
            elif not node.level and (node.module or "").startswith("xeda"):
                prefix = node.module or ""
            else:
                continue
            for alias in node.names:
                dotted = f"{prefix}.{alias.name}"
                if dotted.startswith("xeda") and _is_module(dotted):
                    names[alias.asname or alias.name] = dotted
    return names


def _split_module(dotted: str) -> tuple[str, str] | None:
    """Split `"xeda.a.b.name"` into its longest module prefix and the next component."""
    parts = dotted.split(".")
    for end in range(len(parts) - 1, 0, -1):
        if _is_module(".".join(parts[:end])):
            return ".".join(parts[:end]), parts[end]
    return None


def _patched(source: str, where: str) -> Iterator[PatchTarget]:
    """Every xeda module attribute `source` replaces, itself and in its embedded scripts."""
    tree = _parsed(source)
    if tree is None:
        return
    modules = dict(_aliases(source, None))
    for node in ast.walk(tree):
        # a script a test writes out and runs in a child process patches the same way
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "import" in node.value and "xeda" in node.value:
                yield from _patched(node.value, f"{where}:{node.lineno} (embedded script)")
            continue
        module = name = None
        line = 0  # taken from the matched node, which always carries one
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "setattr" and len(node.args) >= 2:
                first, second = node.args[0], node.args[1]
                line = node.lineno
                if isinstance(first, ast.Name) and isinstance(second, ast.Constant):
                    module, name = modules.get(first.id), second.value
                elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                    split = _split_module(first.value)
                    if split is not None:
                        module, name = split
        elif isinstance(node, ast.Assign):
            for assigned in node.targets:
                if isinstance(assigned, ast.Attribute) and isinstance(assigned.value, ast.Name):
                    module, name, line = modules.get(assigned.value.id), assigned.attr, node.lineno
        if module and isinstance(name, str) and module.startswith("xeda"):
            yield PatchTarget(module, name, f"{where}:{line}")


def _collect() -> list[PatchTarget]:
    found: dict[tuple[str, str], PatchTarget] = {}
    for path, source in _sources(TESTS):  # this module is skipped: it names the patterns
        for target in _patched(source, str(path.relative_to(TESTS.parent))):
            found.setdefault((target.module, target.name), target)
    return sorted(found.values())


_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _reads_its_own_global(module: str, name: str) -> bool:
    """A function defined in `module` reads `name` from the module's own namespace.

    A name used only at import time (a decorator, a module-level expression) does not count:
    patching it afterwards would change nothing.
    """
    tree = _parsed(inspect.getsource(importlib.import_module(module)))
    assert tree is not None
    for scope in (node for node in ast.walk(tree) if isinstance(node, _SCOPES)):
        for inner in ast.walk(scope):
            if isinstance(inner, ast.Name) and inner.id == name:
                if isinstance(inner.ctx, ast.Load):
                    return True
    return False


@cache
def _parsed(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not in this package
        return None


@cache
def _aliases(source: str, package: str | None) -> tuple[tuple[str, str], ...]:
    tree = _parsed(source)
    return () if tree is None else tuple(sorted(_module_names(tree, package).items()))


def _reads_module_attribute(
    source: str, module: str, name: str, where: str, package: str | None, *, top: bool
) -> str | None:
    """Where `source` reads `name` as an attribute of the `module` object, or None."""
    tree = _parsed(source)
    if tree is None:
        return None
    local = {alias for alias, dotted in _aliases(source, package) if dotted == module}
    # the code xeda ships as a string and runs in another interpreter (`REMOTE_PROBE`,
    # `remote_runner`) looks names up there just as ordinary code does
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "import" in node.value and name in node.value:
                found = _reads_module_attribute(
                    node.value,
                    module,
                    name,
                    f"{where}:{node.lineno} (shipped script)",
                    None,
                    top=True,
                )
                if found is not None:
                    return found
    if not local:
        return None
    # at module scope too for a shipped script, which has no enclosing function
    scopes = [tree] if top else [n for n in ast.walk(tree) if isinstance(n, _SCOPES)]
    for scope in scopes:
        for inner in ast.walk(scope):
            via = None
            line = 0  # from the matched expression, which always carries one
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == name
                and isinstance(inner.value, ast.Name)
                and inner.value.id in local
            ):
                via, line = f"{inner.value.id}.{name}", inner.lineno
            elif (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "getattr"
                and len(inner.args) >= 2
                and isinstance(inner.args[0], ast.Name)
                and inner.args[0].id in local
                and isinstance(inner.args[1], ast.Constant)
                and inner.args[1].value == name
            ):
                via, line = f'getattr({inner.args[0].id}, "{name}")', inner.lineno
            if via is not None:
                return f"{where}:{line} reads {via}"
    return None


def _read_in_the_product(module: str, name: str) -> str | None:
    """Where `src/xeda` reads `name` as an attribute of the `module` object, or None.

    `remote_runner.RemoteRunner(...)` in `cli.py`, and `xeda.__version__` and
    `getattr(xeda, "REMOTE_PROTOCOL_VERSION")` inside `remote.py`'s `REMOTE_PROBE` -- code xeda
    ships as a string and runs in the remote interpreter -- are live patch targets even though
    the owning module never reads the name itself: the lookup happens at run time, on the module
    object the patch changed.
    """
    for path, source in _sources(SOURCE):
        found = _reads_module_attribute(
            source,
            module,
            name,
            f"src/xeda/{path.relative_to(SOURCE)}",
            _package_of(path),
            top=False,
        )
        if found is not None:
            return found
    return None


def _read_in_the_suite(module: str, name: str) -> str | None:
    """Where the suite's own code reads `name` as an attribute of the `module` object.

    A flow or helper a test defines is code too: `_ChipdbTaker.prepare_inputs` calls
    `xilinx.prepare_chipdb(...)` through the module, so patching it there is a live target.
    """
    for path, source in _sources(TESTS):
        found = _reads_module_attribute(
            source, module, name, str(path.relative_to(TESTS.parent)), None, top=False
        )
        if found is not None:
            return found
    return None


TARGETS = _collect()
IDS = [f"{t.module}.{t.name}" for t in TARGETS]

#: The launcher's own patch targets: what the stage split (PC-7) must not strand.
LAUNCHER_TARGETS = {
    "expectation",
    "snapshot_inputs",
    "scrub_runs",
    "_drop_unwritten_artifacts",
    "write_trace",
}


def test_the_scan_finds_the_suites_patch_targets():
    """The scan works. An empty or tiny scan would make every case below vacuous."""
    assert len(TARGETS) > 30, f"only {len(TARGETS)} patch targets found; the scan is broken"
    launcher = {t.name for t in TARGETS if t.module == "xeda.flow_runner.default_runner"}
    assert LAUNCHER_TARGETS <= launcher, (
        "the launcher's patch targets are what the stage split must not strand; missing: "
        f"{sorted(LAUNCHER_TARGETS - launcher)}"
    )


def test_the_scan_reads_patches_inside_embedded_scripts():
    """Some tests run a child process from a script in a string literal and patch there.

    `scrub_runs` is only ever patched that way (`test_read_locks.py`), so missing those would
    silently drop a target the split has to keep live.
    """
    embedded = [t for t in TARGETS if "embedded script" in t.where]
    assert embedded, "no patch inside an embedded script was found"
    assert any(t.name == "scrub_runs" for t in embedded)


@cache
def _lookups(module: str, name: str) -> tuple[bool, str | None, str | None]:
    """Whether the module reads its own global, and where the product and the suite read it."""
    return (
        _reads_its_own_global(module, name),
        _read_in_the_product(module, name),
        _read_in_the_suite(module, name),
    )


@pytest.mark.parametrize("target", TARGETS, ids=IDS)
def test_a_patched_module_name_is_looked_up_through_that_module(target: PatchTarget):
    """Patching it reaches code that runs -- the product's, or the suite's own."""
    module = importlib.import_module(target.module)
    assert hasattr(module, target.name), f"{target} names nothing on the module"
    own, product, suite = _lookups(target.module, target.name)
    reason = LOOKED_UP_ELSEWHERE.get((target.module, target.name))
    if reason is not None:
        assert not own and product is None and suite is None, (
            f"{target} is in LOOKED_UP_ELSEWHERE, but the lookup does go through "
            f"{target.module}: drop the entry"
        )
        return
    assert own or product or suite, (
        f"{target} is replaced, but nothing looks `{target.name}` up through "
        f"{target.module}: no function defined there reads it, and neither the product nor the "
        f"suite reads it as an attribute of that module. Either it was imported with "
        f"`from ... import {target.name}` at every call site, or it was moved to another module "
        f"and re-exported -- both leave the patch setting an attribute nobody reads. Patch it on "
        f"the module whose code looks it up, or record it in LOOKED_UP_ELSEWHERE with the reason."
    )


@pytest.mark.parametrize("name", sorted(LAUNCHER_TARGETS), ids=sorted(LAUNCHER_TARGETS))
def test_the_launcher_stage_names_are_looked_up_in_the_product(name: str):
    """The names the stage split (PC-7) moves are patched to intercept the *launcher*.

    So for these the lookup must be in `src/xeda`: once only test code reads the name there,
    the patch no longer reaches a single line of the launcher, and the tests that rely on it
    pass for the wrong reason. This is the case that goes red when a stage body moves out of
    `default_runner.py` and leaves a re-export behind.

    This is a *reachability* check, not a proof: see
    `test_patching_a_launcher_stage_name_intercepts_a_real_launch` for the behavioral one, and
    the module docstring for what neither covers.
    """
    module = "xeda.flow_runner.default_runner"
    own, product, _ = _lookups(module, name)
    assert own or product, (
        f"{module}.{name} is patched to intercept the launcher, but no code in src/xeda looks "
        f"it up through that module any more: it was moved and re-exported. Patch it where it "
        f"is now called, in the stage module that calls it."
    )


#: The launcher names a plain successful launch must go through, and `scrub_runs`, which needs
#: `scrub_old_runs` with the hashed layout. `_drop_unwritten_artifacts` runs only on a failure,
#: so it has its own case below.
ON_A_SUCCESSFUL_LAUNCH = sorted(LAUNCHER_TARGETS - {"_drop_unwritten_artifacts"})


def _intercepting(monkeypatch, names):
    """Wrap each `name` on the launcher module with a recorder that still delegates."""
    from xeda.flow_runner import default_runner

    seen: set[str] = set()
    for name in names:
        original = getattr(default_runner, name)

        def recorder(*args, _name=name, _original=original, **kwargs):
            seen.add(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(default_runner, name, recorder)
    return seen


def test_patching_a_launcher_stage_name_intercepts_a_real_launch(tmp_path, monkeypatch):
    """Patching the name on `default_runner` is observed by a launch that actually happens.

    The reachability check above reads the source; this one runs the launcher. Each name is
    replaced on the module with a recorder that delegates to the original, a real launch is
    performed, and every name must have been observed. A re-export whose live call path went
    elsewhere records nothing and fails here, whatever the source still says.
    """
    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    from .io_flows import _Maker

    seen = _intercepting(monkeypatch, ON_A_SUCCESSFUL_LAUNCH)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = DefaultRunner(
        tmp_path / "run", display_results=False, hashed_run_dirs=True, scrub_old_runs=True
    )
    assert runner.launch_flow(_Maker, design, {}).succeeded
    missing = sorted(set(ON_A_SUCCESSFUL_LAUNCH) - seen)
    assert not missing, (
        "a launch did not go through default_runner."
        + ", default_runner.".join(missing)
        + ": those names are patched to intercept the launcher, and the patch reached nothing"
    )


def test_patching_the_failed_run_artifact_drop_intercepts_a_failing_launch(tmp_path, monkeypatch):
    """The same, for the one name only a failing run reaches."""
    import pytest as _pytest

    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    from .io_flows import _Maker

    class _Failing(_Maker):
        """A flow whose run raises, so the launcher drops what it did not write."""

        def run(self) -> None:
            raise RuntimeError("as the oracle asked")

    seen = _intercepting(monkeypatch, ["_drop_unwritten_artifacts"])
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with _pytest.raises(RuntimeError, match="as the oracle asked"):
        runner.launch_flow(_Failing, design, {})
    assert seen == {"_drop_unwritten_artifacts"}, (
        "a failing launch did not go through default_runner._drop_unwritten_artifacts: it is "
        "patched to intercept the launcher, and the patch reached nothing"
    )
