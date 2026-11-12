- `yosys_fpga` refuses `abc_dff: true` with ABC9 for ECP5 at planning: `synth_ecp5 -dff` of yosys
  leaves the M and CE pins of the flip-flops undriven then, so the netlist failed `check -assert`.
