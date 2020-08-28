set child_scripts_dir [file dirname [info script]]

proc reportCriticalPaths { fileName } {
    # Open the specified output file in write mode
    set FH [open $fileName w]
    # Write the current date and CSV format to a file header
    puts $FH "#\n# File created on [clock format [clock seconds]]\n#\n"
    puts $FH "Startpoint,Endpoint,DelayType,Slack,#Levels,#LUTs"
    # Iterate through both Min and Max delay types
    foreach delayType {max min} {
        # Collect details from the 50 worst timing paths for the current analysis
        # (max = setup/recovery, min = hold/removal)
        # The $path variable contains a Timing Path object.
        foreach path [get_timing_paths -delay_type $delayType -max_paths 50 -nworst 1] {
            # Get the LUT cells of the timing paths
            # set luts [get_cells -filter {REF_NAME =~ LUT*} -of_object $path] # print  ,[llength $luts] << TODO warnings
            # Get the startpoint of the Timing Path object
            set startpoint [get_property STARTPOINT_PIN $path]
            # Get the endpoint of the Timing Path object
            set endpoint [get_property ENDPOINT_PIN $path]
            # Get the slack on the Timing Path object
            set slack [get_property SLACK $path]
            # Get the number of logic levels between startpoint and endpoint
            set levels [get_property LOGIC_LEVELS $path]
            # Save the collected path details to the CSV file
            puts $FH "$startpoint,$endpoint,$delayType,$slack,$levels"
        }
    }
    # Close the output file
    close $FH
    puts "CSV file $fileName has been created.\n"
    return 0
}; # End PROC


proc showWarningsAndErrors {} {
  set num_errors     [get_msg_config -severity {ERROR} -count]
  set num_crit_warns [get_msg_config -severity {CRITICAL WARNING} -count]
  set num_warns      [get_msg_config -severity {WARNING} -count]
  if {$num_errors > 0} {
    puts "** Number of Errors:             $num_errors"
    puts "Exiting due to errors!"
    exit 1
  }

  if {$num_crit_warns > 0} {
    puts "** Number of Critical Warnings:  $num_crit_warns"


    if {EXIT_ON_CRITICAL_WARNING} {
      puts "Exiting due to $num_crit_warns critical warning(s)!"
      exit 1
    }
  }

  if {$num_warns > 0} {
    puts "** Number of Warnings:           $num_warns"
  }

  puts "\n\n"
}

proc errorExit {errorString} {
    puts "Error: $errorString"
    exit 1
}

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

set vivado_dir            .
set reports_dir           ${vivado_dir}/reports
set results_dir           ${vivado_dir}/output
set checkpoints_dir       ${vivado_dir}/checkpoints

# TODO 
set EXIT_ON_CRITICAL_WARNING true

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


# set run_synth_flow "false"
# set run_postsynth_sim "true"

set_param general.maxThreads ${nthreads}


set vhdl_funcsim     ${results_dir}/${top}_impl_funcsim.vhd
set verilog_funcsim  ${results_dir}/${top}_impl_funcsim.v
set verilog_timesim  ${results_dir}/${top}_impl_timesim.v
set sdf_file         "[file rootname ${verilog_timesim}].sdf"

set stamp_filename "${vivado_dir}/synth.stamp"
#Trying to delete a non-existent file is not considered an error.
file delete -force ${stamp_filename}


if {${run_synth_flow}} {
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

    foreach path ${verilog_files} {
        puts "Reading Verilog/SystemVerilog file ${path}"
        if { [catch {eval read_verilog -sv ${path} } myError]} {
            errorExit $myError
        }
    }

    foreach path ${vhdl_files} {
        set vhdl_std_opt [expr {$vhdl_std == "08" ?  "-vhdl2008": ""}];
        puts "Reading VHDL file ${path} ${vhdl_std_opt}"
        if { [catch {eval read_vhdl ${vhdl_std_opt} ${path} } myError]} {
            errorExit $myError
        }
    }

    # create_clock does not work from here!
    set xdc_filename "${vivado_dir}/clock.xdc"
    set xdc_file [open ${xdc_filename} w]
    puts $xdc_file "create_clock -period ${clock_period} -name clock \[get_ports ${clock_port}\]"
    close $xdc_file
    read_xdc ${xdc_filename}

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
        report_clock_utilization                                        -file ${reports_dir}/post_route/clock_utilization.rpt
        report_utilization                                              -file ${reports_dir}/post_route/utilization.rpt
        report_utilization -hierarchical                                -file ${reports_dir}/post_route/hierarchical_utilization.rpt
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
        puts "ERROR: Failed to meet timing by $timing_slack, see [file join ${reports_dir} post_route timing_summary.rpt]"
        exit 1
    }

    set stamp [open ${stamp_filename} w]
    puts $stamp "strategy: ${strategy}"
    puts $stamp "SUCCESS"
    close $stamp
}

if {$run_postsynth_sim} {



    #Depends: apt-get update %% apt-get install -qq gcc libncurses5

    set TB_TOP LWC_TB
    set timing_sim true
    set funcsim_use_vhdl true
    set gen_saif true
    set uut_scope /LWC_TB/uut
    set max_run "100us"
    set initialize_zeros false

    source ${child_scripts_dir}/run_sim.tcl


}

quit
