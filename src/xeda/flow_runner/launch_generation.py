"""The generation stage of a launch: run the design's generator into its storage, and give the
flows a view of the design whose generated sources name their files.

A design declares the sources that its generator writes (`Source.declaration`), and loading the
design runs nothing. A launch generates after every declarative check and before the first
producer. The generator writes into a storage under the run root, named by the configuration it
serves (`generation_key`), so two variants of one design keep their own files. The design that
the caller gave never changes. The flows get a copy (`execution_design`) whose generated sources
are the files in the storage.

Planning writes nothing here. `inspect_generation` reads the record of the storage, and says
whether a launch would run the generator.

The functions are free functions with explicit arguments, and this module never imports
`default_runner`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

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
    "Inspection",
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
class Inspection:
    """What a read-only look at the storage of a design shows.

    `generator` says that the design has a generator. `generation` is the storage as it is, when
    the record vouches for it, and None when a launch would run the generator, for `reason`."""

    generator: bool
    generation: Generation | None = None
    reason: str = ""


def declarations(design: Design) -> tuple[str, ...]:
    """Return the names and patterns of the generated sources of `design`, in order."""
    return tuple(
        source.output_name
        for source in design.rtl.sources
        if source.generated and not source.located and source.output_name is not None
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


def inspect_generation(
    design: Design,
    run_root: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    rebuild_all: bool = False,
) -> Inspection:
    """Look at the storage of `design` and say whether a launch would run its generator.

    It creates and writes nothing, and takes no lock. `run_root` is None when there is none yet.
    The answer is an observation: a launch judges again under its locks."""
    if design.rtl.generator is None:
        return Inspection(False)
    if rebuild_all:
        return Inspection(True, None, REBUILD_ALL)
    environ = dict(os.environ) if environ is None else environ
    try:
        key, generator = generation_key(design, environ)
    except (GenerationError, ValueError, OSError) as error:
        return Inspection(True, None, str(error))
    if run_root is None:
        return Inspection(True, None, "there is no run root yet")
    try:
        storage = generation_storage(run_root, design.name, key)
    except RunDirectoryError as error:
        return Inspection(True, None, str(error))
    reason, matches = judge(storage, key, declarations(design), generator, environ)
    if reason is not None:
        return Inspection(True, None, reason)
    return Inspection(True, Generation(storage, key, matches, generator.describe(design.name)))


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
) -> Iterator[Generation | None]:
    """Generate (`generate`), then hold the shared lease of the storage while the block runs.

    The lease keeps any other launch from regenerating or removing the storage while the
    launch reads it. Xeda cannot turn the exclusive lock of the generation into the shared
    lease, so it releases the first and takes the second, and then judges the storage again: a
    launch that regenerated in between left a current storage, or the loop generates again. The
    block gets the files that this judgment found, and never a storage that it did not accept
    (one that went, or whose inherited environment changed, or whose generator changes what it
    reads while it runs): after `ATTEMPTS` tries the launch fails with a `GenerationError`."""
    if design.rtl.generator is None:
        yield None
        return
    environ = dict(os.environ) if environ is None else environ
    forced = rebuild_all
    reason: str | None = None
    description = "the generator"
    for _attempt in range(ATTEMPTS):
        generation = generate(design, run_root=run_root, rebuild_all=forced, environ=environ)
        assert generation is not None
        description = generation.description
        with storage_lock(generation.storage, exclusive=False, run_root=run_root):
            reason, matches = judge(
                generation.storage,
                generation.key,
                declarations(design),
                generation_key(design, environ)[1],
                environ,
                judging=False,
            )
            if reason is None:
                yield Generation(
                    generation.storage, generation.key, matches, generation.description
                )
                return
        forced = False
    raise GenerationError(
        f"{description} left no output that Xeda could vouch for after {ATTEMPTS} "
        f"tries: {reason}. A generator that changes what it reads while it runs (a file it "
        "writes into a directory in its `sources`) cannot be judged: name only what it reads"
    )


def execution_design(design: Design, generation: Generation | None) -> Design:
    """Return the design that the flows of a launch get: `design`, with the files in place of
    its generated sources.

    Each declaration of a generated source (`gen/*.v`) gives, in its place, one source for each
    file that it matches, in sorted order and with its type. A file that two declarations match
    is listed once. A declaration that matches nothing gives no source (see
    `missing_declarations`). The design that the caller gave is not changed."""
    if generation is None:
        return design
    listed: list[Source] = []
    for source in design.rtl.sources:
        if not (source.generated and not source.located):
            listed.append(source)
            continue
        for name in generation.matches.get(str(source.output_name), ()):
            located = Source(
                {"file": str(generation.storage / name)},
                source.type,
                generated=True,
                output_name=name,
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


def missing_declarations(
    design: Design,
    generation: Generation,
    flow_class: type[Flow],
    settings: Flow.Settings,
) -> tuple[str, ...]:
    """Return the declarations that a node reads and that matched no file after the generation."""
    return tuple(
        str(source.output_name)
        for source in reads_generated(design, flow_class, settings)
        if not generation.matches.get(str(source.output_name))
    )


def refuse_missing(
    design: Design,
    generation: Generation | None,
    flow_class: type[Flow],
    settings: Flow.Settings,
) -> None:
    """Raise `MissingGenerated` for the first declaration that the node reads and nothing matches."""
    if generation is None:
        return
    missing = missing_declarations(design, generation, flow_class, settings)
    if missing:
        raise MissingGenerated(missing[0], generation.description)


def located_paths(generation: Generation | None, path: Path) -> list[Path]:
    """Return the files that a source of a plan stands for.

    A plan names a generated source by the name or pattern that the design declares, which is
    relative. The files are those that it matched in the storage. Any other path is a file
    already."""
    if generation is None or path.is_absolute():
        return [path]
    names = generation.matches.get(path.as_posix())
    if names is None:
        return [path]
    return [generation.storage / name for name in names]
