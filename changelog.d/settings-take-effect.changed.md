- Settings that had no effect now work. `vivado_project` sets `set_synth_properties` and
  `set_impl_properties` and shows the strategies for `show_available_strategies`. `extra_reports`
  writes its reports in `vivado_synth` and `vivado_project`. `report_power` writes `power.xml` in
  `vivado_alt_synth` and `vivado_impl`, as it does in `vivado_synth` and `vivado_project`. `nvc`
  passes `werror` as `-Werror`, and `yosys_fpga` passes `splitnets_driver` and `splitnets_ports`.
