"""Sweep the seeds of a design and read the table."""

from collections.abc import Mapping
from typing import Any, assert_type

import xeda

design = xeda.Design.load("design.yaml")
specs = [xeda.Spec("nextpnr", design, settings={"nextpnr.seed": seed}) for seed in range(4)]

ws = xeda.Workspace(hashed_run_dirs=True)
sweep = ws.sweep(specs, max_workers=2)
assert_type(sweep, xeda.SweepResult)
assert_type(sweep.canceled, bool)
assert_type(len(sweep), int)
assert_type(sweep.counts(), Mapping[xeda.Outcome, int])
assert_type(sweep.rows(metrics=["nextpnr.fmax"]), list[dict[str, Any]])

for point in sweep:
    assert_type(point, xeda.SweepPoint)
    assert_type(point.index, int)
    assert_type(point.spec, xeda.Spec)
    assert_type(point.result, xeda.Result)

table = sweep.to_pandas(metrics=["nextpnr.fmax"])
print(table)
