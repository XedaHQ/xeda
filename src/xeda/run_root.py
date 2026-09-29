"""xeda's space: a run root it created and marked (D21).

Every run directory lies in a run root: `--run-root`, `XEDA_RUN_ROOT` or the launcher's
`run_root`, `./xeda_run` by default. xeda works only in a run root it created, or one that was
empty when it first used it, and marks it so (`RUN_ROOT_MARKER`), with a `.gitignore` of `*` and a
`CACHEDIR.TAG`, so that version control and backup tools leave it alone. Everything under a marked
run root is xeda's: keep nothing of yours there. A directory that holds files and no marker is not
xeda's, and is refused before anything runs, naming it and the fix -- except the default,
`./xeda_run` at the start directory, which an earlier xeda made: that one is adopted, once, with
an info line. Creating `.xeda-run-root` in a directory hands it to xeda.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .utils import XedaException
from .version import __version__

log = logging.getLogger(__name__)

__all__ = [
    "CACHEDIR_TAG",
    "DEFAULT_RUN_ROOT",
    "GITIGNORE",
    "RUN_ROOT_FORMAT",
    "RUN_ROOT_MARKER",
    "RunRootError",
    "ensure_run_root",
    "is_default_location",
    "is_run_root",
]

#: The marker that makes a directory a run root; any regular file by this name does.
RUN_ROOT_MARKER = ".xeda-run-root"
RUN_ROOT_FORMAT = 1
DEFAULT_RUN_ROOT = "xeda_run"
GITIGNORE = "# a run root of xeda's: everything here is xeda's own\n*\n"
#: https://bford.info/cachedir/: backup tools skip a directory holding this file
CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by xeda.\n"
    "# For information about cache directory tags, see https://bford.info/cachedir/\n"
)


class RunRootError(XedaException):
    """A directory named as the run root that xeda cannot take for its own."""


def is_run_root(path: Path) -> bool:
    """Whether `path` holds the marker of a run root: a regular file, whatever it holds."""
    marker = Path(path) / RUN_ROOT_MARKER
    return marker.is_file() and not marker.is_symlink()


def is_default_location(path: Path, start: Path) -> bool:
    """Whether `path` is the default run root an earlier xeda made: `xeda_run`, directly in the
    start directory `start` -- nothing else, `xeda_run_dse` and `xeda_run_<optimizer>` included."""
    path, start = Path(os.path.realpath(path)), Path(os.path.realpath(start))
    return path.parent == start and path.name == DEFAULT_RUN_ROOT


def _create_exclusively(path: Path, text: str) -> bool:
    """Write `text` to `path` if nothing is there; False if something is."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def _mark(root: Path) -> None:
    """The marker first, exclusively, so that a concurrent xeda that finds the directory holding
    files finds the marker among them; then the ignore files, where there are none."""
    marker = {
        "format": RUN_ROOT_FORMAT,
        "created_by": f"xeda {__version__}",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _create_exclusively(root / RUN_ROOT_MARKER, json.dumps(marker, indent=2) + "\n")
    _create_exclusively(root / ".gitignore", GITIGNORE)
    _create_exclusively(root / "CACHEDIR.TAG", CACHEDIR_TAG)


def ensure_run_root(
    path: Union[str, os.PathLike],
    *,
    start: Optional[Path] = None,
    create: bool = True,
) -> Optional[Path]:
    """The run root `path`, resolved, made ready for xeda (see the module): created and marked if
    absent (None instead, without `create`), marked if empty or at the default location of the
    start directory `start` (the current directory by default), used as it is if marked; a
    `RunRootError` otherwise, before anything is written."""
    root = Path(path).resolve()
    start = Path.cwd() if start is None else Path(start)
    if root.exists() and not root.is_dir():
        raise RunRootError(
            f"{root} is not a directory: the run root is the directory xeda keeps its runs in"
        )
    if not root.exists():
        if not create:
            return None
        root.mkdir(parents=True, exist_ok=True)
        _mark(root)
        return root
    if is_run_root(root):
        return root
    if not any(root.iterdir()):
        _mark(root)
        return root
    if is_default_location(root, start):
        _mark(root)
        log.info(
            "xeda now keeps its runs in %s, which an earlier xeda made: it is marked as xeda's "
            "(%s); keep nothing of yours in it",
            root,
            RUN_ROOT_MARKER,
        )
        return root
    raise RunRootError(
        f"{root} holds files and is not a run root xeda created (it has no {RUN_ROOT_MARKER}): "
        "xeda keeps its runs in a directory of its own. Name a new or empty directory with "
        f"--run-root; if {root} holds nothing but runs of an earlier xeda, delete them, or "
        f"create {root / RUN_ROOT_MARKER} to hand the directory to xeda."
    )
