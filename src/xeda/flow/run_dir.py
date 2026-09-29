"""Which directories are xeda's to run in, empty and delete from.

A run replaces and deletes files in its run directory: a flow's `clean` empties it, tool scripts
remove work directories by name, and projects are recreated with `-force`. So xeda runs only in
a directory that is its own, and knows it by the marker (`RUN_DIR_MARKER`) it writes into every
run directory it uses (`claim_run_dir`):

- one given explicitly (`--cwd`, the launcher's `run_path`) is used only if it does not exist,
  is empty, or is marked;
- one xeda chooses, `<run root>/<design>/<flow>` (a dependency's nested in its depender's), must
  lie strictly inside the run root (`run_dir_name`, `check_inside_run_root`), and is used only
  if it does not exist, is empty, is marked, or holds an earlier xeda run of the same flow
  (`is_earlier_run_of`: an existing `xeda_run` tree keeps working).

Either is a directory itself, never a link to one (`refuse_linked_run_dir`): through a link, a
run would clean and write in whatever the link leads to. Anything else is refused before
anything is created, written or deleted. A flow empties only a marked run directory
(`Flow.purge_run_path`), removes a work directory by name only inside the run directory
(`Flow.removable_work_dir`), and removes an earlier copy of an output only there
(`Flow.remove_stale_output`): xeda deletes nothing outside the run directory.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from pathvalidate import sanitize_filename

from ..utils import XedaException

__all__ = [
    "RUN_DIR_MARKER",
    "OutputSnapshot",
    "OutputState",
    "RunDirectoryError",
    "check_inside_run_root",
    "claim_run_dir",
    "is_earlier_run_of",
    "is_marked_run_dir",
    "mark_run_dir",
    "record_output_state",
    "refuse_linked_run_dir",
    "resolved_inside",
    "run_dir_name",
]

log = logging.getLogger(__name__)

#: The file that marks a directory as a run directory of xeda's.
RUN_DIR_MARKER = ".xeda-run-dir"
#: The version of the marker's format.
RUN_DIR_MARKER_FORMAT = 1

_MARKER_TEXT = (
    "# This directory is a run directory of xeda: a run replaces and deletes files here.\n"
    "# Keep nothing of yours in it.\n"
    f"format = {RUN_DIR_MARKER_FORMAT}\n"
)

_ONLY_ITS_OWN = (
    "xeda runs only in an empty directory or one it created, since a run replaces and deletes "
    "files in its run directory."
)
_INSTEAD = (
    "Run without --cwd (or the launcher's run_path): the run then goes to xeda_run/. Or start in "
    "an empty directory."
)


class RunDirectoryError(XedaException):
    """A run directory xeda must not use, or a path inside one it must not remove."""


def refuse_linked_run_dir(directory: Path) -> None:
    """Refuse `directory` as a run directory if it is itself a symbolic link, naming it.

    A run cleans, deletes and writes in its run directory; through a link, it would do all that
    in whatever directory the link leads to -- one marked as xeda's included, since its marker
    is read through the link too. So a run directory is a directory, given or derived."""
    if directory.is_symlink():
        raise RunDirectoryError(
            f"{directory} is a symbolic link (to {os.readlink(directory)}), not a directory: a "
            "run cleans, deletes and writes files in its run directory, and xeda does none of that "
            "through a link. Give the directory it leads to instead, or remove the link."
        )


def is_marked_run_dir(directory: Path) -> bool:
    """Whether `directory` carries xeda's marker: a regular file, not a link to one, in a
    directory that is not a link either (`refuse_linked_run_dir`)."""
    marker = directory / RUN_DIR_MARKER
    return not directory.is_symlink() and marker.is_file() and not marker.is_symlink()


def mark_run_dir(directory: Path) -> None:
    """Mark `directory`, which xeda created, found empty or adopted, as xeda's.

    The marker is created, never written through: anything else already at its name -- a link,
    say -- is refused, naming it, as is a directory that is itself a link."""
    refuse_linked_run_dir(directory)
    if is_marked_run_dir(directory):
        return
    marker = directory / RUN_DIR_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, flags, 0o666)
    except FileExistsError:
        raise RunDirectoryError(
            f"{marker} is not xeda's marker but a link or another kind of entry, which xeda does "
            f"not write through. Remove it if {directory} is xeda's."
        ) from None
    with os.fdopen(fd, "w") as f:
        f.write(_MARKER_TEXT)


def is_earlier_run_of(directory: Path, flow_name: str) -> bool:
    """Whether `directory` holds an earlier xeda run of `flow_name`: its `settings.json` is xeda's
    run record of that flow, as every xeda since 0.2 writes it."""
    try:
        record = json.loads((directory / "settings.json").read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(record, dict)
        and record.get("flow_name") == flow_name
        and "flow_settings" in record
        and "xeda_version" in record
    )


def claim_run_dir(directory: Path, flow_name: Optional[str] = None) -> None:
    """Make `directory` a run directory of xeda's, marked, or refuse it.

    Without `flow_name`, `directory` was given explicitly (`--cwd`, `run_path`); with it, xeda
    chose it for a run of that flow. A marked directory is used as it is. One that does not exist
    is created, and an empty one adopted; a chosen one holding an earlier xeda run of the same
    flow is adopted too. Each is marked. Anything else, a link to a directory included
    (`refuse_linked_run_dir`), is refused before anything is created, written or deleted.
    """
    refuse_linked_run_dir(directory)
    if is_marked_run_dir(directory):
        return
    if directory.exists() or directory.is_symlink():
        if not directory.is_dir():
            raise RunDirectoryError(f"{directory} is not a directory; {_ONLY_ITS_OWN} {_INSTEAD}")
        if flow_name is not None and is_earlier_run_of(directory, flow_name):
            log.info(
                "Adopting %s, an earlier run of %s, as xeda's run directory", directory, flow_name
            )
        elif any(directory.iterdir()):
            if flow_name is None:
                raise RunDirectoryError(
                    f"{directory} holds files xeda did not put there; {_ONLY_ITS_OWN} {_INSTEAD}"
                )
            raise RunDirectoryError(
                f"{directory}, the run directory of {flow_name}, holds files xeda did not put "
                f"there: it carries no {RUN_DIR_MARKER} and holds no earlier run of {flow_name} "
                "(a settings.json of xeda's), and a run replaces and deletes files in its run "
                "directory. Move your files out of it, or give another --xeda-run-dir."
            )
    else:
        directory.mkdir(parents=True)
    mark_run_dir(directory)
    if flow_name is None:
        log.info(
            "xeda now uses %s as a run directory: a run replaces and deletes files there, so keep "
            "nothing of yours in it",
            directory,
        )


#: A file's or directory's state: `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`.
OutputState = Tuple[int, int, int, int, int]


def record_output_state(path: Union[str, os.PathLike]) -> Optional[OutputState]:
    """`path`'s state -- its identity (device, inode), size, mtime and ctime -- or `None` if it
    is not there.

    What `Flow.wrote_output` compares an output's later state against, so that a run's own
    output is told from one an earlier run left by the file's own identity and metadata
    changing -- never a clock. A file on a file system whose clock is behind (an external, named
    output) or read within a coarse tick (FAT's 2s) is judged the same way: unsound comparisons
    like `mtime >= some_timestamp` never enter into it.
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


def run_dir_name(name: str, what: str) -> str:
    """`name`, a design's or a flow's, as the one directory it names inside the run root.

    Unsafe characters are dropped (`sanitize_filename`), as before. A name holding a path
    separator (an absolute path too), or naming no directory of its own (`..`, `.`, or nothing
    once sanitized), is refused: its run directory would lie elsewhere.
    """
    sanitized = sanitize_filename(name)
    if "/" in name or "\\" in name or sanitized in ("", ".", ".."):
        raise RunDirectoryError(
            f"The {what} name {name!r} does not name a directory of its own: a flow runs in "
            f"<run root>/<design>/<flow>, and this name would put the run elsewhere. Name the "
            f"{what} with letters, digits, '_' and '-'."
        )
    return sanitized


def check_inside_run_root(run_path: Path, run_root: Path, what: str = "the run root") -> None:
    """Refuse a run directory xeda derives that does not resolve strictly inside the directory
    it derives it in: the run root for a flow's, its depender's run directory for a
    dependency's (`what` names which)."""
    if resolved_inside(run_path, run_root) is None:
        raise RunDirectoryError(
            f"The run directory {run_path} resolves to {run_path.resolve()}, outside {what} "
            f"{run_root.resolve()}: xeda runs, and deletes files, only in directories it derives "
            "there. Remove the link that leads out of it."
        )
