# These settings are set by XEDA
set design_name           {{design.name}}
set vhdl_std              {{design.vhdl_std}}
set debug                 {{debug}}
set nthreads              {{nthreads}}
set top                   {{design.top}}
set fpga_part             "{{flow.fpga_part}}"
set clock_port            {{design.clock_port}}
set clock_period          {{flow.clock_period}}
set strategy              {{flow.strategy}}


set optimize_power        {{flow.optimize_power}}
set generics_options      "{{flow.generics_options}}"
set fail_critical_warning {{flow.fail_critical_warning}}
set fail_timing           {{flow.fail_timing}}

set reports_dir           reports
set results_dir           output
set checkpoints_dir       checkpoints

set vhdl_funcsim          ${results_dir}/${top}_impl_funcsim.vhd
set verilog_funcsim       ${results_dir}/${top}_impl_funcsim.v
set verilog_timesim       ${results_dir}/${top}_impl_timesim.v
set sdf_file              "[file rootname ${verilog_timesim}].sdf"

{% include 'util.tcl' %}

{% include 'run_synth.tcl' %}
