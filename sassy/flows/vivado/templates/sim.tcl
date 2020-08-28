set child_scripts_dir [file dirname [info script]]
# source -notrace ${child_scripts_dir}/interface.tcl


set verbose [expr {$debug == "1" ? "-verbose 2" : ""}]
set vhdl_std_opt [expr {$vhdl_std == "08" ?  "-2008": ""}];
set vhdl_std_opt [expr {$vhdl_std == "93" ?  "-93_mode": ""}];
set saif_file "${results_dir}/xsim_${top_module_name}_dump.saif"
set vcd_file "${results_dir}/xsim_${top_module_name}_dump.vcd"
set wdb_file "${results_dir}/xsim_${top_module_name}_dump"
set wdb_option "-wdb ${wdb_file}"
set xsim_verbose [expr {$debug == "1" ? "-verbose ${wdb_option}" : "" }]

set xsim_lib_name xil_defaultlib

# -sdfmax /LWC_TB/uut=${sdf_file}
# open_wave_database ${wdb_file}

set xelab_generics "-generic_top G_PERIOD=${clock_period}ns"
set snapshot_name "LWC_TB_time_impl"

set common_xelab_options "-O 1 -L simprims_ver -s ${snapshot_name} ${xelab_generics}"
set interconnect_delay_flags "-transport_int_delays -pulse_r 0 -pulse_int_r 0"
set experiment_0 false

file delete -force xsim.dir

if {${timing_sim}} {
    if {${experiment_0}} {
        puts "timing sim experiment with vhdl netlist!"
        puts "Analyzing design files"
        eval exec xvhdl ${verbose} ${vhdl_std_opt} ${vhdl_funcsim} ${sim_vhdl_files}
        puts "Elaborating design: ${TB_TOP}"
        eval exec xelab ${verbose} ${common_xelab_options} -debug all -maxdelay -sdfmax ${uut_scope}=${sdf_file} ${interconnect_delay_flags} ${TB_TOP}
    } else {
        puts "--Timing Simultation using verilog netlist--"
        puts "Analyzing design files"
        eval exec xvlog -incr -relax ${verbose} -work ${xsim_lib_name} ${verilog_timesim}
        eval exec xvhdl -incr -relax ${verbose} -work ${xsim_lib_name} ${vhdl_std_opt} ${sim_vhdl_files}
        puts "Elaborating design: ${TB_TOP}"
        # eval exec xelab ${verbose} ${common_xelab_options} -debug all -maxdelay ${interconnect_delay_flags} ${TB_TOP} glbl
        eval exec xelab -incr -debug typical -relax -mt 8 -maxdelay -L xil_defaultlib -L simprims_ver -L secureip -s ${snapshot_name} -transport_int_delays -pulse_r 0 -pulse_int_r 0 -pulse_e 0 -pulse_int_e 0 ${xsim_lib_name}.LWC_TB -generic "G_PERIOD=${clock_period}ns" ${xsim_lib_name}.glbl -log elaborate.log
    }
} else {
    if {${funcsim_use_vhdl}} {
        puts "Analyzing design files"
        eval exec xvhdl ${verbose} ${vhdl_std_opt} ${vhdl_funcsim} ${sim_vhdl_files}
        puts "Elaborating design: ${TB_TOP}"
        eval exec xelab ${verbose}  ${common_xelab_options} -debug all ${TB_TOP}
    } else {
        puts "Analyzing design files"
        eval exec xvlog ${verbose} ${verilog_funcsim}
        eval exec xvhdl ${verbose} ${vhdl_std_opt} ${sim_vhdl_files}
        puts "Elaborating design: ${TB_TOP}"
        eval exec xelab ${verbose}  ${common_xelab_options} -debug all ${TB_TOP} glbl
    }
}


puts "Running simulation"
eval xsim ${snapshot_name} ${xsim_verbose}

file delete ${saif_file}
if {${gen_saif}} {
    open_saif ${saif_file}
    log_saif  [get_objects -r ${uut_scope}/*]
}

open_vcd ${vcd_file}
log_vcd *

#
# puts "[get_objects -recursive -filter {type =~ signal} /LWC_TB/*]"
# set signals [get_objects -filter {TYPE == signal} /LWC_TB/*]
# puts "${objs}"
if {${initialize_zeros}} {
    puts "setting initial values to 0"
    foreach sig [get_objects -recursive -filter {TYPE =~ *signal} ${uut_scope}/*] {
        set_value ${sig} {0}
        # set dim [llength [split [get_property VALUE ${sig}] ,] ]
        # if {$dim == 1} {
        # # add_force ${sig} {0} -cancel_after 27
        # } else {
        # # for {set i 0} {$i < $dim} {incr i} {
        # #   set_value ${sig}[$i] {0}
        # #   # add_force ${sig}[$i] {0} -cancel_after 27
        # # }
        # }

    }
}

puts "Running for maximum of ${max_run}"
run "${max_run}"


# if debug
flush_vcd
close_vcd

if {${gen_saif}} {
    close_saif
}

if {${gen_saif} && false} {
    if {$run_synth_flow != 1} {
        open_checkpoint ${checkpoints_dir}/post_route.dcp
    }

    eval read_saif ${saif_file}

    report_power -file ${reports_dir}/post_route/post_synth_power.rpt
}

