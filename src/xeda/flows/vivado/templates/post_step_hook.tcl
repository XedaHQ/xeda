{% include 'util.tcl' %}
showWarningsAndErrors

{%- for file in user_hooks %}
source {{file}}
{%- endfor %}

set RUN_DIR {{run_dir}}

if { ![info exists ACTIVE_STEP] } {
  set ACTIVE_STEP "synth_design"
  set reports_dir [file join ${RUN_DIR} {{settings.reports_dir}} $ACTIVE_STEP]
}

set reports_dir [file join ${RUN_DIR} {{settings.reports_dir}} $ACTIVE_STEP]
set outputs_dir [file join ${RUN_DIR} {{settings.outputs_dir}} $ACTIVE_STEP]

puts "\n=======================( Writing reports after $ACTIVE_STEP )========================"
puts "Writing reports to ${reports_dir}"
file mkdir ${reports_dir}

if {$ACTIVE_STEP == "route_design"} {
  report_timing_summary -check_timing_verbose -warn_on_violation -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file [file join ${reports_dir} timing_summary.rpt]
  report_timing -warn_on_violation -no_header -input_pins -unique_pins -max_paths 128 -nworst 4 -path_type full -delay_type min_max -file [file join ${reports_dir} timing.rpt]
} else {
  report_timing_summary -no_header -delay_type max -file [file join ${reports_dir} timing_summary.rpt]
  report_timing -no_header -delay_type max -file [file join ${reports_dir} timing.rpt]
}

report_utilization -force -file [file join ${reports_dir} utilization.xml] -format xml
report_utilization -force -file [file join ${reports_dir} hierarchical_utilization.xml] -format xml -hierarchical
reportCriticalPaths [file join ${reports_dir} critical_paths.csv]

showWarningsAndErrors

if {$ACTIVE_STEP == "route_design"} {
  report_drc  -file [file join ${reports_dir} drc.rpt]
  report_utilization -force -file [file join ${reports_dir} utilization.rpt]
  report_utilization -force -file [file join ${reports_dir} hierarchical_utilization.rpt] -hierarchical_percentages -hierarchical
  report_route_status -file [file join ${reports_dir} route_status.rpt]
  report_datasheet -file [file join ${reports_dir} datasheet.rpt]
  report_design_analysis -complexity -congestion -timing -show_all -max_paths 4 -file [file join ${reports_dir} design_analysis.rpt]
  report_design_analysis -complexity -logic_level_distribution -qor_summary -json [file join ${reports_dir} design_analysis.json]

  set timing_slack [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
  puts "=======================( Final timing slack: $timing_slack ns )======================="

  {%- if settings.qor_suggestions %}
  report_qor_suggestions -quiet -max_strategies 5 -file [file join ${reports_dir} qor_suggestions.rpt]
  write_qor_suggestions -quiet -strategy_dir  ./strategy_suggestions -force ./qor_suggestions.rqs
  {%- endif %}

  file mkdir ${outputs_dir}
  {% if settings.write_netlist -%}
  puts "\n==========================( Writing netlists and SDF to ${outputs_dir}  )=========================="
  write_verilog -mode timesim -sdf_anno false -force -file ${outputs_dir}/timesim.v
  write_sdf -mode timesim -process_corner slow -force -file ${outputs_dir}/timesim.min.sdf
  write_sdf -mode timesim -process_corner fast -force -file ${outputs_dir}/timesim.max.sdf
  write_vhdl -mode funcsim -include_xilinx_libs -write_all_overrides -force -file ${outputs_dir}/funcsim.vhdl
  write_xdc -no_fixed_only -force ${outputs_dir}/impl.xdc
  {% endif -%}

  {% if settings.bitstream is not none -%}
  puts "\n=============================( Writing bitstream to { {{-settings.bitstream-}} } )=============================="
  set BITSTREAM_OUT_DIR [file dirname { {{-settings.bitstream-}} }]
  file mkdir $BITSTREAM_OUT_DIR
  write_bitstream -force { {{-settings.bitstream-}} }
  {% endif -%}

  if { $timing_slack < 0.000 } {
    puts "\n=========( ERROR: Failed to meet timing by $timing_slack )=========="
    error "Failed to meet timing by $timing_slack, see [file join ${reports_dir} post_route timing_summary.rpt] for details"
    {%- if settings.fail_timing %}
    exit 1
    {%- endif %}
  }

}
