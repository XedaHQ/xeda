"""The run directory of a flow, and the one way to delete anything in it.

Every run directory belongs to Xeda. The launcher chooses it under the run root that Xeda
created (`claimed`), and every file in it is the run's. All deletions in it go through
`RunDirectory.remove`, `clear` or `delete`. These are the deletions of the launcher (`--clean`,
`--post-cleanup`, `xeda scrub`, the pruning of DSE) and of a flow. The methods act inside the
directory only. They decide containment without following a symbolic link out of the directory,
and they remove a link as itself. Every file that Xeda itself writes there goes through
`writable`, which never writes through a link at that name. A flow built directly, without a
launcher, holds an `unlaunched` directory, in which Xeda deletes nothing.

Identity, never a clock, tells what a run wrote there (`OutputSnapshot`). The snapshot holds the
state of every file and directory under the run directory just before the run: device, inode,
size, mtime and ctime. `Flow.wrote_output` compares the state of an output after the run with it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .utils import XedaException

log = logging.getLogger(__name__)

__all__ = [
    "DIR_NAME_HASH_LEN",
    "OutputSnapshot",
    "OutputState",
    "RunDirectory",
    "RunDirectoryError",
    "link_on_way",
    "link_refusal",
    "record_output_state",
    "refuse_link_on_way",
    "resolved_inside",
    "rmtree",
    "run_directory_name",
    "run_directory_problem",
]


class RunDirectoryError(XedaException):
    """A run directory that Xeda cannot use, or a path in one that leads out of it.

    A run directory is unusable when it leads out of its run root or holds files of the design.
    `path` is the directory or link that the refusal names, when it names one.
    """

    def __init__(self, *args: object, path: Path | None = None) -> None:
        super().__init__(*args)
        self.path = path


#: The length of the run hash in the name of a hashed run directory, `<flow>_<hash>`.
DIR_NAME_HASH_LEN = 16


def run_directory_name(flow_name: str) -> re.Pattern[str]:
    """Return the pattern of the names that a run directory of `flow_name` has.

    A name is `flow_name`, which every default run makes. With `hashed_run_dirs`, a name is
    `flow_name`, an underscore and a run hash of `DIR_NAME_HASH_LEN` characters from `[a-z0-9]`.
    The pattern ends with `\\Z` and not with `$`, because `$` also matches before a final
    newline. A directory named `<flow>` and a newline is not a run directory.
    """
    return re.compile(f"^{re.escape(flow_name)}(_" + (r"[a-z0-9]" * DIR_NAME_HASH_LEN) + r")?\Z")


def run_directory_problem(path: Path, flow_name: str, parent: Path) -> Optional[str]:
    """Return why `path` is not a run directory of `flow_name` in `parent`, or None if it is.

    This is the one definition of a run directory for a launch and for `xeda scrub`. A launch
    runs only in a run directory, and scrub removes only run directories. `parent` is the
    directory that holds run directories, resolved (`os.path.realpath`): the directory of the
    design or of the target.

    A run directory is named `flow_name` or `flow_name_<hash>` (`run_directory_name`), is a
    directory, and lies in `parent`. A symbolic link counts only if the end of its chain of links
    is a directory that is named like a run directory of `flow_name` and lies in `parent`. A link
    therefore never stands for the directory of a target, the run directory of another flow, a
    directory below or above `parent`, a directory outside the run root, a file, or nowhere.
    `os.path.realpath` finds the end of the chain, as for the lock beside a link. The function
    only reads: it follows links and looks at what is at their end.

    The answer is a clause with "it" as its subject, ready to follow a name: "it is a link to
    /run/d/a, which is not a directory named vivado_synth or vivado_synth_<hash> in /run/d".
    """
    pattern = run_directory_name(flow_name)
    if pattern.match(path.name) is None:
        return f"its name is not {flow_name} or {flow_name}_<hash>"
    ends_at = Path(os.path.realpath(path))
    if not os.path.islink(path):
        if not path.is_dir():
            return "it is not a directory"
        if ends_at.parent != parent:
            return f"it resolves to {ends_at}, which is not in {parent}"
        return None
    if not ends_at.exists():
        return f"it is a link to {ends_at}, which does not exist"
    if not ends_at.is_dir():
        return f"it is a link to {ends_at}, which is not a directory"
    if pattern.match(ends_at.name) is None or ends_at.parent != parent:
        return (
            f"it is a link to {ends_at}, which is not a directory named {flow_name} or "
            f"{flow_name}_<hash> in {parent}"
        )
    return None


def link_on_way(
    directory: Union[str, os.PathLike], run_root: Union[str, os.PathLike]
) -> Path | None:
    """Return the first symbolic link on the way from the run root down to `directory`, or None.

    `directory` is the directory that holds run directories: the one of the design, of the
    target, or of the mirror of a host. It is included. The way starts below the first name, from
    the top, that resolves to the run root, so a run root that is a link itself is fine, and so is
    any other name for it. The run directory is not on the way. Its own rule is
    `run_directory_problem`. The function only reads. It returns None for a `directory` that
    does not lie below the run root; `RunDirectory.lies_under` judges that.
    """
    given = Path(os.path.abspath(directory))
    root = os.path.realpath(run_root)
    names = [*reversed(given.parents), given]
    for index, name in enumerate(names):
        if os.path.realpath(name) == root:
            return next((below for below in names[index + 1 :] if below.is_symlink()), None)
    return None


def refuse_link_on_way(
    directory: Union[str, os.PathLike], run_root: Union[str, os.PathLike]
) -> None:
    """Raise a `RunDirectoryError` that names the first symbolic link found by `link_on_way`.

    Xeda neither follows nor removes such a link, and writes or deletes nothing through it. This
    is the one rule of the launch, of `xeda scrub` and of the mirror of a remote run.
    """
    link = link_on_way(directory, run_root)
    if link is not None:
        raise RunDirectoryError(link_refusal(link), path=link)


def link_refusal(link: Path) -> str:
    """Return the sentence that refuses `link`, a symbolic link on the way to a run directory."""
    return (
        f"{link} is a symbolic link (to {os.readlink(link)}). Xeda neither follows nor removes "
        "it: remove the link, or give the run root another place with --run-root."
    )


#: The state of a file or directory: `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`.
OutputState = Tuple[int, int, int, int, int]


def record_output_state(path: Union[str, os.PathLike]) -> Optional[OutputState]:
    """Return the state of `path`, or None if it is not there.

    The state holds the identity (device and inode), size, mtime and ctime. `Flow.wrote_output`
    compares the later state of an output with it. A change of the identity or metadata of the
    file tells the output of a run from one that an earlier run left, and a clock never does. A
    file on a file system whose clock is behind, or one read within a coarse tick (2 s on FAT),
    is judged the same way. No comparison like `mtime >= some_timestamp` is involved.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class OutputSnapshot:
    """What was under a run directory before a run could write to it.

    The snapshot holds the state of every file and directory there (`record_output_state`),
    keyed by identity (device and inode) and not by path.

    One file has many names: a link to it or to a directory that holds it, a path through a link
    to the run directory, another letter case or Unicode form on a file system that ignores
    them. An output is the same output under any of them, so `changed` looks it up by its
    identity. The walk is taken before anything can write to the directory. If it read every
    directory, it saw every file and directory, so one whose identity it did not record was
    created or moved there since. A directory that the walk could not read (`unread`) proves
    nothing: what is found under it later has no known prior state. The walk does not follow
    symbolic links to a directory, so it cannot leave the directory or loop. It walks what a
    link inside the directory leads to, when that lies inside, at the place where it lies.
    """

    def __init__(self, directory: Path) -> None:
        self.states: Dict[Tuple[int, int], OutputState] = {}
        #: The directories, resolved, that the walk could not read.
        self.unread: List[Path] = []

        def unreadable(error: OSError) -> None:
            if isinstance(error, FileNotFoundError):
                return  # run directory not created yet: nothing to record
            self.unread.append(Path(error.filename or directory).resolve())

        for dirpath, _dirnames, filenames in os.walk(
            directory, onerror=unreadable, followlinks=False
        ):
            for path in (dirpath, *(os.path.join(dirpath, name) for name in filenames)):
                state = record_output_state(path)
                if state is not None:
                    self.states[state[:2]] = state

    def changed(self, path: Path, current: OutputState) -> Optional[bool]:
        """Return whether what is at `path` now, with state `current`, is new or changed.

        `path` is resolved and lies under the directory of the snapshot. The entry is new or
        changed if the snapshot recorded its identity with another state, or did not record it
        at all (created or moved there since). Return None if the snapshot cannot tell, because
        `path` lies in a directory that the walk could not read.
        """
        prior = self.states.get(current[:2])
        if prior is not None:
            return prior != current
        if any(path.is_relative_to(unread) for unread in self.unread):
            return None
        return True


def resolved_inside(path: Union[str, os.PathLike], directory: Path) -> Optional[Path]:
    """Return `path`, resolved, if it lies strictly inside `directory`, else None.

    A relative `path` is taken relative to `directory`. `directory` is resolved too.
    """
    root = directory.resolve()
    resolved = (root / path).resolve()
    if resolved != root and resolved.is_relative_to(root):
        return resolved
    return None


def _make_writable(path: str) -> None:
    """Add the read, write and search permission of the owner to `path`, keeping its other bits.

    Do nothing for a symbolic link: `chmod` follows links, and Xeda never acts through a link
    that a tool made.
    """
    if not os.path.islink(path):
        os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IRWXU)


def rmtree(path: Path) -> None:
    """Run `shutil.rmtree` and make read-only entries writable.

    Only `RunDirectory` calls it, on a path that it checked. When a read-only entry blocks a
    removal, the function makes the entry and its directory writable and tries again. It never
    changes the directory that holds `path` itself.
    """
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
    """The run directory of a flow, decided once by the launcher."""

    #: The directory, resolved.
    path: Path
    #: The run root that the directory lies under. None for a flow built directly (`unlaunched`).
    run_root: Path | None = None
    #: The logs of tools (`logs/<program>.log`) that this run made so far. A later tool of the run
    #: adds to such a log, and the first tool of a run replaces the log of an earlier run.
    tool_logs: set[Path] = field(default_factory=set, compare=False, repr=False)

    @staticmethod
    def lies_under(path: Path, run_root: Path) -> bool:
        """Return whether `path`, resolved, lies strictly under `run_root`, resolved."""
        resolved, root = Path(os.path.realpath(path)), Path(os.path.realpath(run_root))
        return resolved != root and resolved.is_relative_to(root)

    @classmethod
    def claimed(cls, path: Path, run_root: Path) -> RunDirectory:
        """Return a run directory that Xeda chose under `run_root`. `path` must lie under it.

        No name between the run root and `path` is a symbolic link (`link_on_way`): the
        `RunDirectoryError` names the first one."""
        refuse_link_on_way(Path(os.path.abspath(path)).parent, run_root)
        if not cls.lies_under(path, run_root):
            raise ValueError(f"{path} is not under the run root {run_root}: it is not Xeda's")
        return cls(Path(os.path.realpath(path)), Path(os.path.realpath(run_root)))

    @classmethod
    def unlaunched(cls, path: Path) -> RunDirectory:
        """Return the directory of a flow built directly, without a launcher.

        Xeda deletes nothing in it.
        """
        return cls(Path(os.path.realpath(path)))

    def inside(self, path: str | os.PathLike) -> Path:
        """Return `path` (relative to the run directory, or absolute) located inside it.

        The last component stays as it is, so a symbolic link is itself. Raise a
        `RunDirectoryError` if `path` is not inside, because of `..` or because a symbolic link
        leads out. The error names the link. A tool may have made it, and Xeda neither follows
        nor removes it, and writes or deletes nothing through it.
        """
        given = Path(path)
        lexical = Path(os.path.abspath(given if given.is_absolute() else self.path / given))
        parent = Path(os.path.realpath(lexical.parent))
        if lexical == self.path or not parent.is_relative_to(self.path):
            link = self._link_out(lexical.parent)
            why = (
                f"{link} is a symbolic link out of it (to {os.readlink(link)}), which Xeda "
                "neither follows nor removes: remove the link, or name another path"
                if link is not None
                else "nothing is written or deleted there"
            )
            raise RunDirectoryError(f"{path} is not inside the run directory {self.path}: {why}")
        return parent / lexical.name

    def unlinked(self, path: str | os.PathLike) -> Path:
        """Return `path` inside the run directory (`inside`), reached through no symbolic link.

        A link that stays inside counts too. The caches of Xeda under a run root are named this
        way: the Xilinx chip databases, the records of a design generator and the clone of a Git
        dependency. A link in place of such a name would split a durable lock or an immutable
        entry between two inodes, or send a clone to a place that the run root does not reach.
        """
        located = self.inside(path)
        given = Path(path)
        lexical = Path(os.path.abspath(given if given.is_absolute() else self.path / given))
        if not lexical.is_relative_to(self.path):  # pragma: no cover (`inside` refused it)
            raise RunDirectoryError(f"{path} is not inside the run directory {self.path}")
        current = self.path
        for part in lexical.relative_to(self.path).parts:
            current = current / part
            if current.is_symlink():
                raise RunDirectoryError(
                    f"{current} is a symbolic link where Xeda keeps a cache of its own; "
                    "remove the link and run again."
                )
        return located

    def _link_out(self, directory: Path) -> Path | None:
        """Return the first symbolic link that leads out of the run directory, or None.

        The search follows the way from the run directory down to `directory`, a lexical path
        under it.
        """
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

    def named_within(self, path: str | os.PathLike) -> Path | None:
        """Return `path` as a name relative to this run directory if it lies in it, else None.

        The method resolves each directory on the way to `path`. It looks for the run directory
        among the resolved directory and its parents by file-system identity (`st_dev`,
        `st_ino`) and never by comparing spellings. Another letter case, an alias of the path of
        the run directory, and a link to a directory below it all count. The last component stays
        as it is, so a link at the name is the name. The name is what `path` says below the first
        directory that reaches the run directory. The method does not check it: `inside` does,
        and refuses a link on the way that leads out.
        """
        try:
            own = os.stat(self.path)
        except OSError:
            return None
        given = Path(os.path.abspath(path))
        for directory in reversed(given.parents):  # from the root down
            resolved = Path(os.path.realpath(directory))
            for parent in (resolved, *resolved.parents):
                try:
                    if os.path.samestat(os.stat(parent), own):
                        return resolved.relative_to(parent) / given.relative_to(directory)
                except OSError:
                    continue
        return None

    def holds(self, path: str | os.PathLike) -> bool:
        """Return whether `path` is inside the run directory (`inside`)."""
        try:
            self.inside(path)
        except RunDirectoryError:
            return False
        return True

    def writable(self, path: str | os.PathLike) -> Path:
        """Return `path` located inside the run directory (`inside`), ready for a write.

        Call it before Xeda, or a tool that Xeda points there, writes the file. It removes a
        symbolic link at that name as itself, so the write makes a regular file and never goes
        through the link. In an `unlaunched` directory it does not remove such a link and raises a
        `RunDirectoryError` instead.
        """
        located = self.inside(path)
        if located.is_symlink():
            if self.run_root is None:
                raise RunDirectoryError(
                    f"{located} is a symbolic link in {self.path}, which no launcher chose: "
                    "Xeda does not remove it"
                )
            located.unlink()
        return located

    def remove(self, *paths: str | os.PathLike) -> list[Path]:
        """Delete each of `paths` inside the run directory and return what was deleted.

        A path can be a file, a symbolic link (deleted as itself) or a directory tree. In an
        `unlaunched` directory, delete nothing and log it.
        """
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
        """Empty the run directory. In an `unlaunched` directory, do nothing and log it."""
        if self.run_root is None:
            log.info("Not emptying %s: it is no run directory a launcher chose", self.path)
            return
        if self.path.is_dir():
            log.info("Deleting all files in the run directory %s", self.path)
            self.remove(*sorted(self.path.iterdir()))

    def delete(self, *links: str | os.PathLike) -> None:
        """Delete the run directory itself, after each of `links`.

        The `links` are the names in the run root by which the directory was reached. The method
        removes a name that is a symbolic link as itself, first and whatever it leads to. It
        never follows the link, so no name is left leading nowhere. It leaves a name that is not
        a link (the directory itself, say) or that is gone as it is. It refuses a link whose own
        directory lies outside the run root, and then removes nothing.
        """
        if self.run_root is None:
            raise RunDirectoryError(f"{self.path} is no run directory a launcher chose")
        names = []
        for given in links:
            name = Path(os.path.abspath(given))
            if not name.is_symlink():
                continue
            if not Path(os.path.realpath(name.parent)).is_relative_to(self.run_root):
                raise RunDirectoryError(
                    f"{name} lies outside the run root {self.run_root}: Xeda does not remove it"
                )
            names.append(name)
        for name in names:
            name.unlink()  # the link itself, never what it leads to
        self.clear()
        self.path.rmdir()
