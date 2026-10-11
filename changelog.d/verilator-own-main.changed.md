- `verilator` now runs an HDL top with Verilator's own `--main`, and requires Verilator 5.044.
  `stop_time` is an error (end the testbench with `$finish`, or use `timeout`); `sim.time` of a run
  that drained is empty; `--coverage` now writes `coverage.dat`.
