"""Which directories are xeda's to run in, empty and delete from.

A run replaces and deletes files in its run directory: a flow's `clean` empties it, tool scripts
remove work directories by name, and projects are recreated with `-force`. So xeda runs only in
a directory that is its own:

- one it chooses, `<run root>/<design>/<flow>`, which must lie strictly inside the run root
  (`run_dir_name`, `check_inside_run_root`);
- one it is given (`--cwd`, the launcher's `run_path`) only if the directory does not exist,
  is empty, or carries xeda's marker (`claim_run_dir`), which xeda writes into every such
  directory it runs in.

A flow empties a run directory only if it is one of these (`is_xedas_run_dir`), and removes a
work directory by name only if it lies inside the run directory (`Flow.removable_work_dir`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pathvalidate import sanitize_filename

from ..utils import XedaException

__all__ = [
    "RUN_DIR_MARKER",
    "RunDirectoryError",
    "check_inside_run_root",
    "claim_run_dir",
    "is_marked_run_dir",
    "is_xedas_run_dir",
    "mark_run_dir",
    "run_dir_name",
]

log = logging.getLogger(__name__)

#: The file that marks a directory given explicitly (`--cwd`, `run_path`) as xeda's.
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
    """Mark `directory`, which xeda created or found empty, as xeda's."""
    if not is_marked_run_dir(directory):
        (directory / RUN_DIR_MARKER).write_text(_MARKER_TEXT)


def claim_run_dir(directory: Path) -> None:
    """Make `directory`, given explicitly (`--cwd`, `run_path`), a run directory of xeda's.

    A marked directory is used as it is. One that does not exist is created, and an empty one
    adopted, and either is marked. Anything else is refused before anything is created, written
    or deleted.
    """
    if is_marked_run_dir(directory):
        return
    if directory.exists() or directory.is_symlink():
        if not directory.is_dir():
            raise RunDirectoryError(f"{directory} is not a directory; {_ONLY_ITS_OWN} {_INSTEAD}")
        if any(directory.iterdir()):
            raise RunDirectoryError(
                f"{directory} holds files xeda did not put there; {_ONLY_ITS_OWN} {_INSTEAD}"
            )
    else:
        directory.mkdir(parents=True)
    mark_run_dir(directory)
    log.info(
        "xeda now uses %s as a run directory: a run replaces and deletes files there, so keep "
        "nothing of yours in it",
        directory,
    )


def is_xedas_run_dir(directory: Path, run_root: Path | None) -> bool:
    """Whether `directory` is xeda's to empty: strictly inside the run root (resolved), or
    marked."""
    if run_root is not None:
        resolved, root = directory.resolve(), run_root.resolve()
        if resolved != root and resolved.is_relative_to(root):
            return True
    return is_marked_run_dir(directory)


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


def check_inside_run_root(run_path: Path, run_root: Path) -> None:
    """Refuse a run directory xeda chose that does not resolve strictly inside the run root."""
    resolved, root = run_path.resolve(), run_root.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RunDirectoryError(
            f"The run directory {run_path} resolves to {resolved}, outside the run root {root}: "
            "xeda runs, and deletes files, only inside its run root. Remove the link that leads "
            "out of it."
        )
