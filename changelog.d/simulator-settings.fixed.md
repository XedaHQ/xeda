- `vivado_sim` and `vivado_postsynth_sim` now refuse `saif` (and a timing simulation) without
  `tb.uut`, and an SDF file without `sdf.root` or `tb.uut`, when planning. `nvc` maps a named
  `lib_paths` entry with `--map=<name>:<path>` and adds an unnamed one with `-L <path>`.
  `verilator` takes `optimize` as `true`, `false` or a level of 0, 1, 2 or 3, refuses `-O2`, and
  `0` now gives `-O0`.
