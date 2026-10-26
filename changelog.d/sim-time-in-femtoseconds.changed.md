- `sim.time` is now an integer number of femtoseconds, and `sim.time_unit` is gone. `yosys`,
  `yosys_fpga` and `nextpnr` report their cell or bel types in `cell_types`, `yosys_fpga` reports
  `latch`, `bram.ramb18`, `bram.ramb36` and `dsp`, and the temperatures of `vivado_power` have
  units.
