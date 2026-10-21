"""The digests of the code that a run depends on: Xeda's own and that of a plugin flow.

A trace holds both digests. A change to either one makes a run stale (`trace.check_trace`).

- `xeda_code_digest` covers every file of the installed `xeda` package: code, templates and
  bundled data. An editable install keeps one version string across edits, so this digest, and
  not the version, notices a change to a helper that a flow uses.
- `flow_code_digest` covers the code of a flow that is not part of Xeda (a plugin flow). It
  holds every Flow class of the MRO of the flow class that Xeda does not define.

A digest is a function of the content of files on disk, and of nothing else: not of the modules
that a process has imported, and not of where the files lie. A name in a digest is relative to
the root of its code, so a moved tree has the same digest.

Nothing here is cached. Every call reads the files, so an edit counts at once, also in a process
that lives long (a notebook). A launch computes each digest once and keeps it for its length
(`CodeDigests`).

The root of the code of a Flow class that is not part of Xeda is:

- The top-level package, for a class defined in a package: the directory with the `__init__.py`
  files from the module up. Every file under it counts (a helper module, a template, data).
- The module and the modules that it imports, for a class defined in a module that is not in a
  package. Its directory is not a root: it can be a design directory or a working directory with
  any number of unrelated files, and an edit of one of them must not make a run stale. The
  modules that count are those in the directory of the module that the module imports (the
  `import` statements in its source, found without running it), and so on for each module found.
  A package of the directory counts as a whole. The `templates` directory next to the module
  counts too.

An import that the source does not show (`importlib.import_module(name)`) is not found. Put such
a module in a package.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable
from pathlib import Path
from typing import Type

from ..digest import digest_files, digest_named, package_files
from ..flow import Flow, FlowFatalError

#: The directory of the installed xeda package.
XEDA_PACKAGE = Path(__file__).resolve().parent.parent


def xeda_code_digest() -> str:
    """Return a digest of every file of the installed xeda package.

    That covers code, templates and bundled data. It reads the files on every call."""
    return digest_files(package_files(XEDA_PACKAGE), XEDA_PACKAGE)


def _source_file(cls: type) -> Path:
    """Return the file that defines `cls`.

    Raise `FlowFatalError` if there is none on disk: a class defined in a notebook cell, in the
    interactive interpreter or with `exec`, or one that comes from an archive."""
    name = f"{cls.__module__}.{cls.__qualname__}"
    try:
        source = inspect.getsourcefile(cls)
    except (OSError, TypeError):
        source = None
    if source is None:
        why = f"Python knows no file for its module {cls.__module__}"
    elif Path(source).is_file():
        return Path(source).resolve()
    else:
        why = f"{source} is not a file"
    raise FlowFatalError(
        f"The flow class {name} has no source file on disk ({why}), so Xeda cannot tell when its "
        f"code changes and cannot reuse its runs safely. Define the flow class in a module file "
        f"(.py), and import it from there."
    )


def plugin_classes(flow_class: Type[Flow]) -> list[type]:
    """Return the classes of the MRO of `flow_class` that are Flow classes outside Xeda.

    Raise `FlowFatalError` if the code of one of them cannot be found (`_source_file`)."""
    return [
        cls
        for cls in flow_class.__mro__
        if isinstance(cls, type)
        and issubclass(cls, Flow)
        and not _source_file(cls).is_relative_to(XEDA_PACKAGE)
    ]


def check_flow_code(flow_class: Type[Flow]) -> None:
    """Refuse a flow class whose code cannot be found on disk, naming the class and the reason.

    This is pure. The planning calls it, so the refusal comes before any run."""
    plugin_classes(flow_class)


def _package_root(module_file: Path) -> Path | None:
    """Return the top-level package directory of `module_file`, or None if it is in none."""
    directory = module_file.parent
    if not (directory / "__init__.py").is_file():
        return None
    while (directory.parent / "__init__.py").is_file():
        directory = directory.parent
    return directory


def _imported_modules(source: Path) -> set[str]:
    """Return the top-level names that the `import` statements of the module `source` name.

    A relative import names none: in a module that is not in a package it cannot occur, and
    in a package it stays inside the package. A directory without an `__init__.py` is no package
    here, so a relative import in it is not followed. A module that does not parse names none."""
    try:
        tree = ast.parse(source.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


def _tree(name: str, directory: Path) -> list[tuple[str, Path]]:
    """Return the `(name, file)` pairs of `package_files(directory)`, named under `name`.

    The names do not depend on where the directory is, also when it is a link to another place."""
    top = directory.resolve()
    return [(f"{name}/{file.relative_to(top).as_posix()}", file) for file in package_files(top)]


def _module_files(module_file: Path) -> list[tuple[str, Path]]:
    """Return the `(name, file)` pairs of a module outside a package: it, what it imports from
    its directory (and so on, for each module found), and its `templates` directory."""
    root = module_file.parent
    files = {module_file.name: module_file}
    pending = [module_file]
    while pending:
        for name in sorted(_imported_modules(pending.pop())):
            package, module = root / name, root / f"{name}.py"
            if (package / "__init__.py").is_file():
                found = _tree(name, package)
            elif module.is_file():
                found = [(module.name, module)]
            else:
                continue
            for found_name, file in found:
                if found_name not in files:
                    files[found_name] = file
                    if file.suffix == ".py":
                        pending.append(file)
    if (root / "templates").is_dir():
        files.update(_tree("templates", root / "templates"))
    return sorted(files.items())


def _named_files(cls: type) -> Iterable[tuple[str, Path]]:
    """Return the `(name, file)` pairs of the code of `cls`, a class outside Xeda.

    A name is relative to the root of the code, and for a package it starts with the name of the
    package."""
    module_file = _source_file(cls)
    package = _package_root(module_file)
    if package is not None:
        return _tree(package.name, package)
    return _module_files(module_file)


def flow_code_digest(flow_class: Type[Flow]) -> str:
    """Return a digest of the code of `flow_class` outside Xeda.

    The code of a class is found as the module docstring says. The digest is "" when the xeda
    package defines all the Flow classes of the MRO: `xeda_code_digest` covers that case. It
    raises `FlowFatalError` if the code of a class cannot be found (`check_flow_code`)."""
    entries = [pair for cls in plugin_classes(flow_class) for pair in _named_files(cls)]
    return digest_named(entries) if entries else ""


class CodeDigests:
    """The code digests of one launch.

    A launch needs the same digests for each of its flows. This computes each of them at its first
    use and keeps it, so a launch reads the files of a tree once. A new launch makes a new object,
    so an edit between two launches always counts."""

    def __init__(self) -> None:
        self._xeda: str | None = None
        self._flows: dict[type, str] = {}

    def xeda(self) -> str:
        """Return `xeda_code_digest()`, computed once."""
        if self._xeda is None:
            self._xeda = xeda_code_digest()
        return self._xeda

    def flow(self, flow_class: Type[Flow]) -> str:
        """Return `flow_code_digest(flow_class)`, computed once."""
        if flow_class not in self._flows:
            self._flows[flow_class] = flow_code_digest(flow_class)
        return self._flows[flow_class]
