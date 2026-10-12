- Settings that had no effect now work. `vivado_project` sets `set_synth_properties` and
  `set_impl_properties` and shows the strategies for `show_available_strategies`. `extra_reports`
  writes its reports in `vivado_synth` and `vivado_project`, and `report_power` in `vivado_impl`
  and `vivado_alt_synth`. `nvc` passes `werror` as `-Werror`, `yosys_fpga` passes
  `splitnets_driver` and `splitnets_ports`, and `openroad` sources `floorplan_tcl` and passes
  `rtlmp_config_file`.
