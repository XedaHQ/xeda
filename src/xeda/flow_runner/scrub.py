"""Removal of the run directories of earlier runs: `xeda scrub` and `--scrub` of a launch.

Both read the disk, never a design file, and remove a run directory only under its own lock
(`run_lock.run_dir_lock`). This module never imports `default_runner`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from ..design import DESIGN_NAME, target_name_problem
from ..run_dir import (
    DIR_NAME_HASH_LEN,
    OutputState,
    RunDirectory,
    RunDirectoryError,
    record_output_state,
    refuse_link_on_way,
    run_directory_name,
    run_directory_problem,
)
from ..cancel import launch_canceled
from ..generation import GENERATION_DIRECTORY
from ..types import UNSET, Unset
from ..utils import Canceled, unique
from .run_lock import refuse_if_held, run_dir_lock, storage_lock

log = logging.getLogger(__name__)


class ScrubCandidate(NamedTuple):
    """A run directory that scrub lists for removal: the path it lists it under, and the other
    names of the same directory (links to it), which go with it."""

    path: Path
    aliases: tuple[Path, ...]


#: The answer of the user to the question whether to remove the run directories that scrub
#: listed. It gets the directories (one candidate for each, with its other names) and returns
#: whether to remove them. The library prints nothing, so the callback shows the listing where it
#: asks. Only the command line asks the user. A caller that gives none gets no removal: an
#: unanswered question answers "no".
ScrubConfirmation = Callable[[Sequence[ScrubCandidate]], bool]

#: Told, when a scrub ends, what it did (`ScrubResult`). The command line shows it.
ScrubDone = Callable[["ScrubResult"], None]


#: The files that a launch writes in every run directory it enters (the trace only after a
#: passed or rejected run). A directory that holds one is a run directory, whatever it is called,
#: and never the directory of a target.
LAUNCH_DOCUMENTS = ("settings.json", "results.json", "trace.json")


#: The run records: what a launch writes when its run ends, as against `settings.json`, which it
#: writes when the run starts. A launch that finds its run up to date may write `trace.json`
#: again (a refresh) once its records have settled. A directory where these have changed was
#: written to by a launch since they were seen.
COMPLETION_DOCUMENTS = ("results.json", "trace.json")


class ScrubResult(NamedTuple):
    """What `scrub_design` did.

    `scanned` lists the directories it searched. `removed` lists the run directories it removed
    (none if it found none or the removal was not confirmed). `kept` lists those it kept because
    their run records changed after the listing. `gone` lists those that were gone already when
    their turn came. `skipped` lists the links it did not list because they are no run directory
    (`run_directory_problem`). The removed, kept and gone directories are three separate lists:
    none is in another, and no skipped link is in any of them. A directory that has several
    names (links to it) is listed once, under the first name, and removed under all of them."""

    scanned: list[Path]
    removed: list[Path]
    kept: list[Path]
    gone: list[Path]
    skipped: list[Path]
    #: Why each skipped link was skipped.
    reasons: Mapping[Path, str] = MappingProxyType({})


class _Removal(NamedTuple):
    """What `_remove_confirmed` did with its candidates."""

    removed: list[Path]
    kept: list[Path]
    gone: list[Path]


def _distinct(paths: Iterable[Path]) -> list[Path]:
    """Return `paths` without a second name of a directory already in them, in order.

    A second name is a link inside the directory of the design. Each directory is then searched
    or removed once."""
    first: dict[str, Path] = {}
    for p in paths:
        first.setdefault(os.path.realpath(p), p)
    return list(first.values())


class _Skipped(NamedTuple):
    """A link named like a run directory of the flow that is none, and the reason.

    The reason comes from `run_directory_problem`. Scrub neither lists the link nor follows it."""

    link: Path
    problem: str


class _Scan(NamedTuple):
    """What `_run_directories_in` found in one directory.

    That is the run directories it lists, and the links named like one that it does not list."""

    candidates: list[Path]
    skipped: list[_Skipped]


#: The name of a hashed run directory: the flow, an underscore and the run hash.
_HASHED_NAME = re.compile(r"^(?P<flow>.+)_[a-z0-9]{%d}\Z" % DIR_NAME_HASH_LEN)


def _flow_name_of(directory_name: str) -> str:
    """Return the flow that a run directory of this name belongs to: the name without its hash."""
    named = _HASHED_NAME.match(directory_name)
    return named["flow"] if named else directory_name


def _holds_launch_documents(path: Path) -> bool:
    return any((path / name).exists() for name in LAUNCH_DOCUMENTS)


def _run_directories_in(
    flow_name: str | None, directory: Path, exclude: Sequence[Path], run_root: Path
) -> _Scan:
    """Return the run directories of `flow_name` among the children of `directory`.

    The directories in `exclude` are left out.

    A launch leaves out its own. A child is a run directory by `run_directory_problem`, the
    rule that a launch applies too. A link named like a run directory that fails the rule is
    not listed, and is returned with the reason. Nothing is listed in a directory that is not
    under `run_root`. The children are matched, and not with a `f"{flow_name}_*"` glob, so that
    the unhashed directory, which that glob can never match, is included too. A directory is
    excluded by where it resolves to, never by whether it exists now or has the same inode: the
    scrub of a sibling launch may remove the own directory of a launch while this function
    lists, and it is still not a candidate.

    Without `flow_name`, the run directories of every flow are listed. A child is then a run
    directory of the flow that its name says if it holds a file that a launch writes
    (`LAUNCH_DOCUMENTS`): a directory without one may be the directory of a target."""
    if not directory.is_dir():
        return _Scan([], [])
    xr = directory.resolve()
    if not xr.is_relative_to(os.path.realpath(run_root)):
        return _Scan([], [])
    excluded = {Path(os.path.realpath(ex)) for ex in exclude}
    named = None if flow_name is None else run_directory_name(flow_name)
    candidates: list[Path] = []
    skipped: list[_Skipped] = []
    for p in sorted(directory.iterdir()):
        if named is not None and named.match(p.name) is None:
            continue
        if named is None and not _holds_launch_documents(p):
            continue
        flow = flow_name if flow_name is not None else _flow_name_of(p.name)
        problem = run_directory_problem(p, flow, xr)
        if problem is None:
            if Path(os.path.realpath(p)) not in excluded:
                candidates.append(p)
        elif p.is_symlink():
            skipped.append(_Skipped(p, problem))
    return _Scan(unique(candidates), skipped)


def run_directory_names(path: Path, flow_name: str, run_root: Path) -> list[Path]:
    """Return every name of the run directory `path` of `flow_name` in its directory.

    That is `path` itself and the links beside it that are run directories of the flow
    (`run_directory_problem`) and lead where it leads: the names under which scrub lists it
    (`_listed`). A purge removes all of them with the directory (`RunDirectory.delete`), so no
    name is left leading nowhere."""
    leads_to = os.path.realpath(path)
    beside = _run_directories_in(flow_name, path.parent, (), run_root).candidates
    return unique([path, *(p for p in beside if os.path.realpath(p) == leads_to)])


def _log_skipped(skipped: Iterable[_Skipped]) -> None:
    """Log, one record each, which links scrub did not list, and why. The result holds them too."""
    for link, problem in skipped:
        log.info("Not scrubbing %s: %s", link, problem)


def _target_parents(design_dir: Path) -> _Scan:
    """Return the directories below `design_dir` that can hold the run directories of a target.

    They could be named as a target (`target_name_problem`: a name that is no flow's) and are no
    run directory (a launch writes `LAUNCH_DOCUMENTS` in every one). They are found as they are
    on disk, a target that no design file names any more included, and never searched deeper.
    `design_dir` is no symbolic link (`refuse_link_on_way`). A link to a directory that fits
    the description is not listed, because Xeda neither follows nor removes a link at the level
    of a target. It is returned with the reason, as `skipped`."""
    if not design_dir.is_dir():
        return _Scan([], [])
    parents: list[Path] = []
    skipped: list[_Skipped] = []
    for p in sorted(design_dir.iterdir()):
        if (
            p.is_dir()
            and target_name_problem(p.name) is None
            and not any((p / name).exists() for name in LAUNCH_DOCUMENTS)
        ):
            if p.is_symlink():
                skipped.append(_Skipped(p, _link_problem(p)))
            else:
                parents.append(p)
    return _Scan(parents, skipped)


def _link_problem(link: Path) -> str:
    """Return why scrub does not list a symbolic link at the level of a design or target."""
    return f"it is a symbolic link (to {os.readlink(link)}), which Xeda neither follows nor removes"


class _Listed(NamedTuple):
    """A run directory as listed for removal.

    It holds the path, the flow it is a run directory of, the resolved directory it was listed
    in, and what the listing saw of its run records (`_completion_records`). Scrub judges the
    path again once it holds the lock (`_problem_now`), by what is there then, and compares the
    records to find a directory that a launch wrote its records to after the listing.
    `aliases` are the other names that the listing found for the same directory (links to it,
    or the directory itself when `path` is a link). Scrub removes the directory under every one
    of its names."""

    path: Path
    flow_name: str
    parent: Path
    records: tuple[Optional[OutputState], ...]
    aliases: tuple[Path, ...] = ()


def _completion_records(path: Path) -> tuple[Optional[OutputState], ...]:
    """Return the state of the run records of the run directory `path` (`COMPLETION_DOCUMENTS`).

    The state of a record is `record_output_state` (identity, size and times), or None where the
    record is not there. A launch that ends a run writes each record anew, made whole and
    renamed into place. A launch that finds its run up to date may write the trace again, once
    its records have settled. So the records differ when a launch has written them since they
    were last seen, and not when it only added other files to the directory. A record that was
    not there and is now counts as a difference too."""
    return tuple(record_output_state(path / name) for name in COMPLETION_DOCUMENTS)


def _listed(flow_name: str | None, paths: Iterable[Path]) -> list[_Listed]:
    """Return `paths` as run directories to remove.

    There is one for each directory that the paths lead to, under the first of its names, with
    the other names as its `aliases`. A directory and a link to it are one candidate, so the
    link is removed with the directory and never left leading nowhere. Without `flow_name`, the
    flow of each directory is the one that its name says."""
    names: dict[str, list[Path]] = {}
    for p in paths:
        names.setdefault(os.path.realpath(p), []).append(p)
    return [
        _Listed(
            first,
            flow_name or _flow_name_of(first.name),
            Path(os.path.realpath(first.parent)),
            _completion_records(first),
            tuple(others),
        )
        for first, *others in names.values()
    ]


def _lock_path(path: Path, run_root: Path) -> Path:
    """Return what a candidate is locked by.

    A link to a directory inside the run root is locked by that directory, so it stays one lock
    when another scrub, which held it, has removed the link and then the directory. Anything
    else is locked by its own path. That includes a link out of the run root or to nowhere: the
    lock refuses it, before it makes anything. A link may change while scrub waits for the
    lock, so scrub asks again once it holds the lock."""
    if path.is_symlink() and path.is_dir() and RunDirectory.lies_under(path, run_root):
        return Path(os.path.realpath(path))
    return path


def _refuse_a_change(path: Path, c: _Listed, locked: Path, leads_to: Path) -> None:
    """Judge a name of the directory again, as it is now, under the lock of `locked`.

    The name must still lead to the directory whose lock is held (`leads_to`, where `locked`
    leads) and must still be a run directory of the flow where it was listed. Otherwise a
    `RunDirectoryError` is raised and nothing is removed: a name retargeted while scrub waited,
    or replaced, is not what the user confirmed, and scrub would remove a directory whose lock
    it does not hold."""
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
    """Return whether nothing is at `path`: its name, or a directory above it, does not exist.

    A link is something, wherever it leads. An error that is no `FileNotFoundError` says
    nothing, so it is not "gone"."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        pass  # it cannot be told: the judgment that follows refuses it
    return False


def _problem_now(listed: _Listed) -> Optional[str]:
    """Return why `listed.path` is no run directory of its flow in its listing directory.

    Return None if it is one. The reason comes from `run_directory_problem`, the rule that
    listed it. Scrub asks once it holds the lock, so no launch is running in the directory.

    What the directory held when it was listed does not matter, and nor does whether it is the
    same directory. A launch writes in its run directory, and its inode change time moves with
    every file it adds or removes. Whether it wrote the run records there since the listing is
    another question, which `_completion_records` answers."""
    try:
        return run_directory_problem(listed.path, listed.flow_name, listed.parent)
    except OSError as error:
        return f"it cannot be examined ({error.strerror or error})"


def _stop_if_canceled(path: Path) -> None:
    """Raise `Canceled` when the launch that scrubs must stop: nothing more is removed."""
    if launch_canceled():
        raise Canceled(f"the launch was canceled before it removed {path}")


def _remove_confirmed(
    candidates: Sequence[_Listed],
    run_root: Path,
    confirm: Optional[ScrubConfirmation] = None,
    storages: Sequence[Path] = (),
) -> _Removal:
    """List `candidates`, ask `confirm` once, and remove them if it says yes.

    Without `confirm`, nothing is asked and nothing is removed: the library never prompts.
    It logs once what it did not remove. With `confirm`, the callback shows the listing: it gets
    the directories to remove, and the library logs the other names of each.

    Each candidate is removed as a run directory claimed under `run_root`, the real run root,
    under its own lock. The lock is waited for, so scrub never removes a directory while a
    launch runs in it. The lock is refused for a directory reached through a link out of the run
    root.

    Scrub removes the runs it listed, which are the runs that were confirmed. Once it holds the
    lock of a candidate, it looks at what is at the path. If nothing is there any more
    (`_is_gone`: another scrub, or a purge, removed it while this one waited), scrub skips the
    candidate and logs it, because it wanted the directory gone. If the candidate no longer
    leads to the directory whose lock is held (`_lock_path` asked again: a link retargeted, or
    replaced by a directory, while scrub waited), scrub removes nothing and fails with a
    `RunDirectoryError`, because it would remove a directory whose lock it does not hold. What
    is removed is the locked directory itself, never what a link leads to by then. Scrub fails
    the same way for anything else that is no run directory of the flow by
    `run_directory_problem` (no directory, or a link that now leads anywhere but to a run
    directory of the flow beside it). This holds for every name of the directory
    (`_Listed.aliases`) and is judged before the run records.

    If the run records of a run directory are not what the listing saw (`_completion_records`),
    a launch wrote to the directory after the listing: a run ended there, or a launch found its
    run up to date and refreshed the trace. Scrub cannot tell which, and what is there is no
    longer what was confirmed, so it keeps the directory and logs it. It removes the rest. The
    result holds three lists: the directories removed, kept and gone.

    A launch that scrubs (`--scrub`, `prune_variants`) stops when it is canceled
    (`cancel.launch_canceled`): the wait for a lock ends, and no directory is removed after the
    request. The directories removed before stay removed.

    `storages` are generation storages (`scrub_generations`) that the same confirmation covers.
    They are removed after the run directories, each under its exclusive lock, and they are
    reported in `removed`, or in `gone` if another scrub removed them first."""
    if not candidates and not storages:
        return _Removal([], [], [])
    if confirm is None:
        log.warning(
            "Not removing %d run director%s%s (%s): nothing confirmed the removal, and only the "
            "command line asks the user.",
            len(candidates),
            "y" if len(candidates) == 1 else "ies",
            f" and {len(storages)} generation storage(s)" if storages else "",
            ", ".join([*(str(c.path) for c in candidates), *map(str, storages)]),
        )
        return _Removal([], [], [])
    shown = [ScrubCandidate(c.path, tuple(c.aliases)) for c in candidates]
    if not confirm([*shown, *(ScrubCandidate(storage, ()) for storage in storages)]):
        return _Removal([], [], [])
    log.warning(
        "Removing the following directories: %s",
        " ".join(
            str(name)
            for name in (*(n for c in candidates for n in (c.path, *c.aliases)), *storages)
        ),
    )
    done = _Removal([], [], [])
    for c in candidates:
        p = c.path
        _stop_if_canceled(p)
        locked = _lock_path(p, run_root)
        leads_to = Path(os.path.realpath(locked))
        with run_dir_lock(locked, run_root):
            # The wait for the lock may have ended because the lock came free, after the request.
            _stop_if_canceled(p)
            if _is_gone(p):
                log.info("Not removing %s: it is gone already", p)
                done.gone.append(p)
                continue
            _refuse_a_change(p, c, locked, leads_to)
            for alias in c.aliases:  # each name of the directory, judged as the first is
                if not _is_gone(alias):
                    _refuse_a_change(alias, c, locked, leads_to)
            if _completion_records(p) != c.records:
                log.info("Not removing %s: its run records changed after the listing", p)
                done.kept.append(p)
                continue
            # The links first, as themselves: another scrub that listed one then finds it gone,
            # and a link to a directory that is gone is a link to nowhere, which its lock
            # refuses. Then the directory whose lock is held, never what a link leads to by now.
            RunDirectory.claimed(locked, run_root).delete(p, *c.aliases)
        done.removed.append(p)
    if storages:
        removed, gone = _remove_storages(storages, run_root)
        done.removed.extend(removed)
        done.gone.extend(gone)
    return done


def _rooted_path(path: Path, given_root: Path, resolved_root: Path) -> Path:
    """Replace only the lexical run-root prefix, preserving links below it for the checks."""
    absolute = Path(os.path.abspath(path))
    root = Path(os.path.abspath(given_root))
    return resolved_root / absolute.relative_to(root) if absolute.is_relative_to(root) else absolute


def scrub_runs(
    flow_name: str,
    dir: Path,
    exclude: Sequence[Path] = (),
    run_root: Optional[Path] = None,
    *,
    confirm: Optional[ScrubConfirmation] = None,
    on_result: Optional[ScrubDone] = None,
) -> bool:
    """Find the run directories of `flow_name` directly in `dir` and remove them, with confirmation.

    `confirm` answers the question (`ScrubConfirmation`). Without it, nothing is removed.
    `on_result` is told what scrub did, when it ends (`ScrubDone`).

    They are the directories named `flow_name` or `flow_name_<hash>` (`_run_directories_in`),
    except `exclude`. The `--scrub` option of a launch scrubs its own directory this way, so it
    never reaches the directory of another target. Each directory is removed as a run directory
    claimed under `run_root` (default: the parent of `dir`), the real run root, so none that
    leads out of it is ever removed. The links to it that the listing found (`_listed`) go with
    it. A link named like a run directory that is none is logged and left alone.

    Scrub takes one exclusive lock at a time and waits for it, so a thread that holds a lock of
    Xeda is refused (`LeaseHeldError`)."""
    refuse_if_held()
    if run_root is None:
        run_root = dir.parent
    root = run_root.resolve()
    dir = _rooted_path(dir, run_root, root)
    run_root = root
    scan = _run_directories_in(flow_name, dir, exclude, run_root)
    _log_skipped(scan.skipped)
    if not scan.candidates:
        log.info("There is no other run directory of %s in %s to remove.", flow_name, dir)
    listed = _listed(flow_name, scan.candidates)
    done = _remove_confirmed(listed, run_root, confirm)
    if on_result is not None:
        on_result(
            ScrubResult(
                [dir],
                done.removed,
                done.kept,
                done.gone,
                [s.link for s in scan.skipped],
                {s.link: s.problem for s in scan.skipped},
            )
        )
    return bool(done.removed)


def scrub_design(
    flow_name: str | None,
    design_dir: Path,
    *,
    run_root: Path,
    target: str | None = None,
    design_only: bool = False,
    confirm: Optional[ScrubConfirmation] = None,
) -> ScrubResult:
    """Find the run directories of `flow_name` of a design under `run_root`, and remove them.

    `design_dir` is the directory of the design. One confirmation covers all directories. It
    comes from `confirm` (`ScrubConfirmation`); without it, nothing is removed.
    Without `target`, the run directories are those directly in `design_dir` (the ones made
    without a target) and in the directory of every target below it. With `target`, they
    are only those in `<design_dir>/<target>`. With `design_only`, they are only those directly
    in `design_dir`. What is on disk decides (`_target_parents`). No
    design file is read, so a target that the design no longer names is found, and one that was
    never built is nothing to do. A link named like a run directory that is none
    (`run_directory_problem`) is not listed: it is logged, and returned as `skipped` with its
    reason. A `target` that is no target name is a `RunDirectoryError`, before anything is
    looked at. So is a symbolic link at the design or the named target (`refuse_link_on_way`);
    the error names it. A link found at a target below the design is not followed: it is logged
    and returned as `skipped`.

    Without `flow_name`, the run directories of every flow are removed, and so are the generation
    storages of the design (`<run root>/.generated/<design>/<key>`, see `scrub_generations`),
    under the same confirmation. A link where the storages lie is a `RunDirectoryError` before
    anything is removed.

    Scrub takes one exclusive lock at a time and waits for it, so a thread that holds a lock of
    Xeda is refused (`LeaseHeldError`)."""
    refuse_if_held()
    root = run_root.resolve()
    design_dir = _rooted_path(design_dir, run_root, root)
    run_root = root
    found_skipped: list[_Skipped] = []
    if target is not None:
        problem = target_name_problem(target)
        if problem is not None:
            raise RunDirectoryError(problem)
        refuse_link_on_way(design_dir / target, run_root)
        parent = design_dir / target
        found = [parent] if parent.is_dir() else []
    elif design_only:
        refuse_link_on_way(design_dir, run_root)
        found = [design_dir] if design_dir.is_dir() else []
    else:
        refuse_link_on_way(design_dir, run_root)
        targets = _target_parents(design_dir)
        found = [design_dir, *targets.candidates] if design_dir.is_dir() else []
        found_skipped = targets.skipped
    storages = _generation_storages(design_dir.name, run_root) if flow_name is None else []
    parents = _distinct(found)
    scans = [_run_directories_in(flow_name, parent, (), run_root) for parent in parents]
    skipped = unique([*found_skipped, *(s for scan in scans for s in scan.skipped)])
    candidates = [p for scan in scans for p in scan.candidates]
    _log_skipped(skipped)
    listed = _listed(flow_name, candidates)
    done = _remove_confirmed(listed, run_root, confirm, storages)
    return ScrubResult(
        parents,
        done.removed,
        done.kept,
        done.gone,
        [s.link for s in skipped],
        {s.link: s.problem for s in skipped},
    )


def _generation_storages(design_name: str, run_root: Path) -> list[Path]:
    """Return the generation storages of a design under `run_root`, sorted.

    A link where they lie (`<run root>/.generated/<design>`) is a `RunDirectoryError`."""
    base = run_root / GENERATION_DIRECTORY / design_name
    refuse_link_on_way(base, run_root)
    return sorted(p for p in base.glob("*") if p.is_dir() and not p.is_symlink())


def _remove_storages(storages: Sequence[Path], run_root: Path) -> tuple[list[Path], list[Path]]:
    """Remove the generation storages, each under its exclusive lock. Return `(removed, gone)`.

    The caller has the confirmation. A storage that became a link while scrub waited, or that
    leads to another place than the one that was listed, is not removed: it is a
    `RunDirectoryError`."""
    removed: list[Path] = []
    gone: list[Path] = []
    places = {storage: os.path.realpath(storage) for storage in storages}
    for storage in storages:
        with storage_lock(storage, exclusive=True, run_root=run_root):
            if _is_gone(storage):
                gone.append(storage)
                continue
            # Judged again under the lock, as the user confirmed what was listed: a storage that
            # became a link, or whose place is another one now, is not removed.
            try:
                RunDirectory(run_root, run_root).unlinked(storage)
            except RunDirectoryError as error:
                raise RunDirectoryError(
                    f"{storage} is a link now: it was not removed. {error}"
                ) from error
            if os.path.realpath(storage) != places[storage]:
                raise RunDirectoryError(
                    f"{storage} leads to another place than the one that scrub listed: "
                    "it was not removed."
                )
            owner = RunDirectory.claimed(storage, run_root)
            owner.clear()
            owner.delete()
        removed.append(storage)
    return removed, gone


def scrub_generations(
    design_name: str,
    *,
    run_root: Path,
    confirm: Optional[ScrubConfirmation] = None,
) -> ScrubResult:
    """Find the generation storages of a design under `run_root`, and remove them.

    A storage (`<run root>/.generated/<design>/<key>`) holds the output of the generator of the
    design for one configuration. One confirmation covers all of them; without `confirm`, nothing
    is removed, because the library never asks. Each storage is removed under its exclusive lock,
    which a launch holds in the shared mode for as long as it reads the storage. So scrub waits
    for every launch that reads a storage before it removes that storage. A storage that is gone
    when its turn comes is reported as `gone`. A thread that holds a lock of Xeda is refused
    (`LeaseHeldError`), because scrub waits for each lock."""
    refuse_if_held()
    run_root = run_root.resolve()
    base = run_root / GENERATION_DIRECTORY / design_name
    found = _generation_storages(design_name, run_root)
    if not found:
        return ScrubResult([base], [], [], [], [])
    if confirm is None:
        log.warning(
            "Not removing %d generation storage(s) of %s: nothing confirmed the removal, and only "
            "the command line asks the user.",
            len(found),
            design_name,
        )
        return ScrubResult([base], [], [], [], [])
    if not confirm([ScrubCandidate(path, ()) for path in found]):
        return ScrubResult([base], [], [], [], [])
    removed, gone = _remove_storages(found, run_root)
    return ScrubResult([base], removed, [], gone, [])


def list_run_directories(
    run_root: Path,
    *,
    design_name: str | None = None,
    flow: str | None = None,
    target: str | None | Unset = UNSET,
) -> list[Path]:
    """Return the run directories under `run_root`, sorted: the ones that a selection names.

    Without `design_name`, every design of the run root. Without `flow`, every flow. `target` is a
    target name (the run directories of that target), `None` (those that no target holds), or
    `UNSET` (both). The rule of a run directory is the one of scrub (`_run_directories_in`).
    The function reads the disk and takes no lock; it follows no link at the level of a design or
    a target. An error of the file system (a run root that cannot be read) is an `OSError`."""
    root = run_root.resolve()
    if not root.is_dir():
        return []
    if design_name is not None:
        designs = [root / design_name]
    else:
        designs = [
            p for p in sorted(root.iterdir()) if DESIGN_NAME.fullmatch(p.name) and _is_real_dir(p)
        ]
    found: list[Path] = []
    for design in designs:
        if not _is_real_dir(design):
            continue
        parents: list[Path] = [design / target] if isinstance(target, str) else [design]
        if isinstance(target, Unset):
            parents += [
                p
                for p in sorted(design.iterdir())
                if DESIGN_NAME.fullmatch(p.name)
                and _is_real_dir(p)
                and not _holds_launch_documents(p)
            ]
        for parent in parents:
            if _is_real_dir(parent):
                found += _run_directories_in(flow, parent, (), root).candidates
    return sorted(found)


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()
