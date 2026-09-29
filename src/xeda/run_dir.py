"""A flow's run directory, and the one way anything in it is deleted.

Every run directory is xeda's (D21): one the launcher chose under the run root it created
(`claimed`), whose every file is the run's. Every deletion in it -- the launcher's (`--clean`,
`--post-cleanup`, `xeda scrub`, DSE's pruning), a flow's, and those a tool script used to make --
goes through `RunDirectory.remove`, `clear` or `delete`, which act inside the directory only:
containment is decided without following a symbolic link out of it, and a link is removed as
itself. Every file xeda writes there itself goes through `writable`, which never writes through a
link at that name. A flow built directly, not launched, holds an `unlaunched` directory, in which
xeda deletes nothing.

What a run wrote there is told by identity, never by a clock (`OutputSnapshot`): the state of every
file and directory under the run directory -- device, inode, size, mtime, ctime -- as it was just
before the run, against which `Flow.wrote_output` compares an output's state after it.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .utils import XedaException

log = logging.getLogger(__name__)

__all__ = [
    "OutputSnapshot",
    "OutputState",
    "RunDirectory",
    "RunDirectoryError",
    "record_output_state",
    "resolved_inside",
    "rmtree",
]


class RunDirectoryError(XedaException):
    """A run directory xeda cannot use: one that leads out of its run root, or one holding the
    design's own files; or a path in one that leads out of it."""


#: A file's or directory's state: `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`.
OutputState = Tuple[int, int, int, int, int]


def record_output_state(path: Union[str, os.PathLike]) -> Optional[OutputState]:
    """`path`'s state -- its identity (device, inode), size, mtime and ctime -- or `None` if it
    is not there.

    What `Flow.wrote_output` compares an output's later state against, so that a run's own
    output is told from one an earlier run left by the file's own identity and metadata
    changing -- never a clock. A file on a file system whose clock is behind, or read within a
    coarse tick (FAT's 2s), is judged the same way: unsound comparisons like
    `mtime >= some_timestamp` never enter into it.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class OutputSnapshot:
    """What was under a run directory before a run could write to it: the state of every file
    and directory there (`record_output_state`), keyed by its identity -- device and inode --
    rather than by a path.

    One file has many names: a link to it or to a directory holding it, a path through a link to
    the run directory, another letter case or Unicode form on a file system that ignores them. An
    output is the same output under any of them, so `changed` looks it up by what it *is*. Taken
    before anything can write to the directory, a walk that read every directory saw every file
    and directory in it, so one whose identity it did not record was created (or moved there)
    since. A directory the walk could not read (`unread`) proves nothing: what is found under it
    later has no known prior state. Symbolic links to a directory are not followed, so the walk
    cannot leave the directory or loop; what a link inside leads to inside is walked where it
    lies.
    """

    def __init__(self, directory: Path) -> None:
        self.states: Dict[Tuple[int, int], OutputState] = {}
        #: The directories, resolved, the walk could not read.
        self.unread: List[Path] = []

        def unreadable(error: OSError) -> None:
            if isinstance(error, FileNotFoundError):
                return  # not there (a run directory not created yet): nothing to record
            self.unread.append(Path(error.filename or directory).resolve())

        for dirpath, _dirnames, filenames in os.walk(
            directory, onerror=unreadable, followlinks=False
        ):
            for path in (dirpath, *(os.path.join(dirpath, name) for name in filenames)):
                state = record_output_state(path)
                if state is not None:
                    self.states[state[:2]] = state

    def changed(self, path: Path, current: OutputState) -> Optional[bool]:
        """Whether what is at `path` now -- resolved, under the snapshot's directory -- with
        state `current` is new or changed since the snapshot was taken: its identity recorded
        with another state, or not recorded at all (created or moved there since). `None` if the
        snapshot cannot tell, `path` lying in a directory the walk could not read."""
        prior = self.states.get(current[:2])
        if prior is not None:
            return prior != current
        if any(path.is_relative_to(unread) for unread in self.unread):
            return None
        return True


def resolved_inside(path: Union[str, os.PathLike], directory: Path) -> Optional[Path]:
    """`path`, relative to `directory` unless absolute, resolved -- if that lies strictly inside
    `directory` (resolved too), else None."""
    root = directory.resolve()
    resolved = (root / path).resolve()
    if resolved != root and resolved.is_relative_to(root):
        return resolved
    return None


def _make_writable(path: str) -> None:
    """Add the owner's read, write and search permission to `path`, keeping its other bits,
    unless it is a symbolic link: `chmod` follows links, and xeda never acts through a tool-made
    one."""
    if not os.path.islink(path):
        os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IRWXU)


def rmtree(path: Path) -> None:
    """`shutil.rmtree`, making read-only entries writable. Only `RunDirectory` calls it, on a
    path it checked. A removal a read-only entry blocks is retried after making the entry and
    its directory writable; the directory holding `path` itself is never touched."""
    root = os.path.abspath(path)

    def on_error(func: Any, entry: str, _exc: Any) -> None:
        entry = os.path.abspath(entry)
        if entry != root:
            _make_writable(os.path.dirname(entry))
        _make_writable(entry)
        func(entry)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:
        shutil.rmtree(path, onerror=on_error)


@dataclass(frozen=True)
class RunDirectory:
    """A flow's run directory, decided once, by the launcher."""

    #: the directory, resolved
    path: Path
    #: the run root it lies under; None for a flow built directly (`unlaunched`)
    run_root: Path | None = None

    @staticmethod
    def lies_under(path: Path, run_root: Path) -> bool:
        """Whether `path`, resolved, lies strictly under `run_root`, resolved."""
        resolved, root = Path(os.path.realpath(path)), Path(os.path.realpath(run_root))
        return resolved != root and resolved.is_relative_to(root)

    @classmethod
    def claimed(cls, path: Path, run_root: Path) -> RunDirectory:
        """A run directory xeda chose under `run_root`: `path` must lie under it."""
        if not cls.lies_under(path, run_root):
            raise ValueError(f"{path} is not under the run root {run_root}: it is not xeda's")
        return cls(Path(os.path.realpath(path)), Path(os.path.realpath(run_root)))

    @classmethod
    def unlaunched(cls, path: Path) -> RunDirectory:
        """The directory of a flow built directly, not launched: xeda deletes nothing in it."""
        return cls(Path(os.path.realpath(path)))

    def inside(self, path: str | os.PathLike) -> Path:
        """`path` (relative to the run directory, or absolute) inside it, its last component
        kept as it is (a symbolic link is itself); a `RunDirectoryError` if it is not inside --
        by `..`, or through a symbolic link that leads out, which is named: a tool may have made
        it, and xeda neither follows nor removes it, nor writes or deletes anything through it."""
        given = Path(path)
        lexical = Path(os.path.abspath(given if given.is_absolute() else self.path / given))
        parent = Path(os.path.realpath(lexical.parent))
        if lexical == self.path or not parent.is_relative_to(self.path):
            link = self._link_out(lexical.parent)
            why = (
                f"{link} is a symbolic link out of it (to {os.readlink(link)}), which xeda "
                "neither follows nor removes: remove the link, or name another path"
                if link is not None
                else "nothing is written or deleted there"
            )
            raise RunDirectoryError(f"{path} is not inside the run directory {self.path}: {why}")
        return parent / lexical.name

    def _link_out(self, directory: Path) -> Path | None:
        """The first symbolic link on the way from the run directory down to `directory` (a
        lexical path under it) that leads out of the run directory, if any."""
        if not directory.is_relative_to(self.path):
            return None
        current = self.path
        for part in directory.relative_to(self.path).parts:
            current = current / part
            if current.is_symlink() and not Path(os.path.realpath(current)).is_relative_to(
                self.path
            ):
                return current
        return None

    def holds(self, path: str | os.PathLike) -> bool:
        """Whether `path` is inside the run directory (`inside`)."""
        try:
            self.inside(path)
        except RunDirectoryError:
            return False
        return True

    def writable(self, path: str | os.PathLike) -> Path:
        """Before xeda (or the tool it points there) writes the file `path`: `path` located inside
        the run directory (`inside`), a symbolic link at that name removed as itself, so the
        write makes a regular file and never goes through the link."""
        located = self.inside(path)
        if located.is_symlink():
            if self.run_root is None:
                raise RunDirectoryError(
                    f"{located} is a symbolic link in {self.path}, which no launcher chose: "
                    "xeda does not remove it"
                )
            located.unlink()
        return located

    def remove(self, *paths: str | os.PathLike) -> list[Path]:
        """Delete each of `paths` -- a file, a symbolic link (as itself) or a directory tree --
        inside the run directory; nothing, in an `unlaunched` one (logged). What was deleted."""
        removed: list[Path] = []
        for given in paths:
            path = self.inside(given)
            if not os.path.lexists(path):
                continue
            if self.run_root is None:
                log.info(
                    "Not deleting %s: %s is no run directory a launcher chose", path, self.path
                )
                continue
            if path.is_dir() and not path.is_symlink():
                rmtree(path)
            else:
                path.unlink()
            removed.append(path)
        return removed

    def clear(self) -> None:
        """Empty the run directory (nothing, in an `unlaunched` one: logged)."""
        if self.run_root is None:
            log.info("Not emptying %s: it is no run directory a launcher chose", self.path)
            return
        if self.path.is_dir():
            log.info("Deleting all files in the run directory %s", self.path)
            self.remove(*sorted(self.path.iterdir()))

    def delete(self) -> None:
        """Delete the run directory itself."""
        if self.run_root is None:
            raise RunDirectoryError(f"{self.path} is no run directory a launcher chose")
        self.clear()
        self.path.rmdir()
