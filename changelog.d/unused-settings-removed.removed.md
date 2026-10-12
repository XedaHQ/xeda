- Settings that nothing used are removed. A design that sets one fails with a "was removed" error,
  and the fix is to delete the setting, which did nothing. The simulators have `timeout` where
  `timeout_seconds` was, and `dc` takes `target_libraries`. Removed: `timeout_seconds` and
  `no_console` of every flow, `platform`, `extra_link_libraries`, `mw_ref_lib`, `mw_tf`,
  `alib_dir` and `additional_search_path` of `dc`, `synthesis_effort` of `quartus`, `work_dir` of
  `vcs`, `initialize_zeros` and `read_oneshot` of the Vivado simulation flows, `suppress_msgs` of
  those flows and of `vivado_power`, `netlist_verilog_extmem` of `yosys` and `yosys_fpga`,
  `retime` of `yosys` and `nosynth` of `yosys_fpga`.
