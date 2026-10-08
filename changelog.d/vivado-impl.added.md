- Add the `vivado_impl` flow: `xeda run yosys_fpga+vivado_impl design.yaml` places and routes the
  yosys netlist in Vivado. A flat Xilinx `yosys_fpga` run now also writes `netlist.edif` (about
  1.3 MB). The new `netlist_edif` setting changes the identity of every `yosys_fpga` run, and of
  `nextpnr` after it, once: each runs again, and `--hashed-run-dirs` directories get new names.
