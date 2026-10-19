"""The decision whether the generator of a design (`Design.Generator`) must run again.

A generator runs while the design is loaded, before any flow, run directory or trace exists. The
decision therefore cannot use the machinery of the trace. The record cannot live beside the design
either, because everything outside a run root belongs to the user. The record is an entry of the
same kind as a prepared Xilinx chip database (`flows.xilinx.prepare_chipdb`) with another payload.
It lies under `<run root>/.cache/generators/` and is named by an identity, a hash of the inputs and
the direct executable of the generator. It holds the digest of every source that the last
generation left, and Xeda writes it atomically under the durable lock of the entry. On POSIX, a
lease on the directory descriptor of the design root also serializes the first generation and the
loads with different input identities for that design root. It needs no sidecar file and creates
no run root early.

The decision depends on content. The sources of the generator and the selected executable are
hashed into the identity, and the sources that the generator produced are compared with the
digests in the record. A modification time never decides. A `touch`, a `cp -p` or a branch
round-trip costs a hash and gives no wrong answer, and an edit given back its old mtime is caught.
Metadata is trusted nowhere here (`digest.FileRecord.trusted`), because that rule needs the time
the record was taken, read from the file system of the file (`digest.filesystem_time_ns`). That
call writes a marker in the directory it reads, and Xeda must not write in the tree of the design
or in anything else that a generator reads.

A generator is an external tool, and Xeda assumes nothing about its language or environment. What
it reads is what the design names in `sources`. A directory counts as every file in it, outside
the design root too (a library tree, an editable clone). Xeda cannot see a change such as an
upgraded package: use `--rebuild-all` or `always_runs`.

The launcher supplies the run root at load time (`design.loading_in_run_root`), as it does the
directory that a Git dependency is cloned into. Xeda makes the run root only to write a record
after a successful generation, and never to look one up. A generator that fails, and a design that
never reaches one, leave no run root behind. The record of a generation that did happen stays when
the design fails after it. Without a usable run root (a `Design` built directly, or a run root
whose cache cannot be written), nothing can record a generation, so the generator runs. Xeda
always takes this direction when it cannot prove that something is up to date. `Flow.always_runs()`
uses the same rule for a step that cannot be judged.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, List, Optional, Sequence, Tuple

import yaml

from .digest import content_digest, record_file
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
NOTHING_TO_JUDGE_BY = "it declares no `sources`, so nothing says when it is out of date"
NOTHING_PRODUCED = "the sources it generates cannot be told apart from what it would produce"
NO_RECORD = "nothing records an earlier generation of these sources"
REBUILD_ALL = "this launch rebuilds everything"


def _named(path: Path, root: Path) -> str:
    """Return the name of a file in an identity or a record.

    The name is relative to the design root where the file lies under it, so moving a whole
    design tree changes nothing. Otherwise it is the location the path names.
    """
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _digests(paths: Sequence[Path], root: Path) -> Tuple[Tuple[str, str], ...]:
    """Return the name and content digest of each path, sorted by name, with one entry per name.

    A directory is expanded entry by entry (`listing.directory_files`, links followed,
    version-control metadata left out), as the trace treats a directory that a setting names. The
    digest of a directory is a constant, so a file edited or added inside it would otherwise be
    invisible. Each entry is recorded as itself (`follow_symlinks=False`), as the trace records a
    listing: a link by its target and, for a link to a file, also by the content of that file. A
    dangling lock file of an editor (`.#Top.scala`) is therefore a file like any other and not an
    error.

    A file can be named twice, by a pattern and by its own path, or by a directory and by itself.
    A record that names one file twice reads back as damaged (`_stale`), so this function makes
    the names unique where it makes the entries of the record. It records the path that the
    design declared as the design names it (a link is followed, and a missing file is an error).
    The entry of a directory for the same name defers to it, so the digest does not depend on
    the order of the paths.
    """
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
    """Return the identity of what the generator would produce, as a hash.

    The hash covers the configuration of the generator as the design states it, the content of
    every source that the generator declares, and the selected executable. It does not cover the
    working directory and the environment that `process_generation` adds. A record must be
    reusable from another shell, and Xeda tracks no environment variable.
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
        "executable": (command[0], content_digest(executable)),
    }
    return semantic_hash(recipe)


def _source_digests(generator: Generator, design_root: Path) -> Tuple[Tuple[str, str], ...]:
    """Return the content digests of what the generator reads.

    A source that cannot be read (removed since the design was validated, a link to nothing) is
    named in a `ValueError`, so it is an error of the design and not a traceback from inside a
    hash.
    """
    try:
        return _digests(generator.sources, design_root)
    except OSError as error:
        raise ValueError(
            f"cannot read the generator source {error.filename or '?'}: {error.strerror or error}"
        ) from error


class Generation:
    """The judgment on one generator, and the record of what it produced.

    `reason` is why the generator must run, in the words of a stale reason. It is None when the
    sources of the last generation are still there with the content that it left. `produced()`
    records what a generation just wrote. It makes the run root at that point and not before, so
    a generator that fails leaves no run root behind.
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
        description: Optional[str] = None,
    ) -> None:
        self.reason = reason
        self._run_root = run_root
        self._root = root
        self._identity = identity
        self._generator = generator
        self._design_root = design_root
        self._outputs = outputs
        # How a message names the generator: its design and what it runs (`Generator.describe`).
        self._description = description or (generator.describe() if generator else "a generator")

    def produced(self) -> None:
        """Record the sources that this generation left, with their content.

        The record uses the identity that the inputs had. The method asks for the run root only
        now, so a generator that fails, like a design that never reaches one, leaves no run root
        behind. It records nothing when there is nowhere to keep the record, when the generator
        produced none of the sources that the design declares, or when its inputs changed while
        it ran. The next load then judges the generator stale again, which is the safe answer.
        """
        if self._run_root is None or self._outputs is None:
            return
        assert self._generator is not None and self._design_root is not None
        produced = self._outputs()
        if not produced:
            log.debug("Keeping no record of %s: %s", self._description, NOTHING_PRODUCED)
            return
        try:
            outputs = _digests(produced, self._design_root)
            if generation_identity(self._generator, self._design_root) != self._identity:
                log.info(
                    "The inputs of %s changed while it ran: keeping no record of it",
                    self._description,
                )
                return
            root = self._root if self._root is not None else self._run_root(True)
            if root is None:  # pragma: no cover (a provider that creates nothing)
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
            # Reentrant where the judgment already holds this entry exclusively.
            with _locked(entry):
                owner.unlinked(entry)
                with replacing_file(entry) as stream:
                    yaml.safe_dump(record, stream, sort_keys=True)
        except (OSError, ValueError, yaml.YAMLError) as error:
            # A record is an optimization. Failing to keep one causes another generation, never a
            # wrong reuse.
            log.warning("Cannot record what %s produced: %s", self._description, error)


def _unjudgeable(generator: Generator) -> Optional[str]:
    """Return why the inputs can never show this generator to be stale, or None if they can."""
    if generator.always_runs:
        return ALWAYS_RUNS
    if not generator.sources:
        return NOTHING_TO_JUDGE_BY
    return None


def _stale(
    entry: Path,
    identity: str,
    design_root: Path,
    outputs: Callable[[], Optional[List[Path]]],
) -> Optional[str]:
    """Return why the generator must run, or None.

    The function judges the sources that the generator last produced against the record in
    `entry`. It returns None when every one of them is there with the content that the
    generator left.
    """
    try:
        data = yaml.safe_load(entry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NO_RECORD
    except (OSError, ValueError, yaml.YAMLError) as error:
        return f"the record of its last generation ({entry}) cannot be read: {error}"
    if not isinstance(data, dict) or data.get("format") != RECORD_FORMAT:
        return f"the record of its last generation ({entry}) is of another format"
    if data.get("identity") != identity:
        # Cannot happen while the identity names the entry. A stale answer is the safe one.
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
    description: Optional[str] = None,
    read_only: bool = False,
) -> Iterator[Generation]:
    """Judge a generator under a lease for its design tree, except during planning.

    `read_only` judges as a plan does and creates and writes nothing, but still under the lease.
    The load that judges the declared design of a launch reads the outputs of the generator,
    which another generation in the tree may be writing.
    """
    if planning:
        with _judging_generation_unlocked(
            generator, design_root, outputs, run_root, planning, rebuild_all, description
        ) as generation:
            yield generation
        return
    from .flow_runner.run_lock import generator_design_lock

    design_root = Path(design_root).resolve()
    with generator_design_lock(design_root):
        with _judging_generation_unlocked(
            generator, design_root, outputs, run_root, read_only, rebuild_all, description
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
    description: Optional[str] = None,
) -> Iterator[Generation]:
    """Judge `generator` under a lease for the design root and a lock for the record.

    `outputs` resolves the sources that the generator produces, as the design declares them now.
    The caller asks again after a generation, because a pattern can match files only then. On
    POSIX, the directory lease serializes the first generation and the generations with
    different identities for this design root. It writes nothing beside the design root.

    `run_root` is asked for a run root that already exists. Xeda makes one only to write a record
    (`Generation.produced`), so a design that fails to load creates none. With `planning`,
    nothing is created, locked or written at all. With `rebuild_all` (`--rebuild-all`, which
    `--clean` implies), the generator runs whatever the record says and records what it leaves.
    A plan of such a design needs a generator, as one with a stale record does.
    """
    reason = _unjudgeable(generator)
    if reason is not None:
        yield Generation(reason)
        return
    if run_root is None:  # a `Design` built or loaded outside a launcher
        yield Generation(NO_RUN_ROOT)
        return
    if rebuild_all:
        # `--rebuild-all` and `--clean` force everything to run again, generation included. The
        # record is still written, so the next ordinary launch is up to date. A plan describes
        # that launch, so it needs a generator too, and a plan starts none.
        reason = REBUILD_ALL
    # The configuration as the design states it, before `process_generation` adds a working
    # directory and the environment of this shell. The identity covers it, and the recheck after
    # the run compares it, so only the inputs of the generator can change meanwhile.
    stated = generator.model_copy(deep=True)
    identity = generation_identity(stated, design_root)

    def judged(why: Optional[str], root: Optional[Path] = None) -> Generation:
        """Return the judgment with what it takes to record the generation afterwards.

        When planning, it takes nothing: a plan writes no record and creates no run root.
        """
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
            description=description,
        )

    root = run_root(False)
    if root is None:
        if reason is None:
            reason = NO_RECORD
        # There is no run root yet, and looking for a record must not make one. Nothing can have
        # been recorded, so the generator runs, and the record is written afterwards.
        yield judged(reason)
        return
    owner = RunDirectory(root, root)
    cache = owner.unlinked(root / CACHE_DIRECTORY)
    if planning:  # read-only: a plan creates nothing, not even the cache directory
        entry = owner.unlinked(cache / f"{identity}.yaml")
        owner.unlinked(_entry_lock(entry))
        yield judged(reason or _stale(entry, identity, design_root, outputs))
        return
    while True:
        entry = owner.unlinked(cache / f"{identity}.yaml")
        owner.unlinked(_entry_lock(entry))
        retry = False
        with ExitStack() as stack:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                stack.enter_context(_locked(entry))
            except OSError as error:  # read-only run root: nothing can be recorded, so it runs
                log.debug("Cannot keep a record of a generation in %s: %s", cache, error)
                yield Generation(NO_RUN_ROOT)
                return
            # Revalidate the names and the identity after waiting. Another load may have changed an
            # input while this load waited for the lock of the identity.
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
    """Return the durable lock beside an entry, named as every other lock of Xeda is."""
    from .flow_runner.run_lock import lock_file  # a leaf of the launcher: imported on use

    return lock_file(entry)


@contextmanager
def _locked(entry: Path) -> Iterator[None]:
    """Hold the exclusive durable lock of the entry (no effect on Windows, as for every lock)."""
    from .flow_runner.run_lock import run_dir_lock  # a leaf of the launcher: imported on use

    with run_dir_lock(entry):
        yield
