"""The options of a launch and of the workspace that makes it.

`LaunchOptions` are the seven options that belong to one plan or one run. `WorkspaceOptions` are
the options of the workspace: the run root, the layout of the run directories, the output of the
tools, the answer to a question, the remote host and the private settings. A plan keeps both, so
that running it can check that nothing changed.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..local_settings import LocalSettings
from ..types import Question

__all__ = ["LaunchOptions", "WorkspaceOptions"]


@dataclass(frozen=True)
class LaunchOptions:
    """The options of one launch. `outputs_to` is an absolute path, or `None`."""

    rebuild_all: bool = False
    clean: bool = False
    prune_variants: bool = False
    outputs_to: Path | None = None
    overwrite_outputs: bool = False
    post_cleanup: bool = False
    post_cleanup_purge: bool = False

    def __post_init__(self) -> None:
        if self.outputs_to is not None:
            object.__setattr__(self, "outputs_to", Path(os.path.abspath(self.outputs_to)))

    def as_dict(self) -> dict[str, Any]:
        """Return the seven options by name, as `Plan.options` holds them."""
        return {
            "rebuild_all": self.rebuild_all,
            "clean": self.clean,
            "prune_variants": self.prune_variants,
            "outputs_to": self.outputs_to,
            "overwrite_outputs": self.overwrite_outputs,
            "post_cleanup": self.post_cleanup,
            "post_cleanup_purge": self.post_cleanup_purge,
        }


@dataclass(frozen=True)
class WorkspaceOptions:
    """The options of a workspace, as a value that two plans compare.

    Two values are equal when their `confirm` is the same callable (an identity, not an
    equality) and every other option is equal."""

    run_root: Path
    hashed_run_dirs: bool
    tool_output: str
    confirm: Callable[[Question], bool] | None = field(compare=False)
    remote: Any | None
    local_settings: LocalSettings | None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkspaceOptions):
            return NotImplemented
        return self.confirm is other.confirm and all(
            getattr(self, name) == getattr(other, name)
            for name in ("run_root", "hashed_run_dirs", "tool_output", "remote", "local_settings")
        )

    def __hash__(self) -> int:
        return hash((self.run_root, self.hashed_run_dirs, self.tool_output, id(self.confirm)))
