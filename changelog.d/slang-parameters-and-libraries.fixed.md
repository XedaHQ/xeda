- With slang, `rtl.parameters` now reach the top module (`read_slang -G`), and `verilog_lib` is
  read before the sources. Before, a parameter of a SystemVerilog top, and an instance of a
  `verilog_lib` module, failed the run.
