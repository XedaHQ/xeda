- `vivado_sim` and `vivado_postsynth_sim` now refuse `saif` (and a timing simulation) without
  `tb.uut` when planning. `nvc` passes each `lib_paths` entry as `-L <path>`. `verilator` takes
  `optimize` as `true`, `false` or a level of 0, 1, 2 or 3, refuses `-O2`, and `0` now gives `-O0`.
