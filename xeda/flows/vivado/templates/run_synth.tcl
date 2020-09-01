# TODO move to python
puts "Using \"$strategy\" synthesis strategy"

if {$strategy == "Debug"} {
	set synth_options "-flatten_hierarchy none -keep_equivalent_registers -no_lc -fsm_extraction off -assert -directive RuntimeOptimized"
	set opt_options "-directive RuntimeOptimized"
	set place_options "-directive RuntimeOptimized"
	set route_options "-directive RuntimeOptimized"
	set phys_opt_options "-directive RuntimeOptimized"
} else {

if {$strategy == "Runtime"} {
	set synth_options "-assert -directive RuntimeOptimized"
	set opt_options "-directive RuntimeOptimized"
	set place_options "-directive RuntimeOptimized"
	set route_options "-directive RuntimeOptimized"
	set phys_opt_options "-directive RuntimeOptimized"
} else {

if {$strategy == "Default"} {
	set synth_options "-assert -flatten_hierarchy rebuilt -retiming -directive Default"
	set opt_options "-directive ExploreWithRemap"
	set place_options "-directive Default"
	set route_options "-directive Default"
	set phys_opt_options "-directive Default"
} else {

if {$strategy == "Timing"} {
  puts "Timing optimized goal!"
  set synth_options "-assert -flatten_hierarchy full -retiming -directive PerformanceOptimized"
  set opt_options "-directive ExploreWithRemap"
  # or ExtraTimingOpt, ExtraPostPlacementOpt, Explore
  set place_options "-directive ExtraTimingOpt"

  # very slow: AggressiveExplore
  set route_options "-directive AggressiveExplore"
  # if no directive: -placement_opt
  set phys_opt_options "-directive Explore"
} else {

if {$strategy == "Area"} {
  set synth_options "-assert -flatten_hierarchy full -directive AreaOptimized_high"
  # if no directive: -resynth_seq_area 
  set opt_options "-directive ExploreArea"
  set place_options "-directive Explore"
  set route_options "-directive Explore"
  # if no directive: -placement_opt
  set phys_opt_options "-directive Explore"
}  else {

puts "Unknown strategy=${strategy}"
exit 1
}}}}}

set_param general.maxThreads ${nthreads}

set stamp_filename "${vivado_dir}/synth.stamp"
#Trying to delete a non-existent file is not considered an error.
file delete -force ${stamp_filename}

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

if {[lsearch -exact $parts ${fpga_part}] < 0} {
    puts "ERROR: device ${fpga_part} is not supported!"
    puts "Supported devices: $parts"
    quit
}

puts "Targeting device: ${fpga_part}"

# DO NOT use per file vhdl version as not supported universally (even though our data structures support it)
set vhdl_std_opt [expr {$vhdl_std == "08" ?  "-vhdl2008": ""}];

{% for src in design.sources if not src.sim_only %}
    {% if src.type == 'verilog' %}

        {% if src.variant == 'systemverilog' %}
        puts "Reading SystemVerilog file {{src.file}}"
        if { [catch {eval read_verilog -sv {{src.file}} } myError]} {
            errorExit $myError
        }
        {% else %}
        puts "Reading Verilog file {{src.file}}"
        if { [catch {eval read_verilog {{src.file}} } myError]} {
            errorExit $myError
        }
        {% endif %}

    {% endif %}
    {% if src.type == 'vhdl' %}
    puts "Reading VHDL file {{src.file}} ${vhdl_std_opt}"
    if { [catch {eval read_vhdl ${vhdl_std_opt} {{src.file}} } myError]} {
        errorExit $myError
    }
    {% endif %}
{% endfor %}


# TODO: Skip saving some artifects in case timing not met or synthesis failed for any reason

{% for xdc_file in xdc_files %}
read_xdc {{xdc_file}}
{% endfor %}

puts "\n===========================( RTL Synthesize and Map )==========================="

set synth_options "${synth_options} -max_bram 0 -max_dsp 0"

if {$strategy == "Debug"} {
    eval synth_design -rtl -rtl_skip_ip -top ${top} ${synth_options} ${generics_options}
    set_property KEEP_HIERARCHY true [get_cells -hier * ]
    set_property DONT_TOUCH true [get_cells -hier * ]
}

eval synth_design -part ${fpga_part} -top ${top} ${synth_options} ${generics_options}
showWarningsAndErrors

set_property KEEP_HIERARCHY true [get_cells -hier * ]
set_property DONT_TOUCH true [get_cells -hier * ]

#write_verilog -force ${results_dir}/${top}_synth_rtl.v

# report_utilization -file ${reports_dir}/pre_opt_utilization.rpt
if {$strategy != "Debug" && ($strategy != "Runtime")} {
    puts "\n==============================( Optimize Design )================================"
    eval opt_design ${opt_options}
}


puts "==== Synthesis and Mapping Steps Complemeted ====\n"
write_checkpoint -force ${checkpoints_dir}/post_synth
report_timing_summary -file ${reports_dir}/post_synth/timing_summary.rpt
report_utilization -file ${reports_dir}/post_synth/utilization.rpt
report_utilization -file ${reports_dir}/post_synth/utilization.xml -format xml
reportCriticalPaths ${reports_dir}/post_synth/critpath_report.csv
report_methodology  -file ${reports_dir}/post_synth/methodology.rpt
# report_power -file ${reports_dir}/post_synth/power.rpt

#       ---------------------------------------------------------------------------------
puts "\n================================( Place Design )================================="
eval place_design ${place_options}
showWarningsAndErrors

if {$strategy != "Debug" && ($strategy != "Runtime")} {
    puts "\n========================( Pysically Optimize Design 1 )=========================="
    eval phys_opt_design ${phys_opt_options}
}

if {$optimize_power} {
    puts "\n===============================( Optimize Power )================================"
    eval power_opt_design
}

write_checkpoint -force ${checkpoints_dir}/post_place
report_timing_summary -max_paths 10 -file ${reports_dir}/post_place/timing_summary.rpt

puts "==== Placement Steps Complemeted ====\n"

puts "\n================================( Route Design )================================="
eval route_design -ultrathreads ${route_options}
showWarningsAndErrors

if {$strategy != "Debug" && ($strategy != "Runtime")} {
    puts "\n=========================( Pysically Optimize Design 2)=========================="
    eval phys_opt_design ${phys_opt_options}
    showWarningsAndErrors
}

puts "\n=============================( Writing Checkpoint )=============================="
write_checkpoint -force ${checkpoints_dir}/post_route

puts "\n==============================( Writing Reports )================================"
report_timing_summary -max_paths 10                             -file ${reports_dir}/post_route/timing_summary.rpt

if {$strategy != "Debug"} {
    report_timing  -sort_by group -max_paths 100 -path_type summary -file ${reports_dir}/post_route/timing.rpt
    reportCriticalPaths ${reports_dir}/post_route/critpath_report.csv
    report_clock_utilization                                        -force -file ${reports_dir}/post_route/clock_utilization.rpt
    report_utilization                                              -force -file ${reports_dir}/post_route/utilization.rpt
    report_utilization                                              -force -file ${reports_dir}/post_route/utilization.xml -format xml
    report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.rpt
    report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.xml -format xml
    report_power                                                    -file ${reports_dir}/post_route/power.rpt
    report_drc                                                      -file ${reports_dir}/post_route/drc.rpt
    report_ram_utilization                                          -file ${reports_dir}/post_route/ram_utilization.rpt -append -detail
    report_methodology                                              -file ${reports_dir}/post_route/methodology.rpt
}
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

set stamp [open ${stamp_filename} w]
puts $stamp "strategy: ${strategy}"
puts $stamp "SUCCESS"
close $stamp

