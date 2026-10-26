"""The generation stage of a launch: run the design's generator into its storage, and give the
flows a view of the design whose generated sources name their files.

A design declares the sources that its generator writes (`Source.declaration`), and loading the
design runs nothing. A launch generates after every declarative check and before the first
producer. The generator writes into a storage under the run root, named by the configuration it
serves (`generation_key`), so two variants of one design keep their own files. The design that
the caller gave never changes. The flows get a copy (`execution_design`) whose generated sources
are the files in the storage.

A design may merge dependencies that have generators of their own. Each generator is a unit of
generation (`Design.generation_units`, dependencies first, then the design itself), with a storage
and a key of its own, and a launch holds the leases of them all (`Generations`). The sources of a
dependency's generator keep that dependency as their owner (`Source.owner`).

Planning writes nothing here. `inspect_generation` reads the records of the storages, and says
whether a launch would run a generator.

The functions are free functions with explicit arguments, and this module never imports
`default_runner`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ..dataclass import ValidationError
from ..design import Design, Generator, Source, SourceType, already_listed, generator_of
from ..flow import Flow
from ..frozen import FrozenDict
from ..generation import (
    KEY_LENGTH,
    REBUILD_ALL,
    GenerationError,
    MissingGenerated,
    executable_identity,
    generation_storage,
    input_digests,
    judge,
    matches_in,
    recipe_identity,
    write_record,
)
from ..run_dir import RunDirectory, RunDirectoryError
from ..utils import NonZeroExitCode, semantic_hash
from .configuration import design_configuration
from .run_lock import storage_lock

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
    "located_paths",
    "missing_declarations",
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
    it. A declaration may match nothing: a node that reads it fails (`MissingGenerated`)."""

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
        (unit.root_path.as_posix(), unit) for unit in design.generation_units
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
    try:
        generator = generator_of(design.rtl, root=design.root_path)
    except (ValidationError, ValueError) as error:
        # What the recipe names went since the design loaded (a file of `sources`, say).
        raise GenerationError(f"the recipe of the generator is not valid now: {error}") from error
    assert generator is not None
    environ = dict(os.environ) if environ is None else environ
    identity = {
        "recipe": recipe_identity(generator),
        "configuration": design_configuration(design),
        "inputs": input_digests(generator, design.root_path),
        "executable": executable_identity(generator, design.root_path, environ),
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
    exclusive lock of the storage, so a second launch of the same configuration waits, finds
    the record, and generates nothing. With `rebuild_all` the generator always runs.

    The generator runs in the storage, or in the directory its recipe names (`cwd`), with
    `DESIGN_ROOT` and `XEDA_GENERATED` in its environment. The design tree is never written. A
    failure raises `GenerationError`, and leaves no storage behind. `run_root` exists."""
    if design.rtl.generator is None:
        return None
    environ = dict(os.environ) if environ is None else environ
    names = declarations(design)
    while True:
        key, generator = generation_key(design, environ)
        storage = generation_storage(run_root, design.name, key)
        description = generator.describe(design.name, design.root_path)
        with storage_lock(storage, exclusive=True, run_root=run_root):
            if generation_key(design, environ)[0] != key:
                # What the generator reads changed while this launch waited for the lock: the
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
            owner = RunDirectory(run_root, run_root)
            try:
                if storage.exists():
                    RunDirectory.claimed(storage, run_root).delete()
                owner.unlinked(storage).mkdir(parents=True)
                generator.run(design.root_path, storage, environ)
                matches = {name: tuple(matches_in(storage, name)) for name in names}
                if generation_key(design, environ)[0] == key:
                    write_record(owner, storage, key, generator, environ, matches)
                else:
                    # What the generator reads changed while it ran, itself perhaps (bytecode in
                    # a source directory): its output is not that of this key, so nothing vouches
                    # for it, and the storage of this key goes. The launch generates again.
                    log.info(
                        "The inputs of %s changed while it ran: keeping no output of this key",
                        description,
                    )
                    _discard(storage, run_root)
            except BaseException as error:
                _discard(storage, run_root)
                if isinstance(error, (NonZeroExitCode, OSError, ValueError, RunDirectoryError)):
                    raise GenerationError(f"{description} failed: {error}") from error
                raise
        return Generation(storage, key, matches, description)


def _discard(storage: Path, run_root: Path) -> None:
    """Remove a storage that a failed generation left, so that no half-written output stays."""
    try:
        if storage.exists():
            RunDirectory.claimed(storage, run_root).delete()
    except (OSError, RunDirectoryError) as error:  # the failure of the generator comes first
        log.warning("Cannot remove the incomplete output %s: %s", storage, error)


@contextmanager
def generated(
    design: Design,
    *,
    run_root: Path,
    rebuild_all: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Iterator[Generations | None]:
    """Generate (`generate`) for the design and its dependencies that have generators, then hold
    the shared lease of every storage while the block runs. None: nothing generates.

    The lease keeps any other launch from regenerating or removing a storage while the launch
    reads it. A dependency generates before the design that merged it. Xeda cannot turn the
    exclusive lock of a generation into the shared lease, so it releases the first and takes the
    second, and then judges every storage again: a launch that regenerated in between left a
    current storage, or the loop generates again. The block gets the files that this judgment
    found, and never a storage that it did not accept (one that went, or whose inherited
    environment changed, or whose generator changes what it reads while it runs): after
    `ATTEMPTS` tries the launch fails with a `GenerationError`.

    The locks cannot make two launches wait for each other. A generation holds one exclusive
    lock and waits for no other lock while it holds it. The leases come after all the
    generations, shared, in the order of the paths of the storages, so a launch that waits for a
    lease holds shared leases only, and only a generation, which waits for nothing, holds the
    lock that it waits for."""
    units = generating_units(design)
    if not units:
        yield None
        return
    environ = dict(os.environ) if environ is None else environ
    forced = rebuild_all
    reason: str | None = None
    description = "the generator"
    for _attempt in range(ATTEMPTS):
        ran = [
            (
                owner,
                unit,
                _generated(unit, run_root=run_root, rebuild_all=forced, environ=environ),
            )
            for owner, unit in units
        ]
        with ExitStack() as leases:
            for storage in sorted({found.storage for _, _, found in ran}):
                leases.enter_context(storage_lock(storage, exclusive=False, run_root=run_root))
            judged: dict[str | None, Generation] = {}
            for owner, unit, found in ran:
                description = found.description
                # What the unit reads may have changed while a later unit generated: the storage
                # it holds must be that of its key now.
                current, generator = generation_key(unit, environ)
                matches: Mapping[str, tuple[str, ...]] = {}
                if current != found.key:
                    reason = "what its generator reads changed while it ran"
                else:
                    reason, matches = judge(
                        found.storage,
                        found.key,
                        declarations(unit),
                        generator,
                        environ,
                        judging=False,
                    )
                if reason is not None:
                    break
                judged[owner] = Generation(found.storage, found.key, matches, found.description)
            else:
                yield Generations(judged)
                return
        forced = False
    raise GenerationError(
        f"{description} left no output that Xeda could vouch for after {ATTEMPTS} "
        f"tries: {reason}. A generator that changes what it reads while it runs (a file it "
        "writes into a directory in its `sources`) cannot be judged: name only what it reads"
    )


def _generated(
    unit: Design, *, run_root: Path, rebuild_all: bool, environ: Mapping[str, str]
) -> Generation:
    found = generate(unit, run_root=run_root, rebuild_all=rebuild_all, environ=environ)
    assert found is not None
    return found


def execution_design(design: Design, generations: Generations | None) -> Design:
    """Return the design that the flows of a launch get: `design`, with the files in place of
    its generated sources.

    Each declaration of a generated source (`gen/*.v`) gives, in its place, one source for each
    file that it matches in the storage of its owner, in sorted order and with its type. A file
    that two declarations match is listed once. A declaration that matches nothing gives no
    source (see `missing_declarations`). The design that the caller gave is not changed."""
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
                output_name=name,
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
) -> list[tuple[str, Generation | None]]:
    """The declarations that a node reads, with the generation that should have written each, for
    those that matched no file."""
    unmatched: list[tuple[str, Generation | None]] = []
    for source in reads_generated(design, flow_class, settings):
        generation = generations.of(source)
        if generation is None or not generation.matches.get(str(source.output_name)):
            unmatched.append((str(source.output_name), generation))
    return unmatched


def missing_declarations(
    design: Design,
    generations: Generations,
    flow_class: type[Flow],
    settings: Flow.Settings,
) -> tuple[str, ...]:
    """Return the declarations that a node reads and that matched no file after the generation."""
    return tuple(name for name, _ in _unmatched(design, generations, flow_class, settings))


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
        name, generation = unmatched[0]
        raise MissingGenerated(name, generation.description if generation else "the generator")


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
