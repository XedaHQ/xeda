"""The generation stage of a launch: run the design's generator into its storage, and give the
flows a view of the design whose generated sources name their files.

A design declares the sources that its generator writes (`Source.declaration`), and loading the
design runs nothing. A launch generates after every declarative check and before the first
producer. The generator writes into a storage under the run root, named by the configuration it
serves (`generation_key`), so two variants of one design keep their own files. A generated entry
can also be a path below the design root (a tree entry): Xeda then copies each tree file that the
generation wrote into `.xeda-tree/` of the storage (`tree_outputs`). The design that the caller
gave never changes. The flows get a copy (`execution_design`) whose generated sources are the
files in the storage.

A design may merge dependencies that have generators of their own. Each generator is a unit of
generation (`Design.generation_units`, dependencies first, then the design itself), with a storage
and a key of its own, and a launch holds the leases of them all (`Generations`). The sources of a
dependency's generator keep that dependency as their owner (`Source.owner`).

A launch locks its whole graph in one acquisition: the run directories of its nodes, then the
storages, then the design trees that the generators to run write into (`settle`). A generator
runs under the exclusive locks of that acquisition and takes no other lock. Then the launch
releases the set and takes it again with the storages it generated shared, and judges every
storage again before any node runs.

Planning writes nothing here. `inspect_generation` reads the records of the storages, and says
whether a launch would run a generator.

The functions are free functions with explicit arguments, and this module never imports
`default_runner`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any, Protocol

import yaml

from ..dataclass import ValidationError
from ..deliver import GENERATOR, Delivery
from ..design import (
    TREE_DIRECTORY,
    Design,
    Generator,
    Source,
    SourceType,
    already_listed,
    generator_of,
    storage_name,
    tree_declarations,
)
from ..flow import Flow
from ..frozen import FrozenDict
from ..generation import (
    KEY_LENGTH,
    RECORD_NAME,
    REBUILD_ALL,
    GenerationError,
    MissingGenerated,
    delivered_name,
    executable_identity,
    generation_storage,
    input_digests,
    judge,
    matches_in,
    output_name_of,
    outputs_in,
    recipe_identity,
    stored_pattern,
    write_record,
)
from ..run_dir import RunDirectory, RunDirectoryError
from ..tree_outputs import Observed, copy_tree_file, observe, written_names
from ..utils import NonZeroExitCode, semantic_hash
from .configuration import design_configuration
from .run_lock import LockRequest, LockSet, acquire_all

log = logging.getLogger(__name__)

__all__ = [
    "Generation",
    "Generations",
    "Inspection",
    "generating_units",
    "execution_design",
    "generate",
    "generated",
    "generation_key",
    "inspect_generation",
    "generated_deliveries",
    "located_paths",
    "missing_declarations",
    "planned_deliveries",
    "reading_nodes",
    "reads_generated",
]

#: The reason a plan gives for a node that reads sources which the generator has not written.
NOT_RUN = "the generator has not run"

#: How many times a launch generates to find a storage that nothing changed while it read it.
ATTEMPTS = 3


@dataclass(frozen=True)
class Generation:
    """The output of one generation: its storage, its key, and the files of each declaration.

    `matches` maps each declaration of a generated source (a name or a pattern, as `rtl.sources`
    states it) to the names of the files that it matches in the storage, sorted and relative to
    it (`generation.stored_pattern`: the copy of a tree file lies under `.xeda-tree/`). A
    declaration may match nothing: a node that reads it fails (`MissingGenerated`)."""

    storage: Path
    key: str
    matches: Mapping[str, tuple[str, ...]] = field(default_factory=FrozenDict)
    #: How the messages name the generator.
    description: str = "the generator"

    def __post_init__(self) -> None:
        object.__setattr__(self, "matches", FrozenDict(self.matches))

    @property
    def files(self) -> Mapping[str, Path]:
        """The absolute file of each name that a declaration matched."""
        return FrozenDict(
            {name: self.storage / name for found in self.matches.values() for name in found}
        )


@dataclass(frozen=True)
class Generations:
    """The generations of a design and of its dependencies that have generators, by owner.

    The owner is None for the generator of the design itself, and the root of the dependency
    (`Source.owner`) for the generator of a dependency."""

    by_owner: Mapping[str | None, Generation]

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_owner", MappingProxyType(dict(self.by_owner)))

    @classmethod
    def single(cls, generation: Generation) -> Generations:
        """The generations of a design with a generator and no dependency that has one."""
        return cls({None: generation})

    @property
    def keys(self) -> Mapping[str | None, str]:
        """The key of the storage of each owner."""
        return MappingProxyType({owner: found.key for owner, found in self.by_owner.items()})

    def of(self, source: Source) -> Generation | None:
        """The generation that wrote, or writes, the generated `source`."""
        return self.by_owner.get(source.owner)


@dataclass(frozen=True)
class Inspection:
    """What a read-only look at the storages of a design shows.

    `generator` says that the design, or a dependency, has a generator. `generation` is the
    storages as they are, when the records vouch for all of them, and None when a launch would run
    a generator, for `reason`."""

    generator: bool
    generation: Generations | None = None
    reason: str = ""


def generating_units(design: Design) -> list[tuple[str | None, Design]]:
    """Return the designs whose generators a launch of `design` runs, in the order it runs them.

    A dependency generates before the design that merged it, and a dependency of a dependency
    before that: the units of the design come first, then the design itself. Each is paired with
    its owner (None for the design itself)."""
    units: list[tuple[str | None, Design]] = [
        (unit.generation_label, unit) for unit in design.generation_units
    ]
    if design.rtl.generator is not None:
        units.append((None, design))
    return units


def declarations(design: Design) -> tuple[str, ...]:
    """Return the names and patterns of the generated sources of `design` that its own generator
    writes, in order. The sources of a dependency's generator are that dependency's."""
    return tuple(
        source.output_name
        for source in design.rtl.sources
        if source.generated
        and not source.located
        and source.output_name is not None
        and source.owner is None
    )


def valid_generator(unit: Design) -> Generator | None:
    """Return the generator of `unit` (`design.generator_of`), or None for a design without one.

    A recipe that is not valid now is a `GenerationError`: what it names went since the design
    loaded (a file of `sources`, say)."""
    try:
        return generator_of(unit.rtl, root=unit.root)
    except (ValidationError, ValueError) as error:
        raise GenerationError(f"the recipe of the generator is not valid now: {error}") from error


def generation_key(
    design: Design, environ: Mapping[str, str] | None = None
) -> tuple[str, Generator]:
    """Return the key of the generation that `design` needs, and its generator.

    The key is 16 hexadecimal characters of the hash of everything that decides what the
    generator writes: its recipe (its `env` included), the whole configuration of the design
    without content (`configuration.design_configuration`: `rtl`, `tb`, `language`, and the
    ordered declarations of the sources with their owner), the names and content digests of
    what the generator reads, and the identity of its executable. It does not hold the bytes that
    the generator writes, the files that a pattern matches, the place of any delivery, the place of
    a run root or the target label. The environment that the generator inherits is no part of it:
    it is evidence of freshness (`generation.judge`), so a change regenerates in the same
    storage."""
    if design.rtl.generator is None:
        raise ValueError("the design has no generator")
    generator = valid_generator(design)
    assert generator is not None
    environ = dict(os.environ) if environ is None else environ
    identity = {
        "recipe": recipe_identity(generator),
        "configuration": design_configuration(design),
        "inputs": input_digests(generator, design.root),
        "executable": executable_identity(generator, design.root, environ),
    }
    return semantic_hash(identity)[:KEY_LENGTH], generator


def _inspect_one(
    design: Design, run_root: Path | None, environ: Mapping[str, str]
) -> tuple[str | None, Generation | None]:
    """Look at the storage of the generator of `design` alone: why a launch would run it, or the
    storage as it is."""
    try:
        key, generator = generation_key(design, environ)
    except (GenerationError, ValueError, OSError) as error:
        return str(error), None
    if run_root is None:
        return "there is no run root yet", None
    try:
        storage = generation_storage(run_root, design.name, key)
    except RunDirectoryError as error:
        return str(error), None
    reason, matches = judge(storage, key, declarations(design), generator, environ)
    if reason is not None:
        return reason, None
    return None, Generation(storage, key, matches, generator.describe(design.name))


def inspect_generation(
    design: Design,
    run_root: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    rebuild_all: bool = False,
) -> Inspection:
    """Look at the storages of the generators of `design` and its dependencies, and say whether
    a launch would run one of them.

    It creates and writes nothing, and takes no lock. `run_root` is None when there is none yet.
    The answer is an observation: a launch judges again under its locks."""
    units = generating_units(design)
    if not units:
        return Inspection(False)
    if rebuild_all:
        return Inspection(True, None, REBUILD_ALL)
    environ = dict(os.environ) if environ is None else environ
    found: dict[str | None, Generation] = {}
    for owner, unit in units:
        reason, generation = _inspect_one(unit, run_root, environ)
        if generation is None:
            return Inspection(True, None, reason or "")
        found[owner] = generation
    return Inspection(True, Generations(found))


@dataclass(frozen=True)
class Unit:
    """One generator of a launch, with the storage its configuration names now.

    `owner` is None for the generator of the design itself, and the root of the dependency
    (`Source.owner`) for the generator of a dependency."""

    owner: str | None
    design: Design
    key: str
    generator: Generator
    storage: Path

    @property
    def identity(self) -> str:
        """The name of the storage below the generation directory: `<design>/<key>`."""
        return f"{self.design.name}/{self.key}"


def units_of(design: Design, run_root: Path, environ: Mapping[str, str]) -> list[Unit]:
    """Return the units of `design`, dependencies first, each with the key and the storage that
    what its generator reads names now. A launch locks these storages."""
    found: list[Unit] = []
    for owner, unit in generating_units(design):
        key, generator = generation_key(unit, environ)
        found.append(
            Unit(owner, unit, key, generator, generation_storage(run_root, unit.name, key))
        )
    return found


@dataclass
class GenerationState:
    """What a launch remembers about its generations, through every restart of its acquisition.

    `generated` holds the identities of the storages that this launch generated and found valid
    under their exclusive locks. `rebuild_all` forces such a storage only once. A storage in the
    set is judged as a launch judges what it just generated: a generator that always runs does
    not make it stale again. A storage that is not valid still generates."""

    generated: set[str] = field(default_factory=set)
    #: A launch that only reads what a generation left (the delivery of a sweep point). Every
    #: storage is judged as one this launch generated, and a storage that is not valid fails the
    #: launch: it never runs a generator.
    read_only: bool = False


@dataclass(frozen=True)
class Settled:
    """The result of `settle`: the generations and the locks that the launch holds."""

    generations: Generations | None
    locks: LockSet


class Participants(Protocol):
    """The locks beside the storages that `settle` takes, and the judgment that they wait for."""

    def requests(self) -> Sequence[LockRequest]:
        """The locks to take now, for the modes the participants have now."""

    def settled(self) -> bool:
        """Judge under the locks, after every storage is valid. False: the modes changed, and
        `settle` starts again with the new requests."""


def _reason(
    unit: Unit, environ: Mapping[str, str], *, rebuild_all: bool, state: GenerationState
) -> tuple[str | None, dict[str, tuple[str, ...]]]:
    """Return why the generator of `unit` must run, or None, and the files of its storage.

    `rebuild_all` is the reason only for a storage that this launch has not generated."""
    done = unit.identity in state.generated or state.read_only
    reason, matches = judge(
        unit.storage,
        unit.key,
        declarations(unit.design),
        unit.generator,
        environ,
        judging=not done,
    )
    if reason is None and rebuild_all and not done:
        return REBUILD_ALL, {}
    return reason, matches


def generate_unit(unit: Unit, *, run_root: Path, environ: Mapping[str, str]) -> Generation:
    """Run the generator of `unit` into its storage. The caller holds the exclusive lock of the
    storage and, for a unit with tree entries, of its design tree.

    The generator runs in the design root, or in the directory below it that its recipe names
    (`cwd`), with `DESIGN_ROOT` and `XEDA_GENERATED` in its environment. It writes its storage
    entries under `XEDA_GENERATED`, the storage, and its tree entries below the design root. Xeda
    reads the files that the tree entries match before and after the run, and copies each file
    that the run wrote into `.xeda-tree/` of the storage (`tree_outputs`). Xeda writes nothing
    into the design tree. A failure raises `GenerationError`, and leaves no storage behind.
    `run_root` exists. The result is not judged: a launch judges the storage again."""
    names = declarations(unit.design)
    trees = [name for name in names if storage_name(name) is None]
    design, key, generator, storage = unit.design, unit.key, unit.generator, unit.storage
    description = generator.describe(design.name, design.root)
    owner = RunDirectory(run_root, run_root)
    matches: dict[str, tuple[str, ...]] = {}
    try:
        if storage.exists():
            RunDirectory.claimed(storage, run_root).delete()
        owner.unlinked(storage).mkdir(parents=True)
        before = observe(design.root, trees) if trees else {}
        generator.run(design.root, storage, environ)
        if trees:
            _copy_tree_files(owner, design.root, storage, before, trees)
        matches = outputs_in(storage, names)
        if generation_key(design, environ)[0] == key:
            write_record(owner, storage, key, generator, environ, matches)
        else:
            # What the generator reads changed while it ran, itself perhaps (bytecode in a source
            # directory): its output is not that of this key, so nothing vouches for it, and the
            # storage of this key goes. The launch generates again.
            log.info(
                "The inputs of %s changed while it ran: keeping no output of this key", description
            )
            _discard(storage, run_root)
    except BaseException as error:
        _discard(storage, run_root)
        if isinstance(error, (NonZeroExitCode, OSError, ValueError, RunDirectoryError)):
            raise GenerationError(f"{description} failed: {error}") from error
        raise
    return Generation(storage, key, matches, description)


def _copy_tree_files(
    owner: RunDirectory,
    root: Path,
    storage: Path,
    before: Mapping[str, Observed],
    trees: Sequence[str],
) -> None:
    """Copy into `.xeda-tree/` of `storage` each file below `root` that `trees` match and that
    the generation wrote: it is not in `before`, or has another state or content there."""
    snapshot = storage / TREE_DIRECTORY
    if os.path.lexists(snapshot):
        raise GenerationError(
            f"`{TREE_DIRECTORY}` is a name Xeda keeps for itself in `$XEDA_GENERATED`: the "
            "generator must not write it"
        )
    after = observe(root, trees)
    for name in written_names(before, after):
        destination = snapshot / name
        owner.unlinked(destination.parent).mkdir(parents=True, exist_ok=True)
        copy_tree_file(root, name, after[name], owner.writable(destination))


def generate(
    design: Design,
    *,
    run_root: Path,
    rebuild_all: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Generation | None:
    """Run the generator of `design` into its storage, unless its storage is current.

    The result is None for a design with no generator. Otherwise the storage is current when it
    holds the record of this key, the inherited environment is the one the record names, and
    every generated source is as the last generation left it. Xeda judges that under the
    exclusive lock of the storage (and of the design tree, for a design with tree entries), taken
    in one acquisition, so a second launch of the same configuration waits, finds the record, and
    generates nothing. With `rebuild_all` the generator always runs. A launch does not call this:
    it generates the units that need it under the locks of its whole graph (`settle`).
    `run_root` exists."""
    if design.rtl.generator is None:
        return None
    environ = dict(os.environ) if environ is None else environ
    names = declarations(design)
    while True:
        key, generator = generation_key(design, environ)
        storage = generation_storage(run_root, design.name, key)
        description = generator.describe(design.name, design.root)
        requests = [
            LockRequest("storage", storage, True),
            *_tree_requests(design),
        ]
        with acquire_all(requests, run_root=run_root):
            if generation_key(design, environ)[0] != key:
                # What the generator reads changed while this call waited for the lock: the
                # output would not be that of this key. Start again with the new key.
                continue
            reason, matches = judge(storage, key, names, generator, environ)
            if rebuild_all:
                reason = REBUILD_ALL
            if reason is None:
                log.info(
                    "Not running %s: its generated sources are what its last generation left",
                    description,
                )
                return Generation(storage, key, matches, description)
            log.info("Running %s: %s", description, reason)
            return generate_unit(
                Unit(None, design, key, generator, storage), run_root=run_root, environ=environ
            )


def _tree_requests(design: Design) -> list[LockRequest]:
    """The exclusive lock of the design tree of `design`, for a generator with tree entries."""
    return [LockRequest("tree", design.root, True)] if tree_declarations(design) else []


def _discard(storage: Path, run_root: Path) -> None:
    """Remove a storage that a failed generation left, so that no half-written output stays."""
    try:
        if storage.exists():
            RunDirectory.claimed(storage, run_root).delete()
    except (OSError, RunDirectoryError) as error:  # the failure of the generator comes first
        log.warning("Cannot remove the incomplete output %s: %s", storage, error)


def settle(
    design: Design,
    *,
    run_root: Path,
    environ: Mapping[str, str],
    rebuild_all: bool,
    state: GenerationState,
    stack: ExitStack,
    participants: Participants | None = None,
    leases: Sequence[Path] = (),
    canceled: Callable[[], bool] | None = None,
    before_generation: Callable[[], None] | None = None,
) -> Settled:
    """Take every lock of a launch, make the storages of its generators valid, and keep the locks
    in `stack`. The result holds the generations (None for a design with no generator) and the
    set of locks.

    One acquisition takes the locks of the `participants` (the nodes of the launch), then the
    storage of each generator and each of the `leases` (the storages of Git dependencies), shared
    for a storage that a launch only reads. A storage whose hint says it must be written is
    requested exclusive, and so is the design tree of its unit when the generator has tree
    entries. Under the locks, the call judges every storage again and acts on that:

    - A storage that is not valid under a shared lock makes the call release everything and
      start again with that storage exclusive.
    - Under the exclusive locks, the generators run in dependency order, taking no more locks.
      Each storage is then judged under its exclusive lock. The call releases the whole set and
      takes it again with those storages shared (a lock has no atomic conversion, and another
      launch may replace a storage in the gap). It judges every storage again, so the files of
      the result are those of the judgment under the shared lease.
    - A key that changed, or a lock held elsewhere, makes the call start again. This is no
      failure and counts for nothing.
    - A storage that is still invalid after the launch generated it, or whose generator changed
      what an earlier unit reads, counts as an attempt. After `ATTEMPTS` the call fails with a
      `GenerationError`.

    `before_generation` runs under every set of locks, before any judgment of a storage and so
    before any generator runs. What it raises leaves the call with the locks released.

    Then the participants judge under the locks (`Participants.settled`). The call returns with
    the locks in `stack`, which the caller closes when it has delivered everything."""
    units_present = bool(generating_units(design))
    attempts = 0
    reason: str | None = None
    description = "the generator"
    must_write: set[str] = set()
    keys_generated: dict[str | None, str] = {}
    told: set[str] = set()
    while True:
        units = units_of(design, run_root, environ) if units_present else []
        for unit in units:
            if unit.owner in keys_generated and keys_generated[unit.owner] != unit.key:
                reason = "what its generator reads changed while it ran"
                description = unit.generator.describe(unit.design.name, unit.design.root)
                attempts += 1
                keys_generated.pop(unit.owner)
        if attempts >= ATTEMPTS:
            raise _gave_up(description, reason)
        write = set(must_write)
        for unit in units:
            if (
                _reason(unit, environ, rebuild_all=rebuild_all, state=state)[0] is not None
                and not state.read_only
            ):
                write.add(unit.identity)
        requests = [
            *(participants.requests() if participants is not None else ()),
            *(LockRequest("storage", u.storage, u.identity in write) for u in units),
            *(LockRequest("storage", path, False) for path in leases),
            *(
                request
                for u in units
                if u.identity in write
                for request in _tree_requests(u.design)
            ),
        ]
        with ExitStack() as held:
            locks = held.enter_context(acquire_all(requests, run_root=run_root, canceled=canceled))
            if any(generation_key(u.design, environ)[0] != u.key for u in units):
                continue  # what a generator reads changed while this call waited: new keys
            if before_generation is not None:
                before_generation()
            judged: dict[str | None, Generation] = {}
            ran: list[Unit] = []
            promote = False
            for unit in units:
                found, matches = _reason(unit, environ, rebuild_all=rebuild_all, state=state)
                description = unit.generator.describe(unit.design.name, unit.design.root)
                if found is None:
                    judged[unit.owner] = Generation(unit.storage, unit.key, matches, description)
                    if unit.identity not in state.generated and unit.identity not in told:
                        told.add(unit.identity)
                        log.info(
                            "Not running %s: its generated sources are what its last generation "
                            "left",
                            description,
                        )
                    continue
                if state.read_only:
                    raise GenerationError(
                        f"{description} left no valid sources for the delivery: {found}"
                    )
                if unit.identity in state.generated:
                    # The launch generated it and found it valid: it is not valid now.
                    state.generated.discard(unit.identity)
                    reason = found
                    attempts += 1
                if locks.mode_of("storage", unit.storage) != "exclusive" or (
                    tree_declarations(unit.design)
                    and locks.mode_of("tree", unit.design.root) != "exclusive"
                ):
                    must_write.add(unit.identity)
                    promote = True
                    break
                log.info("Running %s: %s", description, found)
                generate_unit(unit, run_root=run_root, environ=environ)
                ran.append(unit)
            for unit in ran:
                # Judged under the exclusive lock the call still holds, as a launch judges what
                # it just generated.
                state.generated.add(unit.identity)
                found, _matches = _reason(unit, environ, rebuild_all=rebuild_all, state=state)
                if found is None:
                    must_write.discard(unit.identity)
                    keys_generated[unit.owner] = unit.key
                else:
                    state.generated.discard(unit.identity)
                    description = unit.generator.describe(unit.design.name, unit.design.root)
                    reason = found
                    attempts += 1
                    must_write.add(unit.identity)
            if promote or ran:
                continue  # release the set; take it again, with the generated storages shared
            if participants is not None and not participants.settled():
                continue
            stack.enter_context(held.pop_all())
            return Settled(Generations(judged) if units else None, locks)


def _gave_up(description: str, reason: str | None) -> GenerationError:
    return GenerationError(
        f"{description} left no output that Xeda could vouch for after {ATTEMPTS} "
        f"tries: {reason}. A generator that changes what it reads while it runs (a file it "
        "writes into a directory in its `sources`) cannot be judged: name only what it reads"
    )


@contextmanager
def generated(
    design: Design,
    *,
    run_root: Path,
    rebuild_all: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Iterator[Generations | None]:
    """Generate for the design and its dependencies that have generators (`settle`), then hold
    the shared lease of every storage while the block runs. None: nothing generates.

    A launch settles its whole graph in one acquisition; this holds the storages alone."""
    environ = dict(os.environ) if environ is None else environ
    with ExitStack() as stack:
        yield settle(
            design,
            run_root=run_root,
            environ=environ,
            rebuild_all=rebuild_all,
            state=GenerationState(),
            stack=stack,
        ).generations


def fingerprints(generations: Generations | None) -> tuple[tuple[str, str], ...]:
    """Return what identifies the generations as they are now: the storage of each, and the digest
    of the outputs that its record holds (`generation.yaml`, written when the generator ended).

    The caller holds the shared lease of the storages. A key names a configuration, and any
    generation of it is valid; the digests tell the generation that a sweep settled from another
    that a later launch made under the same key."""
    found: list[tuple[str, str]] = []
    for generation in () if generations is None else generations.by_owner.values():
        try:
            record = yaml.safe_load((generation.storage / RECORD_NAME).read_text(encoding="utf-8"))
            digest = semantic_hash(record["outputs"])
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            digest = ""
        found.append((str(generation.storage), digest))
    return tuple(sorted(found))


def verify_fingerprints(
    generations: Generations | None, expected: Sequence[tuple[str, str]]
) -> None:
    """Raise `GenerationError` when the generations are not the ones that `expected` names
    (`fingerprints`). Nothing is expected, nothing is checked."""
    if not expected:
        return
    now = dict(fingerprints(generations))
    for storage, digest in expected:
        if now.get(storage) != digest:
            raise GenerationError(
                f"the generated sources in {storage} are not the ones that the sweep generated: "
                "another launch ran the generator again. Run the sweep again"
            )


def execution_design(design: Design, generations: Generations | None) -> Design:
    """Return the design that the flows of a launch get: `design`, with the files in place of
    its generated sources.

    Each declaration of a generated source (`gen/*.v`) gives, in its place, one source for each
    file that it matches in the storage of its owner, in sorted order and with its type. The
    source keeps the name in declaration form (`generation.output_name_of`): the tree path, or
    `$XEDA_GENERATED/<name>`. A file that two declarations match is listed once. A declaration
    that matches nothing gives no source (see `missing_declarations`). The design that the caller
    gave is not changed."""
    if generations is None:
        return design
    listed: list[Source] = []
    for source in design.rtl.sources:
        if not (source.generated and not source.located):
            listed.append(source)
            continue
        generation = generations.of(source)
        if generation is None:
            continue
        for name in generation.matches.get(str(source.output_name), ()):
            located = Source(
                {"file": str(generation.storage / name)},
                source.type,
                generated=True,
                output_name=output_name_of(name),
                owner=source.owner,
            )
            if not already_listed(listed, located):
                listed.append(located)
    rtl = design.rtl.model_copy(update={"sources": tuple(listed)})
    rtl.invalidate_cached_properties()
    executable = design.model_copy(update={"rtl": rtl})
    executable.invalidate_cached_properties()
    return executable


def reads_generated(
    design: Design, flow_class: type[Flow], settings: Flow.Settings
) -> tuple[Source, ...]:
    """Return the declarations of generated sources that a node of `flow_class` reads.

    A node reads the generated sources of the types it takes (`Flow.design_scope`)."""
    scope: frozenset[SourceType] | None = flow_class.design_scope(settings)
    return tuple(
        source
        for source in design.rtl.sources
        if source.generated and not source.located and (scope is None or source.type in scope)
    )


def _unmatched(
    design: Design,
    generations: Generations,
    flow_class: type[Flow],
    settings: Flow.Settings,
) -> list[tuple[Source, Generation | None]]:
    """The declarations that a node reads, with the generation that should have written each, for
    those that matched no file."""
    unmatched: list[tuple[Source, Generation | None]] = []
    for source in reads_generated(design, flow_class, settings):
        generation = generations.of(source)
        if generation is None or not generation.matches.get(str(source.output_name)):
            unmatched.append((source, generation))
    return unmatched


def missing_declarations(
    design: Design,
    generations: Generations,
    flow_class: type[Flow],
    settings: Flow.Settings,
) -> tuple[str, ...]:
    """Return the declarations that a node reads and that matched no file after the generation."""
    return tuple(
        str(source.output_name)
        for source, _ in _unmatched(design, generations, flow_class, settings)
    )


#: How many files the hint of a missing generated source names.
HINT_FILES = 3


def misplaced_hint(
    design: Design, owner: str | None, declaration: str, storage: Path | None
) -> str:
    """Return a sentence for a generated source that matched nothing, or "".

    `owner` names the generator of the declaration (None for the design's own generator), and
    `storage` is the storage of its last generation (None when there is none). The sentence says
    where a file of that name exists now. It states what is there and never says the generator
    wrote it. It never raises: a link, or a file that cannot be read, gives "".

    - A tree entry that matches a file below the design root: the generation did not write it.
    - A tree entry that matches a file in the storage: the generator writes it there.
    - A storage entry that matches a file below the directory the generator runs in: the
      generator writes it there."""
    unit = next((unit for label, unit in generating_units(design) if label == owner), None)
    if unit is None:
        return ""
    try:
        generator = generator_of(unit.rtl, root=unit.root)
        if generator is None:
            return ""
        name = storage_name(declaration)
        if name is None:
            found = [unit.root / path for path in sorted(observe(unit.root, [declaration]))]
            if found:
                return (
                    f"{_shown(found)} exists, but the last generation did not write it: Xeda "
                    "takes only the files that a generation writes. A tool that skips an output "
                    "it finds up to date must be made to write it"
                )
            in_storage: list[Path] = []
            if storage is not None:  # `matches_in` leaves the record out
                in_storage = [
                    storage / path
                    for path in matches_in(storage, declaration)
                    if path.split("/")[0] != TREE_DIRECTORY
                ]
            if in_storage:
                return (
                    f"The storage of the generator holds {_shown(in_storage)}: if the generator "
                    f"writes it there, write the entry as `$XEDA_GENERATED/{declaration}`"
                )
            return ""
        directory = generator.lookup_directory(unit.root)
        found = [directory / path for path in matches_in(directory, name)]
    except (ValidationError, ValueError, OSError, GenerationError):
        return ""
    if not found:
        return ""
    return (
        f"A file of that name exists at {_shown(found)}, in the directory the generator runs in: "
        "if the generator writes it there, write the entry as "
        f"`{found[0].relative_to(unit.root).as_posix()}`"
    )


def _shown(files: Sequence[Path]) -> str:
    """Name the first `HINT_FILES` of `files`, and how many more there are."""
    shown = ", ".join(f"`{path}`" for path in files[:HINT_FILES])
    more = f" and {len(files) - HINT_FILES} more" if len(files) > HINT_FILES else ""
    return shown + more


def refuse_missing(
    design: Design,
    generations: Generations | None,
    flow_class: type[Flow],
    settings: Flow.Settings,
) -> None:
    """Raise `MissingGenerated` for the first declaration that the node reads and nothing matches."""
    if generations is None:
        return
    unmatched = _unmatched(design, generations, flow_class, settings)
    if unmatched:
        source, generation = unmatched[0]
        name = str(source.output_name)
        raise MissingGenerated(
            name,
            generation.description if generation else "the generator",
            misplaced_hint(design, source.owner, name, generation.storage if generation else None),
        )


def located_paths(generations: Generations | None, path: Path) -> list[Path]:
    """Return the files that a source of a plan stands for.

    A plan names a generated source by the name or pattern that the design declares, which is
    relative. The files are those that it matched in the storage of its generator (the names of
    the generators of a design and its dependencies are told apart when the design loads). Any
    other path is a file already."""
    if generations is None or path.is_absolute():
        return [path]
    for generation in generations.by_owner.values():
        names = generation.matches.get(path.as_posix())
        if names is not None:
            return [generation.storage / name for name in names]
    return [path]


def delivery_directory(unit: Design, start_directory: Path) -> Path | None:
    """Return the directory that receives the copies of the generated sources of `unit`
    (`Generator.outputs_to`, with every path variable expanded), or None for a recipe without one.

    `start_directory` is what `$PWD` stands for."""
    generator = valid_generator(unit)
    return None if generator is None else generator.delivery_directory(unit.root, start_directory)


def reading_nodes(nodes: Iterable[Any], design: Design) -> dict[str | None, Any]:
    """Return, for each generator of `design` and its dependencies (by owner), the first node of
    `nodes` (in plan order) that reads a source that generator writes.

    That node delivers the copies of the generated sources: one node does, so no destination is
    named twice. A node reads the generated sources of the types it takes (`reads_generated`)."""
    found: dict[str | None, Any] = {}
    for node in nodes:
        for source in reads_generated(design, node.flow_class, node.settings):
            found.setdefault(source.owner, node)
    return found


def planned_deliveries(design: Design, start_directory: Path) -> dict[str | None, list[Delivery]]:
    """Return the copies of generated sources whose destinations are known before the generator
    runs, by owner: one for each declared name that is no pattern.

    A pattern names its files only once the generator has written them (`generated_deliveries`).
    The copies name no storage yet (`Delivery.root`), so they serve the checks that come before
    the generator, never a copy."""
    planned: dict[str | None, list[Delivery]] = {}
    for owner, unit in generating_units(design):
        directory = delivery_directory(unit, start_directory)
        if directory is None:
            continue
        planned[owner] = [
            Delivery(GENERATOR, PurePath(stored), directory / delivered_name(stored))
            for stored in map(stored_pattern, declarations(unit))
            if "*" not in stored
        ]
    return planned


def generated_deliveries(
    design: Design, generations: Generations, start_directory: Path
) -> dict[str | None, list[Delivery]]:
    """Return the copies of generated sources to make, by owner: one for each file that a
    declaration matched in the storage of its generator. A copy of a tree file goes under its path
    in the design tree, and any other file under its name in the storage (`delivered_name`)."""
    found: dict[str | None, list[Delivery]] = {}
    for owner, unit in generating_units(design):
        directory = delivery_directory(unit, start_directory)
        generation = generations.by_owner.get(owner)
        if directory is None or generation is None:
            continue
        names = sorted({name for matched in generation.matches.values() for name in matched})
        found[owner] = [
            Delivery(
                GENERATOR, PurePath(name), directory / delivered_name(name), generation.storage
            )
            for name in names
        ]
    return found
