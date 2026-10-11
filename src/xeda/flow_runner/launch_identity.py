"""The second stage of a launch: who the run is and where it runs.

A run is identified by its design hash and the hash of its node (`bindings.node_identity`); its
run directory is decided from those and the launcher's run root alone. Free functions with
explicit arguments. This module never imports `default_runner`: the launcher passes what each
function needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..design import DESIGN_NAME, Design, target_name_problem
from ..flow import Flow, FlowFatalError
from ..flow import flowrun_hash as flow_run_hash
from ..run_dir import (
    DIR_NAME_HASH_LEN,
    RunDirectory,
    RunDirectoryError,
    run_directory_problem,
)
from ..run_root import ensure_run_root
from .bindings import node_identity
from .resolver import PlanNode


@dataclass(frozen=True)
class RunDirPolicy:
    """What one launch does with its run directory (see `FlowLauncher._run_dir_policy`).

    Decided per launch, never by writing to the launcher's settings: every launch of a launcher
    shares those, each of its dependency launches included.
    """

    clean: bool
    scrub_old_runs: bool
    post_cleanup: bool
    post_cleanup_purge: bool

    @property
    def purges(self) -> bool:
        """Whether the clean-up after the launch deletes the run directory (`_clean_up`)."""
        return self.post_cleanup and self.post_cleanup_purge


class PolicySettings(Protocol):
    """The launcher settings `run_dir_policy` reads."""

    clean: bool
    scrub_old_runs: bool
    post_cleanup: bool
    post_cleanup_purge: bool


def run_dir_policy(settings: PolicySettings) -> RunDirPolicy:
    """What a launch does with its run directory: the launcher's settings, for every
    flow alike, dependencies included. Purging is a clean-up after the run, so
    `post_cleanup_purge` alone makes `post_cleanup` true here -- the one place where the two
    settings meet, which `_clean_up`, the pending clean-ups and the delivery by move follow."""
    return RunDirPolicy(
        clean=settings.clean,
        scrub_old_runs=settings.scrub_old_runs,
        post_cleanup=settings.post_cleanup or settings.post_cleanup_purge,
        post_cleanup_purge=settings.post_cleanup_purge,
    )


def run_path_of(
    run_root: Path,
    start: Path,
    hashed_run_dirs: bool,
    design_name: str,
    node_name: str,
    identity: str | None = None,
    *,
    target: str | None = None,
) -> Path:
    """`<run root>/<design>[/<target>]/<node>`, or `<node>_<identity>` with hashed run
    directories: strictly inside the run root, resolved -- a `RunDirectoryError` otherwise,
    before anything (the lock beside it included) is written. A link at that name is used
    only if it is a run directory of the node by `run_directory_problem`, the rule scrub
    lists run directories by: it leads to a directory named like one beside it. A design
    selected as a target runs in that target's directory, as its dependencies do; one
    without, as it always has. `target` is the design's own (`Design.target`), never read
    from the launcher, which may be reused for another design."""
    subdir = node_name
    if hashed_run_dirs and identity:
        subdir += f"_{identity[:DIR_NAME_HASH_LEN]}"
    if not DESIGN_NAME.fullmatch(design_name):
        raise RunDirectoryError(f"{design_name!r} is not a design name")
    parent = Path(design_name)
    if target is not None:
        problem = target_name_problem(target)
        if problem is not None:
            raise RunDirectoryError(problem)
        parent /= target
    ensure_run_root(run_root, start=start, create=False)
    run_path = run_root / parent / subdir
    if not RunDirectory.lies_under(run_path, run_root):
        raise RunDirectoryError(
            f"{run_path} leads out of the run root {run_root} (through a symbolic "
            "link): xeda runs only inside its run root; remove the link"
        )
    if run_path.is_symlink():  # the rule `xeda scrub` lists a run directory by
        beside = Path(os.path.realpath(run_path.parent))
        problem = run_directory_problem(run_path, node_name, beside)
        if problem is not None:
            raise RunDirectoryError(
                f"{run_path} cannot be the run directory of {node_name}: {problem}. Remove "
                f"the link, or make it lead to a run directory of {node_name} beside it."
            )
    return run_path


class RunPaths(Protocol):
    """The launcher's two run-path methods, which `run_identity` asks for the path."""

    def run_path_of(
        self,
        design_name: str,
        node_name: str,
        identity: str | None = None,
        *,
        target: str | None = None,
    ) -> Path: ...

    def get_flow_run_path(
        self,
        design_name: str,
        node_name: str,
        identity: str | None = None,
        *,
        target: str | None = None,
    ) -> Path: ...


def run_identity(
    paths: RunPaths,
    flow_class: type[Flow],
    flow_name: str,
    design: Design,
    settings: Flow.Settings,
    node: PlanNode,
) -> tuple[str, str, Path, str]:
    """`(design_hash, flowrun_hash, run_path, settings_hash)`. The run's hash is
    its node's identity (`bindings.node_identity`): its settings and the ordered origins of
    its inputs. The design
    counts by the parts the flow reads (`Flow.design_parts`) and the types of source it takes
    (`Flow.design_source_types`), so an edit to a testbench or a source the flow does not read
    changes nothing of it."""
    design_hash = design.parts_hash(flow_class.design_parts, flow_class.design_scope(settings))
    settings_hash = flow_run_hash(flow_name, settings, design.name)
    flowrun_hash = node_identity(settings_hash, node.origins)
    # a directory the flow cannot work in is refused on the path alone: nothing is created yet
    # (`get_flow_run_path` creates the run root)
    flow_class.check_run_directory(
        settings,
        paths.run_path_of(design.name, node.name, flowrun_hash, target=design.target),
    )
    run_path = paths.get_flow_run_path(design.name, node.name, flowrun_hash, target=design.target)
    return design_hash, flowrun_hash, run_path, settings_hash


def claim_run_dir(claims: set[Path], run_path: Path) -> None:
    """Record in `claims` that the launch entered `run_path`. The plan has one node per flow, so a
    directory is entered once; a second entry would run over what the first produced."""
    key = Path(os.path.abspath(run_path))
    if key in claims:
        raise FlowFatalError(f"{run_path} is entered twice in one launch")
    claims.add(key)
