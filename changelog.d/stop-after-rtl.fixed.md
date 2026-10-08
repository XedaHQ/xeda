- `stop_after: rtl` on `yosys` and `yosys_fpga` now succeeds with the RTL outputs it wrote.
  Set `netlist_json` and `netlist_verilog` to null with it: a setting that asks for a
  netlist or a report is refused.
