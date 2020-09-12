set design_name           {{design.name}}
set vhdl_std              {{design.vhdl_std}}
set nthreads              {{nthreads}}
set tb_generics_options   "{{flow.generics_options}}"
set tb_top                {{design.tb_top}}
set results_dir           results
# set vhdl_funcsim          ${results_dir}/${top}_impl_funcsim.vhd
# set verilog_funcsim       ${results_dir}/${top}_impl_funcsim.v
# set verilog_timesim       ${results_dir}/${top}_impl_timesim.v
# set sdf_file              "[file rootname ${verilog_timesim}].sdf"
set timing_sim            false
set funcsim_use_vhdl      true
set gen_saif              false

set uut_scope             /${tb_top}/{{design.tb_uut}}
set initialize_zeros      false




set vhdl_std_opt [expr {$vhdl_std == "08" ? "-2008": $vhdl_std == "93" ?  "-93_mode": ""}];
set xelab_vhdl_std_opt [expr {$vhdl_std == "93" ?  "-93_mode": ""}];
set saif_file "${results_dir}/xsim_${tb_top}_dump.saif"
set wdb_file "${results_dir}/xsim_${tb_top}_dump"

{% if 'vcd' in flow and flow.vcd %}
set gen_vcd  true
set vcd_file {{flow.vcd}}
{% else %}
set gen_vcd false
{% endif %}

#FIXME broken
set xsim_lib_name work

# -sdfmax /LWC_TB/uut=${sdf_file}
# open_wave_database ${wdb_file}

set snapshot_name "${tb_top}"

append xelab_flags " -incr -rangecheck -s ${snapshot_name} ${tb_generics_options} "
append xelab_flags " -mt ${nthreads} -log elaborate.log -L ${xsim_lib_name} "
append xelab_flags " -L simprims_ver "
{% if debug %}
    append xelab_flags " -O0 "
{% else %}
    append xelab_flags " -O0 "
{% endif %}

if { {{debug}} || $gen_saif || $gen_vcd } {
    append xelab_flags " -debug typical "
}

file delete -force xsim.dir


set analyze_flags " -incr -rangecheck -work ${xsim_lib_name} {%- if debug %} -verbose 2 {%- endif %}"

set designs "${xsim_lib_name}.${tb_top}"

if {${post_synth_sim}} {

    append designs " glbl"

    if {${timing_sim}} {
        append xelab_flags " -maxdelay -transport_int_delays -pulse_r 0 -pulse_int_r 0 "
    }
}


puts "\n===========================( Analyzing HDL Sources )==========================="
{% for src in design.sources %}
    {% if src.type == 'verilog' %}

        {% if src.variant == 'systemverilog' %}
        puts "Analyzing SystemVerilog file {{src.file}}"
        if { [catch {eval exec xvlog ${analyze_flags} -sv {{src.file}} } myError]} {
            errorExit $myError
        }
        {% else %}
        puts "Analyzing Verilog file {{src.file}}"
	puts "xvlog ${analyze_flags}"
        if { [catch {eval exec xvlog ${analyze_flags} {{src.file}} } myError]} {
            errorExit $myError
        }
        {% endif %}

    {% endif %}
    {% if src.type == 'vhdl' %}
    puts "Analyzing VHDL file {{src.file}} ${vhdl_std_opt}"
    if { [catch {eval  exec xvhdl ${analyze_flags} ${vhdl_std_opt} {{src.file}} } myError]} {
        errorExit $myError
    }
    {% endif %}
{% endfor %}

puts "helo\n"
puts "\n===========================( Elaborating design: ${tb_top} )==========================="
puts "xelab ${xelab_flags} ${xelab_vhdl_std_opt} ${designs}"
eval exec xelab ${xelab_flags} ${xelab_vhdl_std_opt} ${designs} 

# eval exec xelab -incr -debug typical -relax -mt 8 -maxdelay -L xil_defaultlib -L simprims_ver -L secureip -s ${snapshot_name} -transport_int_delays -pulse_r 0 -pulse_int_r 0 -pulse_e 0 -pulse_int_e 0 ${xsim_lib_name}.LWC_TB -generic "G_PERIOD=${clock_period}ns" ${xsim_lib_name}.glbl -log elaborate.log


puts "\n===========================( Loading Simulation )==========================="
eval xsim ${snapshot_name} {% if debug %} -verbose -wdb ${wdb_file} {% endif %}

file delete ${saif_file}
if {${gen_saif}} {
    open_saif ${saif_file}
    log_saif  [get_objects -r ${uut_scope}/*]
}

if {${gen_vcd}} {
    open_vcd ${vcd_file}
    log_vcd *
}
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

puts "\n===========================( Running simulation )==========================="
puts "\n===========================( *ENABLE ECHO* )==========================="
run {% if 'stop_time' in flow %} {{flow.stop_time}} {% else %} all {% endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="


# if debug
if {${gen_vcd}} {
    puts "\n===========================( Saving VCD to ${vcd_file} )==========================="
    flush_vcd
    close_vcd
}

if {${gen_saif}} {
    puts "\n===========================( Saving SAIF to ${vcd_file} )==========================="
    close_saif
}


