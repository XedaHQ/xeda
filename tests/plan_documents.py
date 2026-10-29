"""The documents of the nodes of a plan that the launcher resolved, for the tests of the resolver."""

from xeda.flow_runner.plan_freshness import NodeFreshness
from xeda.plan import node_document, plan_node


def plan_nodes(plan) -> list[dict]:
    """Return the nodes of the resolved `plan` as `Plan.to_dict()` writes them.

    The state of every node is `stale`: the resolver judges no freshness."""
    return [
        node_document(plan_node(node, NodeFreshness("stale", "no completed previous run"), ()))
        for node in plan.nodes
    ]
