"""The second stage of a launch: who the run is and where it runs.

A run is identified by the hash of its node (`bindings.node_identity`); its run directory is
decided from that and the launcher's run root alone. Its design hash is evidence of freshness. Free functions with
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
    refuse_link_on_way,
    run_directory_problem,
)
from ..run_root import ensure_run_root
from .bindings import node_identity
from .configuration import configuration_hash
from .resolver import PlanNode


@dataclass(frozen=True)
class RunDirPolicy:
    """What one launch does with its run directory (see `FlowLauncher._run_dir_policy`).

    Decided per launch, never by writing to the settings of the launcher: every launch of a
    launcher shares those, each of its producer launches included.
    """

    clean: bool
    prune_variants: bool
    post_cleanup: bool
    post_cleanup_purge: bool

    @property
    def purges(self) -> bool:
        """Whether the clean-up after the launch deletes the run directory (`_clean_up`)."""
        return self.post_cleanup and self.post_cleanup_purge


class PolicySettings(Protocol):
    """The launcher settings `run_dir_policy` reads."""

    clean: bool
    prune_variants: bool
    post_cleanup: bool
    post_cleanup_purge: bool


def run_dir_policy(settings: PolicySettings) -> RunDirPolicy:
    """Return what a launch does with its run directory, from the settings of the launcher.

    The settings are the same for every flow, producers included. Purging is a clean-up after
    the run, so `post_cleanup_purge` alone makes `post_cleanup` true here. This is the one
    place where the two settings meet. `_clean_up`, the pending clean-ups and the delivery by
    move follow it."""
    return RunDirPolicy(
        clean=settings.clean,
        prune_variants=settings.prune_variants,
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
    """Return the path `<run root>/<design>[/<target>]/<node>`, or `<node>_<identity>` if hashed.

    The second form is the name with hashed run directories.

    The path lies strictly inside the run root and is resolved. Otherwise a `RunDirectoryError`
    is raised, before anything (the lock beside it included) is written. No name between the run
    root and the run directory (the design, the target) is a symbolic link (`refuse_link_on_way`;
    a run root that is a link is fine). A link at the name of the run directory is
    used only if it is a run directory of the node by `run_directory_problem`, the rule by which
    scrub lists run directories: it leads to a directory named like one beside it. A design
    selected as a target runs in the directory of that target, as its producers do. A design
    without a target runs in the design directory, as it always has. `target` is the design's
    own (`Design.target`), never read from the launcher, which may be reused for another
    design."""
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
    refuse_link_on_way(run_path.parent, run_root)
    if not RunDirectory.lies_under(run_path, run_root):
        raise RunDirectoryError(
            f"{run_path} leads out of the run root {run_root} (through a symbolic "
            "link): Xeda runs only inside its run root; remove the link"
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
    """The two methods of the launcher that give the path of a run directory.

    `run_identity` asks them for the path."""

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
) -> tuple[str, Path, str]:
    """Return `(flowrun_hash, run_path, settings_hash)`.

    The hash of the run is the identity of its node (`bindings.node_identity`): its settings, its
    design configuration and the ordered origins of its inputs. The configuration is the one
    the plan froze for the node (`PlanNode.configuration`, checked by the launcher). It holds no
    content of the design, so it is known before the generator of the design has run."""
    settings_hash = flow_run_hash(flow_name, settings, design.name)
    flowrun_hash = node_identity(
        settings_hash, configuration_hash(node.configuration), node.origins
    )
    # A directory the flow cannot work in is refused on the path alone: nothing is created yet
    # (`get_flow_run_path` creates the run root).
    flow_class.check_run_directory(
        settings,
        paths.run_path_of(design.name, node.name, flowrun_hash, target=design.target),
    )
    run_path = paths.get_flow_run_path(design.name, node.name, flowrun_hash, target=design.target)
    return flowrun_hash, run_path, settings_hash


def claim_run_dir(claims: set[Path], run_path: Path) -> None:
    """Record in `claims` that the launch entered `run_path`.

    The plan has one node per flow, so a directory is entered once. A second entry would run
    over what the first produced."""
    key = Path(os.path.abspath(run_path))
    if key in claims:
        raise FlowFatalError(f"{run_path} is entered twice in one launch")
    claims.add(key)
