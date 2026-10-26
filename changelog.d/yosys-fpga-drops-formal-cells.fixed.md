- `yosys_fpga` removes the cells of `assert` and `$display` from its netlists. They no longer
  reach `netlist.json`, `netlist.edif`, `netlist.v` or the utilization report.
