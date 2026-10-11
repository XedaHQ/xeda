"""Removing the run directories of earlier runs: `xeda scrub` and a launch's `--scrub`.

Both read the disk, never a design file, and remove a run directory only under its own lock
(`run_lock.run_dir_lock`). This module never imports `default_runner`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple, Optional

from ..console import console
from ..design import target_name_problem
from ..run_dir import (
    OutputState,
    RunDirectory,
    RunDirectoryError,
    record_output_state,
    run_directory_name,
    run_directory_problem,
)
from ..utils import unique
from .run_lock import run_dir_lock

log = logging.getLogger(__name__)


#: What a launch writes in every run directory it enters (the trace only after a success): a
#: directory holding one is a run directory, whatever it is called, and never a target's.
LAUNCH_DOCUMENTS = ("settings.json", "results.json", "trace.json")


#: The run records: what a launch writes when its run ends, as against `settings.json`, which it
#: writes when the run starts. A launch that finds its run fresh may write `trace.json` again, a
#: refresh, once its records have settled. A directory where these have changed was written to by a
#: launch since they were seen.
COMPLETION_DOCUMENTS = ("results.json", "trace.json")


class ScrubResult(NamedTuple):
    """What `scrub_design` did: the directories it searched, the run directories it removed
    (none if it found none or the removal was not confirmed), the ones it kept because their run
    records changed after the listing, the ones that were gone already when their turn came, and
    the links it did not list because they are no run directory (`run_directory_problem`). The
    removed, the kept and the gone are three lists: none is in another, and no skipped link is in
    any of them. A directory that has several names (links to it) is listed once, under the first
    name, and removed under all of them."""

    scanned: list[Path]
    removed: list[Path]
    kept: list[Path]
    gone: list[Path]
    skipped: list[Path]


class _Removal(NamedTuple):
    """What `_remove_confirmed` did with its candidates."""

    removed: list[Path]
    kept: list[Path]
    gone: list[Path]


def _distinct(paths: Iterable[Path]) -> list[Path]:
    """`paths` without a second name of a directory already in them (a link inside the design's
    directory), in order: what is searched or removed is each directory once."""
    first: dict[str, Path] = {}
    for p in paths:
        first.setdefault(os.path.realpath(p), p)
    return list(first.values())


class _Skipped(NamedTuple):
    """A link named like a run directory of the flow that is none (`run_directory_problem`), and
    why. Scrub neither lists it nor follows it."""

    link: Path
    problem: str


class _Scan(NamedTuple):
    """What `_run_directories_in` found in one directory: the run directories it lists, and the
    links named like one that it does not."""

    candidates: list[Path]
    skipped: list[_Skipped]


def _run_directories_in(
    flow_name: str, directory: Path, exclude: Sequence[Path], run_root: Path
) -> _Scan:
    """`flow_name`'s run directories among the children of `directory`, except `exclude`: a
    launch leaves out its own. A child is a run directory by `run_directory_problem`, the rule a
    launch applies too. A link named like one that fails the rule is not listed, and is returned
    with the reason. Nothing is listed in a directory that is not under `run_root`. The children
    are matched rather than a `f"{flow_name}_*"` glob, so the unhashed directory -- which that
    glob can never match -- is included too. A directory is excluded by where it resolves to,
    never by whether it exists now or has the same inode: a sibling launch's scrub may remove a
    launch's own directory while this lists, and it is still not a candidate."""
    if not directory.is_dir():
        return _Scan([], [])
    xr = directory.resolve()
    if not xr.is_relative_to(os.path.realpath(run_root)):
        return _Scan([], [])
    excluded = {Path(os.path.realpath(ex)) for ex in exclude}
    named = run_directory_name(flow_name)
    candidates: list[Path] = []
    skipped: list[_Skipped] = []
    for p in sorted(directory.iterdir()):
        if named.match(p.name) is None:
            continue
        problem = run_directory_problem(p, flow_name, xr)
        if problem is None:
            if Path(os.path.realpath(p)) not in excluded:
                candidates.append(p)
        elif p.is_symlink():
            skipped.append(_Skipped(p, problem))
    return _Scan(unique(candidates), skipped)


def run_directory_names(path: Path, flow_name: str, run_root: Path) -> list[Path]:
    """Every name of `flow_name`'s run directory `path` in its directory: `path` itself, and the
    links beside it that are run directories of the flow (`run_directory_problem`) and lead where
    it leads -- the names scrub lists it under (`_listed`). A purge removes all of them with the
    directory (`RunDirectory.delete`), so no name is left leading nowhere."""
    leads_to = os.path.realpath(path)
    beside = _run_directories_in(flow_name, path.parent, (), run_root).candidates
    return unique([path, *(p for p in beside if os.path.realpath(p) == leads_to)])


def _say_skipped(skipped: Iterable[_Skipped]) -> None:
    """Say, one line each, the links scrub did not list, and why: on the console, and in the log
    at info level, because `xeda scrub` configures no logging."""
    for link, problem in skipped:
        log.info("Not scrubbing %s: %s", link, problem)
        _say(f"skipped {link}: {problem}")


def _target_parents(design_dir: Path, run_root: Path) -> list[Path]:
    """The directories below `design_dir` that can hold a target's run directories: those that
    could be named as a target (`target_name_problem`: a name that is no flow's) and are no run
    directory (a launch writes `LAUNCH_DOCUMENTS` in every one), found as they are on disk --
    a target no design file names any more included -- and never searched deeper. A link
    leading out of the run root, or out of `design_dir`, is not followed."""
    if not design_dir.is_dir():
        return []
    xr = design_dir.resolve()
    return [
        p
        for p in sorted(design_dir.iterdir())
        if p.is_dir()
        and target_name_problem(p.name) is None
        and not any((p / name).exists() for name in LAUNCH_DOCUMENTS)
        and xr in p.resolve().parents
        and RunDirectory.lies_under(p, run_root)
    ]


class _Listed(NamedTuple):
    """A run directory as listed for removal: the path, the flow it is a run directory of, the
    resolved directory it was listed in and what the listing saw of its run records
    (`_completion_records`). Scrub judges the path again once it holds the lock
    (`_problem_now`), by what is there then, and compares the records to find a directory that a
    launch wrote its records to after the listing. `aliases` are the other names the listing
    found for the same directory (links to it, or the directory itself when `path` is a link):
    scrub removes the directory under every one of its names."""

    path: Path
    flow_name: str
    parent: Path
    records: tuple[Optional[OutputState], ...]
    aliases: tuple[Path, ...] = ()


def _completion_records(path: Path) -> tuple[Optional[OutputState], ...]:
    """What the run directory `path` holds of its run records (`COMPLETION_DOCUMENTS`): the state
    of each (`record_output_state`: identity, size and times), or None where it is not there. A
    launch that ends a run writes each one anew, made whole and renamed into place. A launch that
    finds its run fresh may write the trace again, once its records have settled. So the records
    differ when a launch has written them since they were last seen, and not when it only added
    other files to the directory. A record that was not there and is, counts as well."""
    return tuple(record_output_state(path / name) for name in COMPLETION_DOCUMENTS)


def _listed(flow_name: str, paths: Iterable[Path]) -> list[_Listed]:
    """`paths` as run directories to remove: one for each directory they lead to, under the first
    of its names, with the others as its `aliases`. A directory and a link to it are one
    candidate, so the link is removed with the directory and never left leading nowhere."""
    names: dict[str, list[Path]] = {}
    for p in paths:
        names.setdefault(os.path.realpath(p), []).append(p)
    return [
        _Listed(
            first,
            flow_name,
            Path(os.path.realpath(first.parent)),
            _completion_records(first),
            tuple(others),
        )
        for first, *others in names.values()
    ]


def _lock_path(path: Path, run_root: Path) -> Path:
    """What a candidate is locked by. A link to a directory inside the run root is locked by that
    directory: it stays one lock when another scrub, which held it, has removed the link and then
    the directory. Anything else is locked by its own path. That includes a link out of the run
    root or to nowhere: the lock refuses it, before it makes anything. A link may change while
    scrub waits for the lock, so scrub asks again once it holds the lock."""
    if path.is_symlink() and path.is_dir() and RunDirectory.lies_under(path, run_root):
        return Path(os.path.realpath(path))
    return path


def _refuse_a_change(path: Path, c: _Listed, locked: Path, leads_to: Path) -> None:
    """Under the lock of `locked`, judge a name of the directory again, as it is now: it still
    leads to the directory whose lock is held (`leads_to`, where `locked` leads), and is still
    a run directory of the flow where it was listed. Otherwise a `RunDirectoryError`, and
    nothing is removed: a name retargeted while scrub waited, or replaced, is not what the user
    confirmed, and scrub would remove a directory whose lock it does not hold."""
    if Path(os.path.realpath(path)) != leads_to:
        raise RunDirectoryError(
            f"{path} changed while scrub waited for its lock. It no longer leads to "
            f"{locked}, the directory whose lock scrub holds. It was not removed."
        )
    problem = _problem_now(c._replace(path=path))
    if problem is not None:
        raise RunDirectoryError(
            f"{path} is no longer a run directory of {c.flow_name} in {c.parent}: "
            f"{problem}. It was not removed."
        )


def _is_gone(path: Path) -> bool:
    """Whether nothing is at `path`: its name, or a directory above it, does not exist. A link is
    something, wherever it leads. An error that is no `FileNotFoundError` says nothing, so it is
    not "gone"."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        pass  # it cannot be told: the judgment that follows refuses it
    return False


def _problem_now(listed: _Listed) -> Optional[str]:
    """Why `listed.path` is no run directory of its flow in the directory it was listed in
    (`run_directory_problem`, the rule that listed it), or None if it is. Scrub asks once it
    holds the lock, so no launch is running in it.

    What the directory held when it was listed does not matter, and nor does whether it is the
    same directory. A launch writes in its run directory, and its inode change time moves with
    every file it adds or removes. Whether it wrote the run records there since the listing is
    another question, which `_completion_records` answers."""
    try:
        return run_directory_problem(listed.path, listed.flow_name, listed.parent)
    except OSError as error:
        return f"it cannot be examined ({error.strerror or error})"


def _say(message: str) -> None:
    """One line of what scrub did, as it is: not folded inside a path, and no part of it taken
    for markup."""
    console.print(message, markup=False, highlight=False, soft_wrap=True)


def _remove_confirmed(candidates: Sequence[_Listed], run_root: Path) -> _Removal:
    """List `candidates`, ask once, and remove them if confirmed: each as a run directory
    claimed under `run_root`, the real run root, under its own lock. The lock is waited for, so
    scrub never removes a directory while a launch runs in it. It is refused for a directory
    reached through a link out of the run root.

    Scrub removes the runs it listed, which are the runs that were confirmed. Once it holds a
    candidate's lock it looks at what is at the path. Nothing there any more (`_is_gone`: another
    scrub, or a purge, removed it while this one waited) is skipped, and said: the scrub wanted
    it gone. A candidate that no longer leads to the directory whose lock is held (`_lock_path`
    asked again: a link retargeted, or replaced by a directory, while scrub waited) is not
    removed, and the scrub fails (`RunDirectoryError`): scrub would remove a directory whose lock
    it does not hold. What is removed is the locked directory itself, never what a link leads to
    by then. Anything else that is no run directory of the flow by `run_directory_problem` -- no
    directory, or a link that now leads anywhere but to a run directory of the flow beside it --
    is not removed either, and the scrub fails the same way. This holds for every name of the
    directory (`_Listed.aliases`), and is judged before the run records. A run directory whose
    run records are not what the listing saw (`_completion_records`) was written to by a launch after the
    listing: a run ended there, or a launch found its run fresh and refreshed the trace. Scrub
    cannot tell which, and what is there is no longer what was confirmed: it is kept, and said.
    The rest are removed. The three lists are the directories removed, kept and gone."""
    if not candidates:
        return _Removal([], [], [])
    console.print(
        f"[red]This will remove all of the following {len(candidates)} run directories:[/red]"
    )
    for c in candidates:
        console.print(c.path)
        for alias in c.aliases:
            _say(f"{alias} (another name of the same directory)")
    confirmation = console.input("Type 'yes' if you're sure you want to continue: ")
    if confirmation.lower() != "yes":
        console.print("Not confirmed. No files or folders were removed.")
        return _Removal([], [], [])
    log.warning(
        "Removing the following directories: %s",
        " ".join(str(name) for c in candidates for name in (c.path, *c.aliases)),
    )
    done = _Removal([], [], [])
    for c in candidates:
        p = c.path
        locked = _lock_path(p, run_root)
        leads_to = Path(os.path.realpath(locked))
        with run_dir_lock(locked, run_root):
            if _is_gone(p):
                log.info("Not removing %s: it is gone already", p)
                _say(f"{p} is gone already")
                done.gone.append(p)
                continue
            _refuse_a_change(p, c, locked, leads_to)
            for alias in c.aliases:  # each name of the directory, judged as the first is
                if not _is_gone(alias):
                    _refuse_a_change(alias, c, locked, leads_to)
            if _completion_records(p) != c.records:
                log.info("Not removing %s: its run records changed after the listing", p)
                _say(
                    f"kept {p}: its run records changed after the listing "
                    "(a run finished or refreshed there)"
                )
                done.kept.append(p)
                continue
            # The links first, as themselves: another scrub that listed one then finds it gone,
            # and a link to a directory that is gone is a link to nowhere, which its lock
            # refuses. Then the directory whose lock is held, never what a link leads to by now.
            RunDirectory.claimed(locked, run_root).delete(p, *c.aliases)
        done.removed.append(p)
    summary = f"{len(done.removed)} folders removed"
    if done.kept:
        summary += f", {len(done.kept)} kept"
    if done.gone:
        summary += f", {len(done.gone)} gone already"
    _say(summary + ".")
    return done


def scrub_runs(
    flow_name: str,
    dir: Path,
    exclude: Sequence[Path] = (),
    run_root: Optional[Path] = None,
) -> bool:
    """Find and (with confirmation) remove `flow_name`'s run directories directly in `dir`,
    the ones named `flow_name` or `flow_name_<hash>` (`_run_directories_in`), except `exclude`.
    A launch's `--scrub` scrubs its own directory this way, so it never reaches another
    target's. Each is removed as a run directory claimed under `run_root` (default: `dir`'s
    parent), the real run root, so none that leads out of it is ever removed, together with the
    links to it that the listing found (`_listed`). A link named like a run directory that is
    none is said, and left alone."""
    if run_root is None:
        run_root = dir.parent
    scan = _run_directories_in(flow_name, dir, exclude, run_root)
    _say_skipped(scan.skipped)
    listed = _listed(flow_name, scan.candidates)
    return bool(_remove_confirmed(listed, run_root).removed)


def scrub_design(
    flow_name: str, design_dir: Path, *, run_root: Path, target: str | None = None
) -> ScrubResult:
    """Find and (with one confirmation) remove `flow_name`'s run directories of the design whose
    directory is `design_dir`, under `run_root`: without `target`, those directly in it (the
    ones made before targets existed) and in every target's directory below it; with one, only
    those in `<design_dir>/<target>`. What is on disk decides (`_target_parents`); no design
    file is read, so a target the design no longer names is found, and one that was never built
    is nothing to do. A link named like a run directory that is none (`run_directory_problem`)
    is not listed: it is said before the question, and returned as `skipped`. A `target` that is
    no target name is a `RunDirectoryError`, before anything is looked at."""
    if target is not None:
        problem = target_name_problem(target)
        if problem is not None:
            raise RunDirectoryError(problem)
        parent = design_dir / target
        xr = design_dir.resolve()
        found = (
            [parent]
            if parent.is_dir()
            and xr in parent.resolve().parents
            and RunDirectory.lies_under(parent, run_root)
            else []
        )
    else:
        found = [design_dir, *_target_parents(design_dir, run_root)] if design_dir.is_dir() else []
    parents = _distinct(found)
    scans = [_run_directories_in(flow_name, parent, (), run_root) for parent in parents]
    skipped = unique([s for scan in scans for s in scan.skipped])
    candidates = [p for scan in scans for p in scan.candidates]
    _say_skipped(skipped)
    done = _remove_confirmed(_listed(flow_name, candidates), run_root)
    return ScrubResult(parents, done.removed, done.kept, done.gone, [s.link for s in skipped])
