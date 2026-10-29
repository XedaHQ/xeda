"""The node locks of a launch: which run directories it holds shared, and which exclusive.

A launch locks the run directory of every node of its plan before it runs anything. A node that
the launch only reuses has a shared lock, a lease. A node that it may run has an exclusive lock.
A judgment that is made without locks is a hint: a node that is not up to date, or whose state
is unknown, gets an exclusive lock. Under the locks the launch judges again. A node that was
shared and is not up to date makes the launch release everything and start again with that node
exclusive (`NeedsExclusive`). A launch never turns a shared lock into an exclusive one while it
holds other locks.

The functions are free functions with explicit arguments, and this module never imports
`default_runner`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path

from .plan_freshness import NodeFreshness
from .run_lock import LockRequest

log = logging.getLogger(__name__)

__all__ = ["NeedsExclusive", "NeedsPruning", "NodeLocks"]

#: The states of a node that a launch reuses without writing in its run directory.
REUSED = ("fresh", "rejected")


class NeedsExclusive(BaseException):
    """A node that the launch holds shared must be written: start again with it exclusive.

    The exception is no failure, so it is no `Exception`: it passes the handlers that record a
    failure of a node. The launcher catches it, releases every lock and acquires again."""

    def __init__(self, nodes: Iterable[str]) -> None:
        self.nodes = frozenset(nodes)
        super().__init__(f"{', '.join(sorted(self.nodes))} must be locked exclusively")


class NeedsPruning(BaseException):
    """The plan that the caller gave still holds, and the launch must prune the other run
    directories before it takes its locks again.

    A prune takes one lock at a time and holds none else, so it cannot run under the locks of the
    launch. Like `NeedsExclusive`, it is no failure and no `Exception`."""


class NodeLocks:
    """The locks of the run directories of a plan, and the judgment that they wait for.

    `paths` maps the name of each node to its run directory. A node in `exclusive` is locked
    exclusively, and a node leaves no set it entered: a launch that started again with a node
    exclusive keeps it so. `judge` judges the plan without locks (`plan_freshness.judge_plan`).
    With `everything_runs` (`--rebuild-all`, `--clean`) every node is exclusive and there is
    nothing to judge."""

    def __init__(
        self,
        paths: Mapping[str, Path],
        judge: Callable[[], Mapping[str, NodeFreshness]],
        *,
        everything_runs: bool,
        exclusive: Collection[str] = (),
    ) -> None:
        self.paths = dict(paths)
        self._judge = judge
        self.exclusive: set[str] = set(exclusive)
        if everything_runs:
            self.exclusive |= self.paths.keys()
        self._hinted = everything_runs

    def _states(self) -> Mapping[str, NodeFreshness] | None:
        try:
            return self._judge()
        except Exception as error:  # noqa: BLE001 - a hint: no answer means nothing is reused
            log.debug("The nodes cannot be judged without a run: %s", error)
            return None

    def hint(self) -> None:
        """Judge the plan without locks. Every node that is not up to date becomes exclusive."""
        if self._hinted:
            return
        self._hinted = True
        states = self._states()
        late = set(self.paths) if states is None else self._not_reused(states)
        self.exclusive |= late

    def _not_reused(self, states: Mapping[str, NodeFreshness]) -> set[str]:
        return {
            name for name in self.paths if name not in states or states[name].state not in REUSED
        }

    def requests(self) -> list[LockRequest]:
        """The run-directory locks to take now."""
        return [
            LockRequest("run_dir", path, name in self.exclusive)
            for name, path in self.paths.items()
        ]

    def settled(self) -> bool:
        """Judge the plan under the locks. False: some shared node must be exclusive."""
        if len(self.exclusive) == len(self.paths):
            return True
        states = self._states()
        late = (set(self.paths) if states is None else self._not_reused(states)) - self.exclusive
        if not late:
            return True
        log.debug("Locking exclusively: %s", ", ".join(sorted(late)))
        self.exclusive |= late
        return False

    def promote(self, nodes: Iterable[str]) -> None:
        """Make `nodes` exclusive in the next acquisition."""
        self.exclusive |= set(nodes)
