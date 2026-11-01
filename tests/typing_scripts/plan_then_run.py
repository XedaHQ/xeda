"""Plan a launch, read the plan, and run the plan with the workspace that made it."""

from collections.abc import Mapping
from typing import Literal, assert_type

import xeda

design = xeda.Design.load("design.yaml")
spec = xeda.Spec("yosys_fpga", design)

ws = xeda.Workspace()
plan = ws.plan(spec, clean=True)
assert_type(plan, xeda.Plan)
assert_type(plan.spec, xeda.Spec)
assert_type(plan.roots, tuple[str, ...])
assert_type(plan.nodes, Mapping[str, xeda.PlanNode])
for node in plan.nodes.values():
    assert_type(node.name, str)
    assert_type(node.state, Literal["fresh", "rejected", "stale", "unknown", "blocked"])
    assert_type(node.inputs, tuple[xeda.PlanInput, ...])

result = ws.run(plan)
assert_type(result, xeda.Result)
