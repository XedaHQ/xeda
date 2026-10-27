- `yosys_fpga` with `sta: true` no longer fails to write `netlist.json` for a design that maps a
  shift register or a distributed RAM. The timing analysis no longer changes the netlists.
