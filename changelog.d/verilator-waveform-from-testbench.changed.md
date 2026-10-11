- `verilator` waveforms: `vcd`, `fst` and `saif` (set to `true`) build the model with that trace
  format, and the testbench's `$dumpfile` names the file. Any other name or location is an error
  without cocotb, and so are two formats. The trace options keep Verilator's defaults.
