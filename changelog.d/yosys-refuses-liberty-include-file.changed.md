- `yosys` refuses a Liberty file that includes another with `include_file`, before it starts. The
  Liberty reader of yosys does not read `include_file`; the OpenROAD flows do.
