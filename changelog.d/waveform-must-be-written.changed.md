- A `verilator` run without cocotb fails when it wrote no `dump.<format>` for the trace format it
  was built with: the testbench must call `$dumpfile("dump.vcd")`. The same holds for the
  Verilator backend of `bsc_sim`.
