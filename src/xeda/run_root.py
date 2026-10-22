"""The run root: a directory that Xeda created and marked as its own.

Every run directory lies in a run root. `--run-root`, `XEDA_RUN_ROOT` or the `run_root` of the
launcher name it, and `./xeda_run` is the default. Xeda works only in a run root that it created
or that was empty when Xeda first used it. It marks the run root (`RUN_ROOT_MARKER`) and adds a
`.gitignore` of `*` and a `CACHEDIR.TAG`, so version control and backup tools leave it alone.
Everything under a marked run root belongs to Xeda: keep nothing of yours there.

A directory that holds files and has no marker does not belong to Xeda. Xeda refuses it before
anything runs and names the directory and the fix. The one exception is the default,
`./xeda_run` in the start directory. Xeda adopts that directory once, marks it, and logs an info
line. Creating `.xeda-run-root` in a directory hands it to Xeda.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from . import errors
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
GITIGNORE = "# A run root of Xeda: everything here is Xeda's own\n*\n"
#: The tag file from https://bford.info/cachedir/. Backup tools skip a directory that holds it.
CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by Xeda.\n"
    "# For information about cache directory tags, see https://bford.info/cachedir/\n"
)


class RunRootError(errors.RunRootError):
    """A directory named as the run root that Xeda cannot take for its own."""

    def __init__(self, message: str, path: Path) -> None:
        super().__init__(message, code="invalid_run_root", path=path)


def is_run_root(path: Path) -> bool:
    """Return whether `path` holds the marker of a run root: a regular file with any content."""
    marker = Path(path) / RUN_ROOT_MARKER
    return marker.is_file() and not marker.is_symlink()


def is_default_location(path: Path, start: Path) -> bool:
    """Return whether `path` is the default run root.

    That is `xeda_run` directly in the start directory `start`. Any other name does not count,
    `xeda_run_dse` and `xeda_run_<optimizer>` included.
    """
    path, start = Path(os.path.realpath(path)), Path(os.path.realpath(start))
    return path.parent == start and path.name == DEFAULT_RUN_ROOT


def _create_exclusively(path: Path, text: str) -> bool:
    """Write `text` to `path` if nothing is there. Return False if something is."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def _mark(root: Path) -> None:
    """Mark `root` as a run root.

    Write the marker first, with exclusive creation, so a concurrent Xeda that finds the
    directory holding files finds the marker among them. Then write the ignore files that are
    absent.
    """
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
    """Validate the resolved run root and, with `create`, make it ready for Xeda.

    Return a root that is already marked unchanged. An absent root, an empty root and the
    unmarked default root are created and marked only with `create`. Without `create`, return
    None for them, and the launcher prepares the root on its first use. Refuse every other
    unmarked root before anything is written.
    """
    root = Path(path).resolve()
    start = Path.cwd() if start is None else Path(start)
    if root.exists() and not root.is_dir():
        raise RunRootError(
            f"{root} is not a directory: the run root is the directory Xeda keeps its runs in",
            root,
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
        if not create:
            return None
        _mark(root)
        return root
    if is_default_location(root, start):
        if not create:
            return None
        _mark(root)
        log.info(
            "Xeda keeps its runs in %s, the default run root: it is marked as Xeda's "
            "(%s); keep nothing of yours in it",
            root,
            RUN_ROOT_MARKER,
        )
        return root
    raise RunRootError(
        f"{root} holds files and is not a run root Xeda created (it has no {RUN_ROOT_MARKER}): "
        "Xeda keeps its runs in a directory of its own. Name a new or empty directory with "
        f"--run-root; if {root} holds nothing but runs of Xeda, delete them, or "
        f"create {root / RUN_ROOT_MARKER} to hand the directory to Xeda.",
        root,
    )
