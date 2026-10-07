- Add the `vivado_impl` flow: `xeda run yosys_fpga+vivado_impl design.yaml` places and routes the
  yosys netlist in Vivado. `yosys_fpga` now writes that netlist, `netlist.edif`, for a flat Xilinx
  synthesis.
