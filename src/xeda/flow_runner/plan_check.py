"""The check that a plan still holds: `Workspace.run(plan)` compares it with a fresh planning.

Freshness is an observation, so the states and their reasons are no part of the comparison: a
launch judges freshness again. The graph, the identity and the run directory of each node, the
deliveries, the settings (the locator settings among them), the commits of the Git dependencies
and the options of the workspace are. Each function returns a text that names the first
difference, or `None`. This module never imports `default_runner`.
"""

from __future__ import annotations

from ..plan import Plan, PlanNode
from .launch_options import WorkspaceOptions

__all__ = ["workspace_difference", "plan_difference"]


def workspace_difference(planned: WorkspaceOptions | None, current: WorkspaceOptions) -> str | None:
    """Return the first option of the workspace that differs from the one of the plan."""
    if planned is None or planned == current:
        return None
    if planned.remote != current.remote:
        return f"the remote host differs: the plan was made with {planned.remote!r}"
    if planned.run_root != current.run_root:
        return f"the run root differs: the plan was made for {planned.run_root}"
    for name in ("hashed_run_dirs", "tool_output"):
        was, now = getattr(planned, name), getattr(current, name)
        if was != now:
            return f"`{name}` differs: the plan was made with {was!r}, this workspace has {now!r}"
    if planned.confirm is not current.confirm:
        return "`confirm` differs: the plan was made with another callback"
    return "`local_settings` differ from those that the plan was made with"


def _graph_difference(expected: PlanNode, fresh: PlanNode) -> str | None:
    for name in ("flow", "inputs", "outputs_enabled", "input_types", "output_types", "action"):
        if getattr(expected, name) != getattr(fresh, name):
            return f"the graph changed: `{name}` of node {expected.name} differs"
    return None


def _git_dependencies(plan: Plan) -> dict[int, tuple[str | None, str | None]]:
    """The `(repository, commit)` of each Git dependency of `plan`, by its index in the entries.
    Two entries of one repository at different commits have different indexes."""
    return {
        index: (entry["repository"], entry["commit"])
        for index, entry in enumerate(plan._dependencies)
        if entry["commit"] is not None
    }


def plan_difference(expected: Plan, fresh: Plan) -> str | None:
    """Return the first difference between `expected` and `fresh`, a new planning of its spec."""
    if tuple(expected.nodes) != tuple(fresh.nodes) or expected.roots != fresh.roots:
        return (
            f"the graph changed: the nodes were {list(expected.nodes)}, "
            f"they are now {list(fresh.nodes)}"
        )
    for name, node in expected.nodes.items():
        found = _graph_difference(node, fresh.nodes[name])
        if found:
            return found
    for name, node in expected.nodes.items():
        now = fresh.nodes[name]
        if node.identity != now.identity:
            return f"the identity of node {name} changed"
        if node.run_dir != now.run_dir:
            return f"the run directory of node {name} was {node.run_dir}, it is now {now.run_dir}"
    for name, node in expected.nodes.items():
        if node.deliveries != fresh.nodes[name].deliveries:
            return f"the deliveries of node {name} changed"
    for name, node in expected.nodes.items():
        if node.settings != fresh.nodes[name].settings:
            return f"the settings of node {name} changed"
    resolved = _git_dependencies(fresh)
    for index, (repository, commit) in _git_dependencies(expected).items():
        current = resolved.pop(index, None)
        if current is None:
            return f"dependency {repository} was removed"
        if current != (repository, commit):
            return f"dependency {current[0]} now resolves to {current[1]}"
    for repository, _commit in resolved.values():
        return f"dependency {repository} was added"
    return None
