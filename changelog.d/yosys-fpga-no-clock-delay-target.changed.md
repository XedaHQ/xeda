- `yosys_fpga` no longer gives ABC9 a delay target from the clock period, which cost about 13
  percent fmax on ECP5. The new `abc9_delay_target` setting sets a target explicitly.
