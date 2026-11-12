- `yosys_fpga` refuses a device that yosys cannot synthesize for, or a setting that the synthesis
  pass of the device never takes (such as `widemux` outside Xilinx), when it plans the run (also
  with `--dry-run`), not when the run starts.
