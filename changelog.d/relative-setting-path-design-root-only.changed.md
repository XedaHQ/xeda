- A relative path in a flow setting is now read from the design root in every flow. Before, some
  flows (for example `nvc.cover_spec`, `tcl_files` of Vivado and `extraction.rules_file` of
  OpenROAD) read it from the run directory, and the trace also looked under the start directory.
  If you relied on the start directory, write `$PWD/...`.
