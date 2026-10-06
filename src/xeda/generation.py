"""Whether a design's generator (`Design.Generator`) must run again.

A generator runs while the design is loaded, before any flow, run directory or trace exists, so
the decision cannot use the trace's machinery -- and its record may not be kept beside the design:
everything outside a run root is the user's (D21). The record is therefore the same object as a
prepared Xilinx chip database (`flows.xilinx.prepare_chipdb`) with another payload: an entry under
`<run root>/.cache/generators/`, named by an identity hashed from the generator's inputs and direct
executable, holding the digest of every source the last generation left, written atomically under
the entry's own durable lock. A POSIX directory-descriptor lease also serializes loads for the
same design-root directory across the first generation and different input identities, without a
sidecar or eager run-root creation.

The decision is **content-based**, as every other change xeda judges is: the generator's own
sources, selected executable, and the installed packages it declares are hashed into the identity,
and the sources it produced are compared with the digests the record holds. A modification time never decides, so a
`touch`, a `cp -p` or a branch round-trip costs a hash rather than a wrong answer, and an edit
given back its old mtime is caught. Metadata is trusted nowhere here (`digest.FileRecord.trusted`):
that rule needs the time the record was taken, read from the file's own file system
(`digest.filesystem_time_ns`), which writes a marker in the directory it reads -- and neither the
design's tree nor an installed package is xeda's to write in. Each installed package is hashed
once per process (`digest.installed_package_digest`), as the installed xeda package is.

Where the run root comes from at load time: the launcher puts it there
(`design.loading_in_run_root`), as it does the directory a Git dependency is cloned into. It is
made only to write a record, after a generation that succeeded, never to look one up, so a
generator that fails -- like a design that never reaches one -- leaves no run root behind; a
record of a generation that did happen outlives a design that fails after it. With none at all --
a `Design` built directly, a run root whose cache cannot be written -- nothing can record a
generation, so the generator runs: the direction xeda takes everywhere when it cannot prove
something is up to date, and `Flow.always_runs()`'s vocabulary for a step that cannot be judged.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, List, Optional, Sequence, Tuple

import yaml

from .digest import content_digest, installed_package_digest, record_file
from .listing import VCS_METADATA, directory_files
from .run_dir import RunDirectory
from .utils import replacing_file, semantic_hash

if TYPE_CHECKING:  # a design's generator; `design` imports this module, never the other way
    from .design import Generator

log = logging.getLogger(__name__)

#: Where the records live, under the run root.
CACHE_DIRECTORY = Path(".cache") / "generators"

#: The format of a record: an entry of another format is not read.
RECORD_FORMAT = 1

#: Why a generator cannot be judged at all, so that it runs on every load.
NO_RUN_ROOT = "there is no run root to keep a record of its last generation in"
ALWAYS_RUNS = "it declares `always_runs`"
NOTHING_TO_JUDGE_BY = (
    "it declares neither `sources` nor `packages`, so nothing says when it is out of date"
)
NOTHING_PRODUCED = "the sources it generates cannot be told apart from what it would produce"
NO_RECORD = "nothing records an earlier generation of these sources"
REBUILD_ALL = "this launch rebuilds everything"


def _named(path: Path, root: Path) -> str:
    """How a file is named in an identity or a record: relative to the design root where it lies
    under it (so moving a whole design tree changes nothing), else the location it names."""
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _digests(paths: Sequence[Path], root: Path) -> Tuple[Tuple[str, str], ...]:
    """Each path's name and the digest of its content, sorted by name, **one entry per name**. A
    directory is expanded entry by entry (`listing.directory_files`, links followed,
    version-control metadata left out), exactly as the trace treats a directory a setting names:
    its own digest is a constant, so a file edited or added inside one would otherwise be
    invisible. Each entry is recorded as itself (`follow_symlinks=False`), as the trace records a
    listing: a link by its target and, for a link to a file, that file's content -- so an
    editor's dangling lock file (`.#Top.scala`) is a file like any other rather than an error.

    A file can be named twice -- by a pattern and by its own path, or by a directory and by
    itself -- and a record naming one file twice reads back as damaged (`_stale`), so the names
    are made unique here, where the record's entries are made. The path the design declared is
    recorded as the design names it (a link followed, a missing one an error); a directory's
    entry for the same name defers to it, so the digest does not depend on the order the paths
    were given in."""
    named: dict[str, str] = {}
    for path in paths:
        named[_named(path, root)] = record_file(path).sha
    for path in paths:
        if path.is_dir():
            for child in directory_files(path, skip=VCS_METADATA, follow_links=True):
                name = _named(child, root)
                if name not in named:
                    named[name] = record_file(child, follow_symlinks=False).sha
    return tuple(sorted(named.items()))


def generation_identity(generator: Generator, design_root: Path) -> str:
    """What the generator would produce, hashed: its configuration **as the design states it**
    (never the working directory and environment `process_generation` completes it with -- a
    record must be reusable from another shell, and xeda tracks no environment variable), the
    content of every source it declares, and the digest of every installed package it declares.

    A package nothing provides is a `ValueError` naming it: a generator that reads one cannot be
    judged by silence.
    """
    executable = generator.execution_executable_path(design_root)
    command = generator.execution_command(design_root)
    recipe = {
        "format": RECORD_FORMAT,
        "class": type(generator).__name__,
        "generator": generator.model_dump(
            mode="json", exclude={"sources", "generated_sources", "cwd", "env"}
        ),
        "cwd": generator.cwd,
        "env": generator.env,
        "sources": _source_digests(generator, design_root),
        "packages": tuple(
            (name, installed_package_digest(name)) for name in sorted(set(generator.packages))
        ),
        "executable": (command[0], content_digest(executable)),
    }
    return semantic_hash(recipe)


def _source_digests(generator: Generator, design_root: Path) -> Tuple[Tuple[str, str], ...]:
    """The content of what the generator reads. A source that cannot be read (removed since the
    design was validated, a link to nothing) names itself: it is the design's error, not a
    traceback from inside a hash."""
    try:
        return _digests(generator.sources, design_root)
    except OSError as error:
        raise ValueError(
            f"cannot read the generator source {error.filename or '?'}: {error.strerror or error}"
        ) from error


class Generation:
    """The judgement on one generator, and the record of what it produced.

    `reason` is why it must run, in the words of a stale reason, or None when the sources of the
    last generation are still there with the content it left. `produced()` records what a
    generation just wrote -- making the run root then, and not before, so a generator that fails
    leaves none behind.
    """

    def __init__(
        self,
        reason: Optional[str],
        *,
        run_root: Optional[Callable[[bool], Optional[Path]]] = None,
        root: Optional[Path] = None,
        identity: str = "",
        generator: Optional[Generator] = None,
        design_root: Optional[Path] = None,
        outputs: Optional[Callable[[], Optional[List[Path]]]] = None,
    ) -> None:
        self.reason = reason
        self._run_root = run_root
        self._root = root
        self._identity = identity
        self._generator = generator
        self._design_root = design_root
        self._outputs = outputs

    def produced(self) -> None:
        """Record the sources this generation left, with their content, under the identity its
        inputs had -- asking for the run root only now, so that a generator that fails, like a
        design that never reaches one, leaves none behind. Nothing is recorded when there is
        nowhere to keep it, when the generator produced none of the sources the design declares,
        or when its inputs changed while it ran: the next load then judges it out of date again,
        which is the safe answer."""
        if self._run_root is None or self._outputs is None:
            return
        assert self._generator is not None and self._design_root is not None
        produced = self._outputs()
        if not produced:
            log.debug(
                "Keeping no record of generator '%s': %s", self._generator.name, NOTHING_PRODUCED
            )
            return
        try:
            outputs = _digests(produced, self._design_root)
            if generation_identity(self._generator, self._design_root) != self._identity:
                log.info(
                    "The inputs of generator '%s' changed while it ran: keeping no record of it",
                    self._generator.name,
                )
                return
            root = self._root if self._root is not None else self._run_root(True)
            if root is None:  # pragma: no cover - a provider that creates nothing
                return
            owner = RunDirectory(root, root)
            cache = owner.unlinked(root / CACHE_DIRECTORY)
            entry = owner.unlinked(cache / f"{self._identity}.yaml")
            owner.unlinked(_entry_lock(entry))
            cache.mkdir(parents=True, exist_ok=True)
            record = {
                "format": RECORD_FORMAT,
                "identity": self._identity,
                "generator": self._generator.name,
                "outputs": [[name, sha] for name, sha in outputs],
            }
            # Reentrant where the judgement already holds this entry exclusively.
            with _locked(entry):
                owner.unlinked(entry)
                with replacing_file(entry) as stream:
                    yaml.safe_dump(record, stream, sort_keys=True)
        except (OSError, ValueError, yaml.YAMLError) as error:
            # A record is an optimization: failing to keep one costs a re-run, never a wrong one.
            log.warning(
                "Cannot record what generator '%s' produced: %s", self._generator.name, error
            )


def _unjudgeable(generator: Generator) -> Optional[str]:
    """Why this generator can never be judged out of date by its inputs, if it cannot."""
    if generator.always_runs:
        return ALWAYS_RUNS
    if not generator.sources and not generator.packages:
        return NOTHING_TO_JUDGE_BY
    return None


def _stale(
    entry: Path,
    identity: str,
    design_root: Path,
    outputs: Callable[[], Optional[List[Path]]],
) -> Optional[str]:
    """Why the generator must run, judging the sources it last produced against the record
    `entry` holds, or None when every one of them is there with the content it left."""
    try:
        data = yaml.safe_load(entry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NO_RECORD
    except (OSError, ValueError, yaml.YAMLError) as error:
        return f"the record of its last generation ({entry}) cannot be read: {error}"
    if not isinstance(data, dict) or data.get("format") != RECORD_FORMAT:
        return f"the record of its last generation ({entry}) is of another format"
    if data.get("identity") != identity:
        # Cannot happen while the entry is named by the identity; a stale answer is the safe one.
        return "the record of its last generation is of other inputs"
    recorded = data.get("outputs")
    if not isinstance(recorded, list) or not recorded:
        return f"the record of its last generation ({entry}) names no generated source"
    produced = outputs()
    if not produced:
        return NOTHING_PRODUCED
    try:
        now = _digests(produced, design_root)
    except FileNotFoundError as error:
        return f"{_named(Path(error.filename or '?'), design_root)} is not there"
    except OSError as error:
        return f"a generated source cannot be read: {error}"
    was = tuple(
        sorted(
            (name, sha)
            for name, sha in (
                entry_
                for entry_ in recorded
                if isinstance(entry_, list)
                and len(entry_) == 2
                and all(isinstance(part, str) for part in entry_)
            )
        )
    )
    if len(was) != len(recorded):
        return f"the record of its last generation ({entry}) is malformed"
    if len({name for name, _ in was}) != len(was):
        return f"the record of its last generation ({entry}) is malformed"
    if {name for name, _ in now} != {name for name, _ in was}:
        return "the design declares other generated sources than its last generation left"
    changed = [name for (name, sha), (_, recorded_sha) in zip(now, was) if sha != recorded_sha]
    if changed:
        return f"{changed[0]} is not what its last generation left"
    return None


@contextmanager
def judging_generation(
    generator: Generator,
    design_root: Path,
    outputs: Callable[[], Optional[List[Path]]],
    run_root: Optional[Callable[[bool], Optional[Path]]] = None,
    planning: bool = False,
    rebuild_all: bool = False,
) -> Iterator[Generation]:
    """Judge a generator under a lease for its design tree, except during read-only planning."""
    if planning:
        with _judging_generation_unlocked(
            generator, design_root, outputs, run_root, planning, rebuild_all
        ) as generation:
            yield generation
        return
    from .flow_runner.run_lock import generator_design_lock

    design_root = Path(design_root).resolve()
    with generator_design_lock(design_root):
        with _judging_generation_unlocked(
            generator, design_root, outputs, run_root, planning, rebuild_all
        ) as generation:
            yield generation


@contextmanager
def _judging_generation_unlocked(
    generator: Generator,
    design_root: Path,
    outputs: Callable[[], Optional[List[Path]]],
    run_root: Optional[Callable[[bool], Optional[Path]]] = None,
    planning: bool = False,
    rebuild_all: bool = False,
) -> Iterator[Generation]:
    """Judge `generator` under a same-design-root lease and a per-identity record lock.

    `outputs` resolves the sources the generator produces, as the design declares them now --
    called again after a generation, because a pattern is exactly what one was waiting for.
    The directory lease serializes first generation and different identities for this design
    root on POSIX, without writing beside it. `run_root` is asked for a run root that is already
    there; one is made only to write a record (`Generation.produced`), so a design that fails to
    load creates none, and with `planning` nothing is created, locked or written at all. With
    `rebuild_all` (`--rebuild-all`, which
    `--clean` implies) the generator runs whatever the record says, and records what it leaves.
    """
    reason = _unjudgeable(generator)
    if reason is not None:
        yield Generation(reason)
        return
    if run_root is None:  # a `Design` built or loaded outside a launcher
        yield Generation(NO_RUN_ROOT)
        return
    if rebuild_all and not planning:
        # `--rebuild-all`/`--clean` is what forces everything to run again, generation included;
        # the record is still written, so the next ordinary launch is up to date.
        reason = REBUILD_ALL
    # The configuration as the design states it, before `process_generation` completes the
    # generator with a working directory and this shell's environment: what the identity is of,
    # and what the recheck after the run compares, so that only its *inputs* can change meanwhile.
    stated = generator.model_copy(deep=True)
    identity = generation_identity(stated, design_root)

    def judged(why: Optional[str], root: Optional[Path] = None) -> Generation:
        """The judgement, with what it takes to record the generation afterwards -- nothing when
        planning, which writes no record and creates no run root."""
        if planning:
            return Generation(why)
        return Generation(
            why,
            run_root=run_root,
            root=root,
            identity=identity,
            generator=stated,
            design_root=design_root,
            outputs=outputs,
        )

    root = run_root(False)
    if root is None:
        if reason is None:
            reason = NO_RECORD
        # There is no run root yet, and finding out whether one holds a record must not make one:
        # nothing can have been recorded, so this generates, and records afterwards.
        yield judged(reason)
        return
    owner = RunDirectory(root, root)
    cache = owner.unlinked(root / CACHE_DIRECTORY)
    if planning:  # read-only: a plan creates nothing, not even the run root's cache directory
        entry = owner.unlinked(cache / f"{identity}.yaml")
        owner.unlinked(_entry_lock(entry))
        yield judged(_stale(entry, identity, design_root, outputs))
        return
    while True:
        entry = owner.unlinked(cache / f"{identity}.yaml")
        owner.unlinked(_entry_lock(entry))
        retry = False
        with ExitStack() as stack:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                stack.enter_context(_locked(entry))
            except OSError as error:  # a read-only run root: nothing can be recorded, so it runs
                log.debug("Cannot keep a record of a generation in %s: %s", cache, error)
                yield Generation(NO_RUN_ROOT)
                return
            # Revalidate names and identity after waiting: another load may have changed an input
            # while this load waited for the identity-specific lock.
            owner.unlinked(entry)
            owner.unlinked(_entry_lock(entry))
            current_identity = generation_identity(stated, design_root)
            if current_identity != identity:
                identity = current_identity
                retry = True
            else:
                yield judged(reason or _stale(entry, identity, design_root, outputs), root)
        if not retry:
            return


def _entry_lock(entry: Path) -> Path:
    """The durable lock beside an entry, as every other lock of xeda's is named."""
    from .flow_runner.run_lock import lock_file  # a leaf of the launcher: imported on use

    return lock_file(entry)


@contextmanager
def _locked(entry: Path) -> Iterator[None]:
    """Hold the entry's exclusive durable lock (no-op on Windows, as every other one is)."""
    from .flow_runner.run_lock import run_dir_lock  # a leaf of the launcher: imported on use

    with run_dir_lock(entry):
        yield
