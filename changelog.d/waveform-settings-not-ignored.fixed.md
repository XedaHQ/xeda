- `nvc` now writes the shared `vcd` setting, and honors `wave_include_glob` and
  `wave_exclude_glob`. `vcs` and `yosys_sim` refuse `vcd` when planned instead of ignoring it, and
  a simulator refuses two waveform formats at once or an option that refines a missing waveform.
