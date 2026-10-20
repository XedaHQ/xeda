- `--remote` accepts the same names in `set_synth_properties` and the other tables of tool or
  netlist names as a local run, including `A` from one origin with `A.B` from another. It carries
  a name inside up to two other names, and refuses a deeper chain before it connects.
