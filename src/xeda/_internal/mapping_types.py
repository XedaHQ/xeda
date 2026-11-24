"""The shapes of the plain mappings that a plan or a record holds, as `TypedDict` classes.

A value that Xeda writes to JSON as an object, and that a user reads by key, has a class here. A
mapping with dynamic keys (a node name, a metric path, a setting) has none: it stays a `Mapping`.

A plan or a record holds each of these mappings read-only (`frozen.freeze`), so every key of its
class is `ReadOnly`: a type checker refuses an assignment. The classes of the mappings that
`xeda.introspect` returns are in `introspect_types`.

This module has no `from __future__ import annotations` on purpose: `TypedDict` reads `NotRequired`
from an annotation that is not text.
"""

from pathlib import Path
from typing import Literal, final

from typing_extensions import ReadOnly, TypedDict

__all__ = [
    "DependencyInfo",
    "DerivedDesign",
    "DerivedGeneratedSource",
    "DerivedPassedSource",
    "ExecutableCheck",
    "PlanOptions",
]


class PlanOptions(TypedDict):
    """The seven launch options that a plan was made with (`Plan.options`).

    The mapping is read-only."""

    #: Run every node, also a node whose last run is fresh.
    rebuild_all: ReadOnly[bool]
    #: Empty the run directory of each node before it runs, and run every node.
    clean: ReadOnly[bool]
    #: Remove the other run directories of every node of the plan before the launch.
    prune_variants: ReadOnly[bool]
    #: The absolute directory that the outputs of the requested flow are copied to, or `None`.
    outputs_to: ReadOnly[Path | None]
    #: Replace a file at a delivery destination even if Xeda did not write it.
    overwrite_outputs: ReadOnly[bool]
    #: After the run, keep only the settings, the record, the artifacts and the declared outputs.
    post_cleanup: ReadOnly[bool]
    #: After the run, remove the run directory. This implies `post_cleanup`.
    post_cleanup_purge: ReadOnly[bool]


class ExecutableCheck(TypedDict):
    """Whether the main program of a node is on its `PATH` (`PlanNode.executable_check`).

    The mapping is read-only."""

    #: The name of the main program.
    executable: ReadOnly[str]
    #: `found`, `missing`, or `unknown` when Xeda cannot tell (the flow runs in a container, or
    #: the program is a relative path).
    state: ReadOnly[Literal["found", "missing", "unknown"]]
    #: The absolute path of the program for `found`, else `None`.
    path: ReadOnly[str | None]
    #: Why the state is `unknown`, else `None`.
    reason: ReadOnly[str | None]


class DependencyInfo(TypedDict):
    """A dependency of a design as planning resolved it (`RunRecord.dependencies`).

    A Git dependency has all four keys. A local dependency has only the `design_file`. The mapping
    is read-only."""

    #: The URL of the repository, with no user name and no password, or `None`.
    repository: ReadOnly[str | None]
    #: The branch or tag as written, or `None` for a pinned commit or the `HEAD`.
    reference: ReadOnly[str | None]
    #: The commit that the reference named, or `None` for a local dependency.
    commit: ReadOnly[str | None]
    #: The path of the design file, in the repository or as written.
    design_file: ReadOnly[str]


@final
class DerivedGeneratedSource(TypedDict):
    """A source that a converter generated (`DerivedDesign.sources`): a file of the run."""

    #: The name of the file, relative to the run directory. It is one of `OutputRecord.names`.
    generated: ReadOnly[str]
    #: The name of the `SourceType` of the file.
    type: ReadOnly[str]


@final
class DerivedPassedSource(TypedDict):
    """A source of the input design that a converter passes through (`DerivedDesign.sources`)."""

    #: The index of the source in the input design.
    source: ReadOnly[int]
    #: The file of the source, as the input design names it.
    file: ReadOnly[str]
    #: The name of the `SourceType` of the file.
    type: ReadOnly[str]


class DerivedDesign(TypedDict):
    """The design that a converter derived (`OutputRecord.design`). The mapping is read-only."""

    #: The top module of the derived design, or `None`.
    top: ReadOnly[str | None]
    #: The hash of the design that the converter read.
    input_design: ReadOnly[str]
    #: The sources in order. A source has the key `generated` or the key `source`.
    sources: ReadOnly[tuple[DerivedGeneratedSource | DerivedPassedSource, ...]]
