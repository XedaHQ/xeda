- A `yosys` netlist that is not mapped to a Liberty library (`gates`, `lut`, or no `platform`,
  `platform_file` or `liberty`) is now the output `netlist_generic`, type `GenericVerilogNetlist`.
  Read `outputs.netlist_generic` in `results.json`; no flow takes it as an input.
