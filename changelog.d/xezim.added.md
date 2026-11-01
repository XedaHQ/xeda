- Add the `xezim` flow, which simulates Verilog and SystemVerilog with the xezim simulator. A
  Verilog `$stop` passes on it, so a testbench must use `$error` or `$fatal` to report a failure.
