"""The one walker of "every file under a directory" (R50 m): traces, directory listings, artifact
files, delivery and the package digest all list with `directory_files`.

Standard library only, so that every module that lists a directory can import it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

__all__ = ["VCS_METADATA", "directory_files"]

#: What version control keeps in a working copy, which no tool reads: left out of a listing of a
#: directory a setting names, so a commit or a fetch there is no change to a run.
VCS_METADATA = frozenset({".git", ".hg", ".svn"})


def _never(_name: str) -> bool:
    return False


def directory_files(
    directory: Path,
    prune: Iterable[Path] = (),
    skip: frozenset[str] = frozenset(),
    *,
    follow_links: bool = False,
    reserved: Callable[[str], bool] = _never,
) -> list[Path]:
    """Every entry under `directory`, recursively, in the resolved directory, sorted by name at
    each level: regular files, symbolic links (each as itself), subdirectories (each listed
    before what it holds) and special files (a FIFO, a socket: listed, never read). With
    `follow_links`, a link to a directory is listed and then walked, its entries by their path
    through it, each directory once (`(st_dev, st_ino)`), so a cycle ends. A directory in
    `prune` is not entered, however it is reached -- recognized by its `(st_dev, st_ino)`, not
    its path, so a link to an ancestor does not lead into it; an entry named in `skip`, at any
    depth, or `reserved` at the top, is left out. A directory that cannot be listed is an entry
    with nothing under it (`directory` too): it cannot be recorded (`digest.record_file`), so
    the next launch runs again."""
    top = Path(directory).resolve()
    pruned: set[tuple[int, int]] = set()
    for path in prune:
        try:
            st = os.stat(path)
        except OSError:
            continue
        pruned.add((st.st_dev, st.st_ino))
    found: list[Path] = []
    seen: set[tuple[int, int]] = set()

    def walk(path: Path) -> None:
        try:
            st = os.stat(path)
            key = (st.st_dev, st.st_ino)
            if key in seen or key in pruned:
                return
            seen.add(key)
            with os.scandir(path) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            if path == top:
                found.append(path)
            return
        for entry in entries:
            if (path == top and reserved(entry.name)) or entry.name in skip:
                continue
            child = path / entry.name
            if entry.is_symlink():
                found.append(child)
                if follow_links and entry.is_dir():
                    walk(child)  # returns at once for a pruned or an already walked directory
            elif entry.is_dir(follow_symlinks=False):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # gone since it was listed
                if (st.st_dev, st.st_ino) in pruned:
                    continue
                found.append(child)
                walk(child)
            else:
                found.append(child)

    walk(top)
    return found
