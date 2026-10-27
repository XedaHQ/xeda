- Add `out_of_context` to `yosys_fpga`. The synthesis then puts no IO buffers on the ports and no
  clock buffer on the clock. The new setting changes the identity, so `yosys_fpga` runs once more.
  The setting is shared: `yosys_fpga` and `nextpnr` in one chain must agree on it.
