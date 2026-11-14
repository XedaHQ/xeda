"""The code that this process imported: the files of the `xeda` package and of each plugin flow.

A process runs the code that it imported. A trace, a generation record and a reuse must name that
code, and not the code that is on disk later. So the process takes a record of its code once, and
each launch checks that the files on disk are still those (`verify`). When they are not, the
process runs other code than the files say, and it refuses to launch (`StaleRuntimeError`, code
`stale_runtime`). Only a new process runs the new code.

- The `xeda` package is recorded when `xeda` is imported, before any other module of Xeda. This
  module uses only the standard library, so the record is taken before the heavy modules load.
- The code root of a plugin flow (`root_files`) is recorded when its first Flow class is created
  (`Flow.__init_subclass__`), or else when Xeda first asks for its digest.

A record holds the metadata of each file (size, modification time, inode change time, inode). It
holds the content digest only of a file that changed within the racy window (`settled`): that file
is read at once. The other files are read at the first check, and the rule of the trace
(`trusted`, which `digest.FileRecord` applies too) tells whether their metadata still vouches for
them then. A later
check stats each file and reads one only when its metadata cannot vouch for it. A file added or
removed, or whose content differs, makes the process stale. A file whose metadata changed before
the first check, when its content was not yet read, does too, since nothing tells what was
imported.

A second import of `xeda` or a reload of a module keeps the first record: only a new process
starts a new one. Nothing here sees a change in memory (a monkeypatch, `importlib.reload`), the
versions of installed dependencies, or a file that changed and changed back between two checks.
"""

from __future__ import annotations

import ast
import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .listing import directory_files
from .package_paths import package_root

#: The timestamp granularity that the racy check assumes. 2 s covers FAT and HFS+. On file systems
#: with nanosecond timestamps it costs one extra hash of files written just before a record.
RACY_NS = 2_000_000_000

#: The marker that makes a directory a run root; any regular file by this name does.
RUN_ROOT_MARKER = ".xeda-run-root"

_CHUNK = 1 << 20

#: The directory of the installed xeda package.
XEDA_PACKAGE = package_root().resolve()


def content_digest(path: Path) -> str:
    """Return the SHA3-256 of the file content, cut to 128 bits (`Source._content_hash`)."""
    h = hashlib.sha3_256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()[:32]


def digest_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """Return a digest of the `(name, content digest)` pairs, whatever their order.

    A pair that occurs twice counts twice."""
    h = hashlib.sha3_256()
    for name, sha in sorted(pairs):
        h.update(name.encode() + b"\0")
        h.update(sha.encode())
    return h.hexdigest()[:32]


def settled(mtime_ns: int, ctime_ns: int, time_ns: int, racy_ns: int | None = None) -> bool:
    """Return whether a file that last changed at `mtime_ns` or `ctime_ns` did so well before
    `time_ns`, outside the racy window of `racy_ns` (`RACY_NS` by default).

    A file recorded at `time_ns` can then be trusted by its metadata later. A file written
    within the same timestamp tick could have changed after it was hashed without its metadata
    showing it. This is git's "racy git" rule."""
    return max(mtime_ns, ctime_ns) + (RACY_NS if racy_ns is None else racy_ns) < time_ns


def metadata(st: os.stat_result) -> tuple[int, int, int, int]:
    """Return what a record keeps of the status of a file: size, mtime, inode change time and
    inode."""
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def trusted(
    recorded: tuple[int, int, int, int],
    st: os.stat_result,
    trusted_before_ns: int,
    racy_ns: int | None = None,
) -> bool:
    """Return whether a file with status `st` is the file whose `metadata` was `recorded` at
    `trusted_before_ns`, without reading the file.

    It is when size, mtime, inode change time and inode are the same, and the file last changed
    well before the record was taken (`settled`)."""
    return metadata(st) == recorded and settled(
        recorded[1], recorded[2], trusted_before_ns, racy_ns
    )


def is_run_root(path: Path) -> bool:
    """Return whether `path` holds the marker of a run root: a regular file with any content."""
    marker = Path(path) / RUN_ROOT_MARKER
    return marker.is_file() and not marker.is_symlink()


def package_files(directory: Path) -> list[Path]:
    """Return the files that make up a Python package directory (`listing.directory_files`,
    resolved).

    These are all files under it except compiled bytecode (`__pycache__`, `.pyc`), hidden files,
    which Python and editors create, and the files of a run root (`is_run_root`), which a launch
    started in the directory writes.
    """
    top = directory.resolve()
    listed = directory_files(top, skip=frozenset({"__pycache__"}), follow_links=True)
    run_roots = tuple(
        p.parent for p in listed if p.name == RUN_ROOT_MARKER and is_run_root(p.parent)
    )
    return sorted(
        p
        for p in listed
        if p.is_file()
        and p.suffix not in (".pyc", ".pyo")
        and not any(part.startswith(".") for part in p.relative_to(top).parts)
        and not any(p.is_relative_to(root) for root in run_roots)
    )


# The code root of a plugin flow


def package_directory(module_file: Path) -> Path | None:
    """Return the top-level package directory of `module_file`, or None if it is in none."""
    directory = module_file.parent
    if not (directory / "__init__.py").is_file():
        return None
    while (directory.parent / "__init__.py").is_file():
        directory = directory.parent
    return directory


def _imported_modules(source: Path) -> set[str]:
    """Return the dotted names of the modules that the `import` statements of `source` name.

    `import a.b` names `a.b`, and `from a.b import c` names `a.b` and `a.b.c` (`c` can be a module
    or a name in `a.b`). A relative import of one dot (`from . import helper`, `from .helper import
    x`) names the module in the directory of `source`: it occurs in a namespace package, a
    directory without an `__init__.py`. Other relative imports name none. A module that does not
    parse names none."""
    try:
        tree = ast.parse(source.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level <= 1 and (node.level or node.module):
            if node.module:
                names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}" if node.module else alias.name)
    return names


def _local_module(root: Path, dotted: str) -> list[tuple[str, Path]]:
    """Return the `(name, file)` pairs of the module `dotted` in the directory `root`.

    A module is a file `a/b.py` or a package `a/b` (with an `__init__.py`), found under `root`
    through the directories of its name. A directory without an `__init__.py` on the way is a
    namespace package: it leads on, and only the module found counts, never the directory. Nothing
    is returned for a module that is not under `root`."""
    path, parts = root, dotted.split(".")
    for index, part in enumerate(parts):
        if (path / part / "__init__.py").is_file():
            # the whole package, as the rest of the name is inside it
            return tree_files("/".join(parts[: index + 1]), path / part)
        if (path / f"{part}.py").is_file():
            return [("/".join(parts[: index + 1]) + ".py", path / f"{part}.py")]
        if not (path / part).is_dir():
            return []
        path = path / part
    return []


def tree_files(name: str, directory: Path) -> list[tuple[str, Path]]:
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
            for found_name, file in _local_module(root, name):
                if found_name not in files:
                    files[found_name] = file
                    if file.suffix == ".py":
                        pending.append(file)
    if (root / "templates").is_dir():
        files.update(tree_files("templates", root / "templates"))
    return sorted(files.items())


def root_files(module_file: Path) -> list[tuple[str, Path]]:
    """Return the `(name, file)` pairs of the code root of the module `module_file`.

    The root is the top-level package of the module, or the module and what it imports, for a
    module in no package (`code_digest` says more). A name is relative to the root, and for a
    package it starts with the name of the package."""
    package = package_directory(module_file)
    if package is not None:
        return tree_files(package.name, package)
    return _module_files(module_file)


# The record of a code root, and the check


class _File:
    """What the process knows of one code file: its metadata when it was recorded (`metadata`),
    its content digest once it was read (`sha`), and the time the record was taken."""

    __slots__ = ("metadata", "sha", "taken_ns")

    def __init__(self, st: os.stat_result, sha: str | None, taken_ns: int) -> None:
        self.metadata, self.sha, self.taken_ns = metadata(st), sha, taken_ns


class CodeRoot:
    """The files of one code root as this process recorded them, named relative to the root."""

    def __init__(self, label: str, list_files: Callable[[], list[tuple[str, Path]]]) -> None:
        #: What the message of a refusal calls the code: "Xeda's code", or the place of a root.
        self.label = label
        self._list_files = list_files
        taken_ns = time.time_ns()
        self.files: dict[str, _File] = {}
        for name, path in list_files():
            try:
                st = os.stat(path)
                racy = not settled(st.st_mtime_ns, st.st_ctime_ns, taken_ns)
                self.files[name] = _File(st, content_digest(path) if racy else None, taken_ns)
            except OSError:
                pass  # gone since it was listed: the first check finds it missing or new

    def changed_files(self) -> list[str]:
        """Return the names of the files that are not what this process recorded, sorted.

        It reads a file only when its metadata cannot vouch for it. A file whose content was not
        read yet is read now, if its metadata is still that of the record."""
        now = dict(self._list_files())
        changed = set(now) ^ set(self.files)
        for name, known in self.files.items():
            if name in now and not _unchanged(known, now[name]):
                changed.add(name)
        return sorted(changed)

    def verified_pairs(self) -> list[tuple[str, str]]:
        """Return the `(name, content digest)` pairs of the recorded files, after a check.

        Raise `StaleRuntimeError` when a file changed (`changed_files`)."""
        with _LOCK:
            changed = self.changed_files()
            if changed:
                from xeda.errors import StaleRuntimeError

                more = f" (and {len(changed) - 1} more)" if len(changed) > 1 else ""
                raise StaleRuntimeError(
                    f"{self.label} changed on disk after this process imported it: "
                    f"{changed[0]}{more}. This process runs the old code. Restart the Python "
                    "process (in Jupyter, restart the kernel).",
                    code="stale_runtime",
                )
            return [(name, str(known.sha)) for name, known in self.files.items()]


def _unchanged(known: _File, path: Path) -> bool:
    """Return whether the file `path` holds what the record `known` says, and complete the
    record: its digest once read, and its metadata once read again with the same content.

    This is the rule of `digest.record_file` for a regular file."""
    taken_ns = time.time_ns()
    try:
        st = os.stat(path)
        if trusted(known.metadata, st, known.taken_ns):
            if known.sha is None:
                sha = content_digest(path)
                if metadata(os.stat(path)) != known.metadata:
                    return False  # it changed while it was read
                known.sha = sha
            return True
        if known.sha is None or content_digest(path) != known.sha:
            return False  # changed, or changed before it was read: what was imported is unknown
    except OSError:
        return False
    known.metadata, known.taken_ns = metadata(st), taken_ns
    return True


_LOCK = threading.Lock()

# A reload of this module runs this code again in the same namespace: the first record stays.
if "_XEDA" not in globals():
    #: The record of the `xeda` package, taken when the process imported it.
    _XEDA = CodeRoot(
        "Xeda's code",
        lambda: [
            (file.relative_to(XEDA_PACKAGE).as_posix(), file)
            for file in package_files(XEDA_PACKAGE)
        ],
    )
    #: The record of each plugin code root, by its top-level package or its module file.
    _ROOTS: dict[Path, CodeRoot] = {}
    #: The root that each module file of a Flow class was first found in. A later change of the
    #: package around it (an `__init__.py` added or removed) makes that root stale, and never
    #: starts another record.
    _ROOT_OF: dict[Path, CodeRoot] = {}


def verify() -> None:
    """Raise `StaleRuntimeError` if a file of the `xeda` package, or of the code of a flow that is
    registered now, is not what this process imported. Every entry that plans or launches calls
    this first, so a request of a stale process writes nothing."""
    _XEDA.verified_pairs()
    # The files of Xeda are those that this process imported, so these imports load no new code.
    from .flow import Flow, registered_flows
    from .flow.source_file import without_source_file
    from .flow_runner.code_digest import code_roots

    roots: list[CodeRoot] = []
    for _, flow_class in list(registered_flows.values()):
        classes = [c for c in flow_class.__mro__ if isinstance(c, type) and issubclass(c, Flow)]
        if all(c.__module__.partition(".")[0] == "xeda" for c in classes):
            continue  # a flow of Xeda's own: its files are those checked above
        if without_source_file(classes) is None:
            roots += [root for root in code_roots(flow_class) if root not in roots]
    for root in roots:  # many flows of one package share its root: check it once
        root.verified_pairs()


def xeda_digest() -> str:
    """Return the digest of the `xeda` package that this process imported, after `verify`."""
    return digest_pairs(_XEDA.verified_pairs())


def plugin_root(module_file: Path) -> CodeRoot:
    """Return the record of the code root of the module `module_file`, taken now if this is its
    first use.

    `Flow.__init_subclass__` calls this for the module of each Flow class outside Xeda, so a root
    is recorded as its first Flow class is created."""
    with _LOCK:
        root = _ROOT_OF.get(module_file)
        if root is None:
            top = package_directory(module_file) or module_file
            root = _ROOTS.get(top)
            if root is None:
                root = _ROOTS[top] = CodeRoot(
                    f"The code of a flow in {top}", lambda: root_files(module_file)
                )
            _ROOT_OF[module_file] = root
    return root
