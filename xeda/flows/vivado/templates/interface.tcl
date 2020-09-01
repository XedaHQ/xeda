# These settings are set by XEDA
set design_name           {{design.name}}
set vhdl_std              {{design.vhdl_std}}
set fpga_part             "{{flow.fpga_part}}"
set clock_port            {{design.clock_port}}
set clock_period          {{flow.clock_period}}
set top                   {{design.top}}
set tb_top                {{design.tb_top}}
set strategy              {{flow.strategy}}

set debug                 {{debug}}
set nthreads              {{nthreads}}
set run_synth_flow        {{run_synth_flow}}
set run_postsynth_sim     {{run_postsynth_sim}}
set optimize_power        {{flow.optimize_power}}
set generics_options      "{{flow.generics_options}}"
set tb_generics_options   "{{flow.tb_generics_options}}"
set fail_critical_warning {{flow.fail_critical_warning}}
set fail_timing           {{flow.fail_timing}}

set vivado_dir            .
set reports_dir           ${vivado_dir}/reports
set results_dir           ${vivado_dir}/output
set checkpoints_dir       ${vivado_dir}/checkpoints


set vhdl_funcsim          ${results_dir}/${top}_impl_funcsim.vhd
set verilog_funcsim       ${results_dir}/${top}_impl_funcsim.v
set verilog_timesim       ${results_dir}/${top}_impl_timesim.v
set sdf_file              "[file rootname ${verilog_timesim}].sdf"

# sim

