- A flow refuses a setting it does not use, and the error says where the setting belongs.
  `vivado_project` refuses the settings of `vivado_sim`, `yosys_sim` refuses the synthesis
  settings of `yosys`, and `ghdl_synth` refuses the compiler and linker flags of `ghdl_sim`.
  `vivado_alt_synth` refuses `set_synth_properties`, `set_impl_properties` and
  `show_available_strategies`, which need a project. `optimization_flags` is a setting of
  `bsc_sim`, `ghdl_sim` and `vivado_sim` only.
