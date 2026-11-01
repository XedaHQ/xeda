"""Each line is an error that a type checker must report. An unused `type: ignore` fails the run."""

import xeda

design = xeda.Design.load("design.yaml")
spec = xeda.Spec("ghdl", design)
plan = xeda.Workspace().plan(spec)

xeda.run(plan)  # type: ignore[arg-type]
xeda.run(spec, nosuch=1)  # type: ignore[call-arg]
x: int = xeda.run(spec)  # type: ignore[assignment]
xeda.run(spec).nonexistent  # type: ignore[attr-defined]
