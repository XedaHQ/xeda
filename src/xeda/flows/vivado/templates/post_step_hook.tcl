{% include 'util.tcl' %}
showWarningsAndErrors

{%- for file in user_hooks %}
source {{file|tcl_word}}
{%- endfor %}

{#- `ACTIVE_STEP` is Vivado's own: its runs set it before each step and unset it after. #}
set xeda_reports_dir [file join {{run_dir|tcl_word}} {{settings.reports_dir}} {{step}}]

puts "\n=======================( Writing reports after {{step}} )========================"
puts "Writing reports to ${xeda_reports_dir}"
file mkdir ${xeda_reports_dir}

{% if step == "route_design" -%}
report_timing_summary -check_timing_verbose -warn_on_violation -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file [file join ${xeda_reports_dir} timing_summary.rpt]
report_timing -warn_on_violation -no_header -input_pins -unique_pins -max_paths 128 -nworst 4 -path_type full -delay_type min_max -file [file join ${xeda_reports_dir} timing.rpt]
{%- else -%}
report_timing_summary -no_header -delay_type max -file [file join ${xeda_reports_dir} timing_summary.rpt]
report_timing -no_header -delay_type max -file [file join ${xeda_reports_dir} timing.rpt]
{%- endif %}

report_utilization -force -file [file join ${xeda_reports_dir} utilization.xml] -format xml
report_utilization -force -file [file join ${xeda_reports_dir} hierarchical_utilization.xml] -format xml -hierarchical
reportCriticalPaths [file join ${xeda_reports_dir} critical_paths.csv] {{settings.num_critical_paths}}
reportCriticalPathsByDelay [file join ${xeda_reports_dir} critical_paths_by_delay.csv] {{settings.num_critical_paths}}

showWarningsAndErrors

{%- if step == "synth_design" and outputs.checkpoint_synth is defined %}

file mkdir [file dirname {{outputs.checkpoint_synth|tcl_word}}]
write_checkpoint -force {{outputs.checkpoint_synth|tcl_word}}
{%- endif %}

{%- if step == "route_design" %}

report_drc -file [file join ${xeda_reports_dir} drc.rpt]
report_utilization -force -file [file join ${xeda_reports_dir} utilization.rpt]
report_utilization -force -file [file join ${xeda_reports_dir} hierarchical_utilization.rpt] -hierarchical_percentages -hierarchical
report_route_status -file [file join ${xeda_reports_dir} route_status.rpt]
report_datasheet -file [file join ${xeda_reports_dir} datasheet.rpt]
report_design_analysis -complexity -congestion -timing -show_all -max_paths 4 -file [file join ${xeda_reports_dir} design_analysis.rpt]
report_design_analysis -complexity -logic_level_distribution -qor_summary -json [file join ${xeda_reports_dir} design_analysis.json]

set xeda_timing_slack [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
puts "=======================( Final timing slack: $xeda_timing_slack ns )======================="

{%- if settings.qor_suggestions %}
report_qor_suggestions -quiet -max_strategies 5 -file [file join ${xeda_reports_dir} qor_suggestions.rpt]
write_qor_suggestions -quiet -strategy_dir [file join ${xeda_reports_dir} strategy_suggestions] -force [file join ${xeda_reports_dir} qor_suggestions.rqs]
{%- endif %}

{%- if settings.report_power %}
report_power -quiet -file [file join ${xeda_reports_dir} power.xml] -format xml
{%- endif %}

{%- if outputs.checkpoint_route is defined %}

file mkdir [file dirname {{outputs.checkpoint_route|tcl_word}}]
write_checkpoint -force {{outputs.checkpoint_route|tcl_word}}
{%- endif %}

{%- if outputs.netlist is defined %}

puts "\n==========================( Writing netlists, SDF and constraints )=========================="
file mkdir [file dirname {{outputs.netlist|tcl_word}}]
write_verilog -mode funcsim -force -file {{outputs.netlist|tcl_word}}
write_verilog -mode timesim -sdf_anno false -force -file {{outputs.netlist_timing|tcl_word}}
write_sdf -mode timesim -process_corner fast -force -file {{outputs.sdf_min|tcl_word}}
write_sdf -mode timesim -process_corner slow -force -file {{outputs.sdf_max|tcl_word}}
write_xdc -no_fixed_only -force {{outputs.xdc_exported|tcl_word}}
{%- endif %}

if { $xeda_timing_slack != "" && $xeda_timing_slack < 0.000 } {
  puts "\n=========( ERROR: Failed to meet timing by $xeda_timing_slack )=========="
  {%- if settings.fail_timing %}
  {#- fails the step, and with it the run: nothing after routing runs #}
  error "Failed to meet timing by $xeda_timing_slack, see [file join ${xeda_reports_dir} timing_summary.rpt] for details"
  {%- endif %}
}
{%- endif %}
