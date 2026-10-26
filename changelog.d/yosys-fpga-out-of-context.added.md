- Add `out_of_context` to `yosys_fpga`. The synthesis then puts no IO buffers on the ports and no
  clock buffer on the clock, so the netlist is a block that a larger design can instantiate.
