- By default, `ghdl_synth` writes its Verilog to `outputs/<design>.v` and `nextpnr` writes the
  Xilinx placement dump to `outputs/<design>_placement.json`, the names a location already gave
  them. `yosys_sim`'s `cxxrtl.filename` is now a name in the run directory and takes no location:
  use `--outputs-to` to copy the file.
