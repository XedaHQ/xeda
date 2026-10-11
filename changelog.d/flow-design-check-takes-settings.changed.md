- `check_design_supported` and `runs_without_testbench_top` of a flow now take the flow's settings:
  an override has the signature `(cls, design, settings=None)`. An override that takes the design
  alone fails with a `TypeError` when a launch is planned: add the argument.
