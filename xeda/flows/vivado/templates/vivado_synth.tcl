# These settings are set by XEDA
set design_name           {{design.name}}
set vhdl_std              {{design.language.vhdl.standard}}
set debug                 {{debug}}
set nthreads              {{nthreads}}
set top                   {{design.rtl.top}}


set optimize_power        {{flow.optimize_power}}
set fail_critical_warning {{flow.fail_critical_warning}}
set fail_timing           {{flow.fail_timing}}

set reports_dir           {{reports_dir}}
set results_dir           {{results_dir}}
set checkpoints_dir       {{checkpoints_dir}}

set vhdl_funcsim          ${results_dir}/${top}_impl_funcsim.vhd
set verilog_funcsim       ${results_dir}/${top}_impl_funcsim.v
set verilog_timesim       ${results_dir}/${top}_impl_timesim.v
set sdf_file              "[file rootname ${verilog_timesim}].sdf"

{% include 'util.tcl' %}

# TODO move all strategy-based decisions to Python side
puts "Using \"{{flow.strategy}}\" synthesis strategy"

set_param general.maxThreads ${nthreads}

file mkdir ${results_dir}
file mkdir ${reports_dir}
file mkdir [file join ${reports_dir} post_synth]
file mkdir [file join ${reports_dir} post_place]
file mkdir [file join ${reports_dir} post_route]
file mkdir ${checkpoints_dir}

# suppress some warning messages
# warning partial connection
set_msg_config -id "\[Synth 8-350\]" -suppress
# info do synthesis
set_msg_config -id "\[Synth 8-256\]" -suppress
set_msg_config -id "\[Synth 8-638\]" -suppress
# BRAM mapped to LUT due to optimization
set_msg_config -id "\[Synth 8-3969\]" -suppress
# BRAM with no output register
set_msg_config -id "\[Synth 8-4480\]" -suppress
# DSP without input pipelining
set_msg_config -id "\[Drc 23-20\]" -suppress
# Update IP version
set_msg_config -id "\[Netlist 29-345\]" -suppress   

set parts [get_parts]

puts "\n================================( Read Design Files and Constraints )================================"

if {[lsearch -exact $parts {{flow.fpga_part}}] < 0} {
    puts "ERROR: device {{flow.fpga_part}} is not supported!"
    puts "Supported devices: $parts"
    quit
}

puts "Targeting device: {{flow.fpga_part}}"

# DO NOT use per file vhdl version as not supported universally (even though our data structures support it)
set vhdl_std_opt [expr {$vhdl_std == "08" ?  "-vhdl2008": ""}];

{% for src in design.rtl.sources %}
{%- if src.type == 'verilog' %}
{%- if src.variant == 'systemverilog' %}
puts "Reading SystemVerilog file {{src.file}}"
if { [catch {eval read_verilog -sv {{src.file}} } myError]} {
    errorExit $myError
}
{% else %}
puts "Reading Verilog file {{src.file}}"
if { [catch {eval read_verilog {{src.file}} } myError]} {
    errorExit $myError
}
{%- endif %}
{%- endif %}
{% if src.type == 'vhdl' %}
puts "Reading VHDL file {{src.file}} ${vhdl_std_opt}"
if { [catch {eval read_vhdl ${vhdl_std_opt} {{src.file}} } myError]} {
    errorExit $myError
}
{%- endif %}
{%- endfor -%}

# TODO: Skip saving some artifects in case timing not met or synthesis failed for any reason

{% for xdc_file in xdc_files %}
read_xdc {{xdc_file}}
{% endfor %}

puts "\n===========================( RTL Synthesize and Map )==========================="
{% if flow.strategy == "Debug" %}
eval synth_design -rtl -rtl_skip_ip -top ${top} {{options.synth}} {{generics_options}}
set_property KEEP_HIERARCHY true [get_cells -hier * ]
set_property DONT_TOUCH true [get_cells -hier * ]
{% endif %}

eval synth_design -part {{flow.fpga_part}} -top ${top} {{options.synth}} {{generics_options}}
showWarningsAndErrors

set_property KEEP_HIERARCHY true [get_cells -hier * ]
set_property DONT_TOUCH true [get_cells -hier * ]

#write_verilog -force ${results_dir}/${top}_synth_rtl.v
# report_utilization -file ${reports_dir}/pre_opt_utilization.rpt

{% if flow.strategy != "Debug" and flow.strategy != "Runtime" %}
puts "\n==============================( Optimize Design )================================"
eval opt_design {{options.opt}}
{% endif %}


puts "==== Synthesis and Mapping Steps Complemeted ====\n"
write_checkpoint -force ${checkpoints_dir}/post_synth
report_timing_summary -file ${reports_dir}/post_synth/timing_summary.rpt
report_utilization -file ${reports_dir}/post_synth/utilization.rpt
report_utilization -file ${reports_dir}/post_synth/utilization.xml -format xml
reportCriticalPaths ${reports_dir}/post_synth/critpath_report.csv
report_methodology  -file ${reports_dir}/post_synth/methodology.rpt

puts "\n================================( Place Design )================================="
eval place_design {{options.place}}
showWarningsAndErrors

{% if flow.strategy != "Debug" and flow.strategy != "Runtime" %}
puts "\n========================( Pysically Optimize Design 1 )=========================="
eval phys_opt_design {{options.phys_opt}}
{% endif %}

if {$optimize_power} {
    puts "\n===============================( Optimize Power )================================"
    eval power_opt_design
}

write_checkpoint -force ${checkpoints_dir}/post_place
report_timing_summary -max_paths 10 -file ${reports_dir}/post_place/timing_summary.rpt

puts "==== Placement Steps Complemeted ====\n"

puts "\n================================( Route Design )================================="
eval route_design {{options.route}}
showWarningsAndErrors

{% if flow.strategy != "Debug" and flow.strategy != "Runtime" %}
puts "\n=========================( Pysically Optimize Design 2)=========================="
eval phys_opt_design {{options.phys_opt}}
showWarningsAndErrors
{% endif %}

puts "\n=============================( Writing Checkpoint )=============================="
write_checkpoint -force ${checkpoints_dir}/post_route

puts "\n==============================( Writing Reports )================================"
report_timing_summary -max_paths 10                             -file ${reports_dir}/post_route/timing_summary.rpt

{% if flow.strategy != "Debug" %}
report_timing  -sort_by group -max_paths 100 -path_type summary -file ${reports_dir}/post_route/timing.rpt
reportCriticalPaths ${reports_dir}/post_route/critpath_report.csv
report_clock_utilization                                        -force -file ${reports_dir}/post_route/clock_utilization.rpt
report_utilization                                              -force -file ${reports_dir}/post_route/utilization.rpt
report_utilization                                              -force -file ${reports_dir}/post_route/utilization.xml -format xml
report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.rpt
report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.xml -format xml
report_power                                                    -file ${reports_dir}/post_route/power.rpt
report_drc                                                      -file ${reports_dir}/post_route/drc.rpt
report_ram_utilization                                          -file ${reports_dir}/post_route/ram_utilization.rpt -append
report_methodology                                              -file ${reports_dir}/post_route/methodology.rpt
{% endif %}

puts "==== Routing Steps Complemeted ====\n"

puts "\n==========================( Writing Netlist and SDF )============================="
write_sdf -mode timesim -process_corner slow -force -file ${sdf_file}
write_verilog -include_xilinx_libs -force ${results_dir}/${top}_impl_netlist.v
# write_verilog -mode timesim -sdf_anno true -sdf_file ${sdf_file} -force ${verilog_timesim}
write_verilog -mode timesim -nolib -sdf_anno true -force -file ${verilog_timesim}
write_verilog -mode funcsim -force ${verilog_funcsim}
write_vhdl    -mode funcsim -force ${vhdl_funcsim}
write_xdc -no_fixed_only -force ${results_dir}/${top}_impl.xdc

if {false} {
    puts "\n==============================( Writing Bitstream )==============================="
    write_bitstream -force ${results_dir}/${top}.bit
}
showWarningsAndErrors

puts "\n\n---*****===( Vivado Flow Completed )===*****---\n"


set timing_slack [get_property SLACK [get_timing_paths]]
puts "Final timing slack: $timing_slack ns"

if {$timing_slack < 0} {
    puts "\n===========================( *ENABLE ECHO* )==========================="
    puts "ERROR: Failed to meet timing by $timing_slack, see [file join ${reports_dir} post_route timing_summary.rpt] for details"
    if {$fail_timing} {
        exit 1
    }
    puts "\n===========================( *DISABLE ECHO* )==========================="
}
