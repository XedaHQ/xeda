{#- The implementation steps of the non-project scripts, from `place_design` to the bitstream.

    The script that includes this file has opened a placeable design (synthesized, or linked from
    a netlist), has included `util.tcl`, and has set `reports_dir`. It reads the implementation
    options of `settings.impl`. The checkpoints and the netlists are written only by a flow whose
    settings switch them on (`write_checkpoint`, `write_netlist`, `write_timing_netlist`). For
    those, the script has also set `settings.outputs_dir` and `checkpoints_dir`, and has made
    them and the directory `post_place` of `reports_dir`. -#}
{%- set write_checkpoint = settings.write_checkpoint is defined and settings.write_checkpoint -%}
{%- set write_netlist = settings.write_netlist is defined and settings.write_netlist -%}
{%- set write_timing_netlist = settings.write_timing_netlist is defined and settings.write_timing_netlist -%}
puts "\n================================( Place Design )================================="
place_design {{settings.impl.steps.place|flatten_options}}
showWarningsAndErrors


{% if settings.impl.steps.power_opt %}
puts "\n===============================( Post-placement Power Optimization )================================"
power_opt_design
report_power_opt -file ${reports_dir}/post_place/post_place_power_optimization.rpt
showWarningsAndErrors
{%- endif %}

{% if settings.impl.steps.place_opt is not none %}

puts "\n==============================( Post-place optimization )================================"
opt_design {{settings.impl.steps.place_opt|flatten_options}}

{% if settings.impl.steps.place_opt2 is not none %}
puts "\n==============================( Post-place optimization 2)================================"
opt_design {{settings.impl.steps.place_opt2|flatten_options}}
{%- endif %}

{%- endif %}


{% if settings.impl.steps.phys_opt is not none %}
puts "\n========================( Post-place Physical Optimization )=========================="
phys_opt_design {{settings.impl.steps.phys_opt|flatten_options}}

{% if settings.impl.steps.phys_opt is not none %}
puts "\n========================( Post-place Physical Optimization 2 )=========================="
phys_opt_design {{settings.impl.steps.phys_opt|flatten_options}}
{%- endif %}
{%- endif %}

{% if write_checkpoint %}
write_checkpoint -force ${checkpoints_dir}/post_place
report_timing_summary -file ${reports_dir}/post_place/timing_summary.rpt
report_utilization -hierarchical -force -file ${reports_dir}/post_place/hierarchical_utilization.rpt
{%- endif %}

puts "\n================================( Route Design )================================="
route_design {{settings.impl.steps.route|flatten_options}}
showWarningsAndErrors

{% if settings.impl.steps.post_route_phys_opt is not none %}
puts "\n=========================( Post-Route Physical Optimization )=========================="
phys_opt_design {{settings.impl.steps.post_route_phys_opt|flatten_options}}
showWarningsAndErrors
{%- endif %}

{% if write_checkpoint %}
puts "\n=============================( Writing Checkpoint )=============================="
write_checkpoint -force ${checkpoints_dir}/post_route
{%- endif %}

puts "\n==============================( Writing Reports )================================"
set rep_dir [file join ${reports_dir} route_design]
file mkdir ${rep_dir}

set timing_summary_file [file join ${rep_dir} timing_summary.rpt]

set num_max_paths {{settings.num_critical_paths}}
report_timing_summary -check_timing_verbose -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file ${timing_summary_file}
report_timing         -no_header -input_pins  -unique_pins -sort_by group -max_paths ${num_max_paths} -path_type full -delay_type min_max -file [file join ${rep_dir} timing.rpt]
reportCriticalPaths                [file join ${rep_dir} critpath_report.csv] ${num_max_paths}
reportCriticalPathsByDelay         [file join ${rep_dir} critpath_by_delay_report.csv] ${num_max_paths}

report_utilization                 -file [file join ${rep_dir} utilization.rpt]
report_utilization                 -file [file join ${rep_dir} utilization.xml] -format xml
report_utilization -hierarchical   -file [file join ${rep_dir} hierarchical_utilization.xml] -format xml

{% if settings.extra_reports -%}
report_clock_utilization           -file [file join ${rep_dir} clock_utilization.rpt]
report_power                       -file [file join ${rep_dir} power.rpt]
report_drc                         -file [file join ${rep_dir} drc.rpt]
report_methodology                 -file [file join ${rep_dir} methodology.rpt]
{%- endif %}

{% if settings.qor_suggestions -%}
report_qor_suggestions             -file [file join ${rep_dir} qor_suggestions.rpt]
{%- endif %}

set timing_slack [get_property SLACK [get_timing_paths]]

if {[string is double -strict $timing_slack]} {
    puts "Final timing slack: $timing_slack ns"

    if {[string is double -strict $timing_slack] && ($timing_slack < 0)} {
        puts "ERROR: Failed to meet timing by $timing_slack, see ${timing_summary_file} for details"
        {% if settings.fail_timing %}
        exit 1
        {%- endif %}
    }
}

{% if write_netlist -%}
puts "\n==========================( Writing Netlist and Constraints )============================="
write_verilog -mode funcsim -force ${settings.outputs_dir}/impl_funcsim.v
##    write_vhdl    -mode funcsim -include_xilinx_libs -write_all_overrides -force -file ${settings.outputs_dir}/impl_funcsim_xlib.vhd
write_xdc -no_fixed_only -force ${settings.outputs_dir}/impl.xdc
{% endif -%}

{% if write_timing_netlist -%}
puts "\n==========================( Writing Timing Netlist and SDF )============================="
write_sdf -mode timesim -process_corner slow -force -file ${settings.outputs_dir}/impl_timesim.sdf
# should match sdf
write_verilog -mode timesim -sdf_anno false -force -file ${settings.outputs_dir}/impl_timesim.v
{% endif -%}

{% if settings.bitstream -%}
puts "\n==============================( Writing Bitstream )==============================="
write_bitstream -force {{settings.bitstream|tcl_word}}
{% endif -%}

showWarningsAndErrors
puts "\n===========================( *DISABLE ECHO* )==========================="
