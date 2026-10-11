- `nextpnr` stays up to date when `yosys_fpga` writes a netlist that differs only in the source
  locations (`src` attributes), as after a comment is added above the logic. Its reports then
  cite the lines of the netlist it placed. `list-flows --json` shows `equivalence` on inputs.
