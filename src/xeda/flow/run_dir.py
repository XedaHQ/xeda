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

Anything else is refused before anything is created, written or deleted. A flow empties only a
marked run directory (`Flow.purge_run_path`), removes a work directory by name only inside the
run directory (`Flow.removable_work_dir`), and removes an earlier copy of an output only there
(`Flow.remove_stale_output`): xeda deletes nothing outside the run directory.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from pathvalidate import sanitize_filename

from ..utils import XedaException

__all__ = [
    "RUN_DIR_MARKER",
    "RunDirectoryError",
    "check_inside_run_root",
    "claim_run_dir",
    "is_earlier_run_of",
    "is_marked_run_dir",
    "mark_run_dir",
    "record_output_state",
    "resolved_inside",
    "run_dir_name",
    "snapshot_output_states",
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


def is_marked_run_dir(directory: Path) -> bool:
    """Whether `directory` carries xeda's marker: a regular file, not a link to one."""
    marker = directory / RUN_DIR_MARKER
    return marker.is_file() and not marker.is_symlink()


def mark_run_dir(directory: Path) -> None:
    """Mark `directory`, which xeda created, found empty or adopted, as xeda's.

    The marker is created, never written through: anything else already at its name -- a link,
    say -- is refused, naming it."""
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
    flow is adopted too. Each is marked. Anything else is refused before anything is created,
    written or deleted.
    """
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


def record_output_state(path: Path) -> Optional[Tuple[int, int, int, int]]:
    """`path`'s `(inode, size, mtime_ns, ctime_ns)`, or `None` if it is not there.

    What `Flow.wrote_output` compares a path's later state against, so that a run's own output is
    told from one an earlier run left by the file's own identity and metadata changing -- never a
    clock. A file on a file system whose clock is behind (an external, named output) or read
    within a coarse tick (FAT's 2s) is judged the same way: unsound comparisons like `mtime >=
    some_timestamp` never enter into it.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def snapshot_output_states(directory: Path) -> Dict[Path, Tuple[int, int, int, int]]:
    """Every existing file's state under `directory`, recursively, keyed by its path.

    Meant to be taken before anything can write to `directory`: a path found here later with a
    different state (or a path never listed here at all) is new or changed since. `directory` not
    existing yet, or being empty, both come back empty -- everything under it was absent.
    Symbolic links to a directory are not followed, so a link cannot walk this outside `directory`
    or loop back into it.
    """
    states: Dict[Path, Tuple[int, int, int, int]] = {}
    for dirpath, _dirnames, filenames in os.walk(directory, followlinks=False):
        for filename in filenames:
            path = Path(dirpath) / filename
            state = record_output_state(path)
            if state is not None:
                states[path] = state
    return states


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
