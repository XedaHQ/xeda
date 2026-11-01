"""Run a flow and read what it left: the everyday script, with the type of every public call."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, assert_type

import xeda

design = xeda.Design.load("design.yaml", target="ecp5")
assert_type(design, xeda.Design)
assert_type(design.name, str)
assert_type(design.root, Path)

spec = xeda.Spec("nextpnr", design, settings={"nextpnr.seed": 7})
assert_type(spec, xeda.Spec)

result = xeda.run(spec, hashed_run_dirs=True, run_root="xeda_run")
assert_type(result, xeda.Result)
assert_type(result.outcome, xeda.Outcome)
assert_type(result.passed, bool)
assert_type(result.error, xeda.ErrorInfo | None)
assert_type(result.nodes, Mapping[str, xeda.NodeResult])
assert_type(result.require_passed(), xeda.Result)
assert_type(result.to_dict(), dict[str, Any])

for name, node in result.nodes.items():
    assert_type(name, str)
    assert_type(node.disposition, xeda.Disposition)
    assert_type(node.record, xeda.RunRecord | None)
    assert_type(node.run_dir, Path)

record = result.record
assert_type(record, xeda.RunRecord)
assert_type(record.outcome, xeda.Outcome)
assert_type(record.metrics, Mapping[str, Any])
assert_type(record.run_dir, Path)
assert_type(record.runtime, float)

same = xeda.load(record.run_dir)
assert_type(same, xeda.RunRecord)

ws = xeda.Workspace("xeda_run", tool_output="terminal")
assert_type(ws, xeda.Workspace)
assert_type(ws.run_root, Path)
assert_type(ws.run(spec, rebuild_all=True), xeda.Result)
assert_type(xeda.Workspace.load(record.run_dir), xeda.RunRecord)

listing = ws.records(design_name=design.name, flow="nextpnr", target=None)
assert_type(listing, xeda.RunListing)
for found in listing:
    assert_type(found, xeda.RunRecord)
assert_type(listing.problems, tuple[xeda.RecordProblem, ...])

if xeda.run(spec).outcome is xeda.Outcome.PASSED:
    print(record.metrics["fmax"])
