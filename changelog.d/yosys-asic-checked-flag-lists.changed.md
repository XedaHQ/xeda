- With a standard-cell mapping, `yosys` refuses each flag in `synth_flags`, `abc_flags`,
  `read_verilog_flags` and `netlist_verilog_flags` that a setting or the script already gives,
  and names the setting. `read_systemverilog_flags` now reaches the slang front end too, and with
  `systemverilog: slang` it refuses the flags that Xeda sets itself.
