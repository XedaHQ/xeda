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

A call lists the files and checks each of them, so an edit counts at once, also in a process that
lives long (a notebook), and so does an added or a removed file. A check is by the metadata of a
file, as the trace checks its inputs: the process keeps a record of each code file, and reads the
file again only when the record cannot vouch for it (`_content_digest`). A relaunch of unchanged
code then reads no file. A launch computes each digest once and keeps it for its length
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
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Type

from ..digest import FileRecord, content_pairs, digest_pairs, package_files, record_file
from ..flow import Flow, FlowFatalError

#: The directory of the installed xeda package.
XEDA_PACKAGE = Path(__file__).resolve().parent.parent


#: The most files whose records the process keeps. It holds the code of a few trees; a process
#: that meets more, like a test suite, starts the store afresh.
RECORD_LIMIT = 50_000


@lru_cache(maxsize=None)
def _records() -> dict[Path, tuple[FileRecord, int]]:
    """Return the store of the records of the code files that this process has read.

    Each entry is the record of a file and the time (`time.time_ns()`) at which it was taken. The
    store only saves reading. It never decides what a digest is: `_content_digest` checks the
    metadata of a file at each use."""
    return {}


def _content_digest(path: Path) -> str:
    """Return the content digest of the code file `path`, reading it only if it may have changed.

    This is the rule of the trace (`digest.record_file`): the stored record is used when the
    size, the modification time, the inode change time and the inode of the file are the same,
    and the file had last changed well before the record was taken (outside the racy window,
    `FileRecord.trusted`). Otherwise the file is read and a new record is stored. Every call
    checks the file, so an edit counts at once, and a same-size edit with the old time put back
    changes the inode change time.

    The time of a record is the clock of this machine. The trace uses the clock of the file system
    of the run directory, which needs a marker file written there, and a launch writes nothing in
    a code tree. So a code tree on a network file system whose clock is more than the racy window
    behind this machine's is not covered against two edits of one file, of the same size, within
    one timestamp tick."""
    records = _records()
    taken_ns = time.time_ns()
    held = records.get(path)
    record = record_file(
        path,
        None if held is None else held[0],
        None if held is None else held[1],
    )
    if held is None or record is not held[0]:
        if len(records) >= RECORD_LIMIT:
            records.clear()
        records[path] = (record, taken_ns)
    return record.sha


def xeda_code_digest() -> str:
    """Return a digest of every file of the installed xeda package.

    That covers code, templates and bundled data. It lists the files and checks each of them on
    every call (`_content_digest`)."""
    entries = [
        (file.relative_to(XEDA_PACKAGE).as_posix(), file) for file in package_files(XEDA_PACKAGE)
    ]
    return digest_pairs(content_pairs(entries, _content_digest))


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
    """Return the names of the modules that the `import` statements of `source` name.

    These are the first parts of the names of absolute imports. A relative import of one dot
    (`from . import helper`, `from .helper import x`) names the module in the directory of
    `source`: it occurs in a namespace package, a directory without an `__init__.py`. Other
    relative imports name none. A module that does not parse names none."""
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
        elif isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                names.add(node.module.partition(".")[0])
            else:
                names.update(alias.name for alias in node.names)
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


def _code_root(cls: type) -> Path:
    """Return the root of the code of `cls`: its top-level package, or its module file."""
    module_file = _source_file(cls)
    return _package_root(module_file) or module_file


#: What was read of the code of a flow: the `(name, content digest)` pairs of each code root.
Snapshots = dict[Path, list[tuple[str, str]]]


def flow_code_digest(flow_class: Type[Flow], snapshots: Snapshots | None = None) -> str:
    """Return a digest of the code of `flow_class` outside Xeda.

    The code of a class is found as the module docstring says. The digest is "" when the xeda
    package defines all the Flow classes of the MRO: `xeda_code_digest` covers that case. It
    raises `FlowFatalError` if the code of a class cannot be found (`check_flow_code`).

    `snapshots` holds what was read of each code root before, and gets what this reads. Two
    classes of one tree then see the same files."""
    snapshots = {} if snapshots is None else snapshots
    pairs: list[tuple[str, str]] = []
    for cls in plugin_classes(flow_class):
        root = _code_root(cls)
        if root not in snapshots:
            snapshots[root] = content_pairs(_named_files(cls), _content_digest)
        pairs += snapshots[root]
    return digest_pairs(pairs) if pairs else ""


class CodeDigests:
    """The code digests of one launch.

    A launch needs the same digests for each of its flows. This reads each code root once, at its
    first use, and keeps what it read for the launch: all the flows of a tree get one snapshot of
    it, also when the tree changes during the launch. A new launch makes a new object, so an edit
    between two launches always counts."""

    def __init__(self) -> None:
        self._xeda: str | None = None
        self._snapshots: Snapshots = {}

    def xeda(self) -> str:
        """Return `xeda_code_digest()`, computed once."""
        if self._xeda is None:
            self._xeda = xeda_code_digest()
        return self._xeda

    def flow(self, flow_class: Type[Flow]) -> str:
        """Return `flow_code_digest(flow_class)` over the snapshots of the launch."""
        return flow_code_digest(flow_class, self._snapshots)
