# These settings are set by SASSYN
set design_name           {{design.name}}
set vhdl_std              {{design.vhdl_std}}
set vhdl_files            "{{design.vhdl_sources|join(' ')}}"
set verilog_files         "{{design.verilog_sources|join(' ')}}"
set sim_vhdl_files        "{{design.vhdl_tb_sources|join(' ')}}"
set sim_verilog_files     "{{design.verilog_tb_sources|join(' ')}}"
set clock_port            {{design.clock_port}}
set top                   {{design.top}}
set tb_top                {{design.tb_top}}
set strategy              {{flow.strategy}}
set clock_period          {{flow.clock_period}}
set fpga_part             "{{flow.fpga_part}}"

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


# sim
set timing_sim            false
set funcsim_use_vhdl      true
set gen_saif              false
set gen_vcd               false
set uut_scope             /LWC_TB/uut
set max_run               "100us"
set initialize_zeros      false
