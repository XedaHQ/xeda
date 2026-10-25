- `sim.time` is now an integer number of femtoseconds, and `sim.time_unit` is gone. `yosys_fpga`
  counts each cell type in `cell_types` and reports `latch`, `bram.ramb18`, `bram.ramb36` and `dsp`,
  and the temperatures of `vivado_power` have units.
